# CLAUDE.md — Gateway IoT Industrial

Guia de referência para o projeto gateway-palant-01. Leia antes de fazer qualquer modificação.

---

## O que é este projeto

Gateway IoT industrial modbus. Faz a ponte de CLPs Modbus TCP/IP e RTU para Redis pub/sub. Pode ser adaptado para qualquer aplicação industrial — o mapeamento de tags é inteiramente definido pelos arquivos CSV em `tables/`, sem necessidade de alterar o código.

**Fluxo principal:**

```
CLP (Modbus TCP)
    ↑↓
  Delfos  (leitura)  →  Redis plc_alarmes / plc_process / plc_visual / plc_config
                                                    ↓
                                                   Hub  (FastAPI + Socket.IO)
                                                    ↓
                                              Browser / Frontend
  Atena   (escrita)  ←  Redis plc_commands / ia_status / ia_data  ←  [Hub / IA]
```

---

## Estrutura do projeto

```
gateway/
├── Delfos/                    # Processo leitor do CLP
│   ├── delfos.py              # Entry point — time-tracking loop 50ms
│   ├── modbus_functions.py    # setup_modbus(), read_coils(), read_registers()
│   ├── redis_config_functions.py  # setup_redis(), publish_to_channel(), get_latest_message()
│   ├── table_filter.py        # find_contiguous_groups(), extract_parameters_from_csv(), extract_parameters_by_group()
│   ├── .env                   # Credenciais locais (NÃO commitar)
│   └── .env.example           # Template de variáveis
│
├── Atena/                     # Processo escritor do CLP
│   ├── atena.py               # Entry point — loop de eventos Redis
│   ├── data_handle.py         # Handlers por canal (user_status, plc_commands, ia_status, ia_data)
│   ├── modbus_functions.py    # setup_modbus(), write_coils_to_device(), write_registers_to_device()
│   ├── redis_config_functions.py  # setup_redis(), subscribe_to_channels()
│   ├── table_filter.py        # extract_deep_keys(), find_values_by_object_tag()
│   ├── .env                   # Credenciais locais (NÃO commitar)
│   └── .env.example           # Template de variáveis
│
├── Hub/                       # Processo bridge Redis ↔ WebSocket + painel web
│   ├── main.py                # FastAPI + Socket.IO + endpoints REST
│   ├── redis_bridge.py        # psubscribe('plc_*') → sio.emit por room
│   ├── config_store.py        # leitura/escrita de group_config.json e variable_overrides.json
│   ├── templates/
│   │   └── index.html         # Painel web (AG Grid + Bootstrap 5 + Socket.IO — CDN)
│   ├── .env                   # Credenciais locais (NÃO commitar)
│   └── .env.example           # Template de variáveis
│
├── tables/
│   ├── operacao.csv           # Mapeamento principal: 81 tags Modbus ↔ JSON
│   ├── configuracao.csv       # Parâmetros de configuração: 41 tags
│   ├── group_config.json      # Mapeia grupos → canal Redis + delay_ms + history_size
│   ├── variable_overrides.json# Exceções por tag individual (sobrescreve o grupo)
│   ├── alarms_data.csv        # Subconjunto de alarmes (referência)
│   ├── read_data.csv          # Mapeamento simplificado (testes)
│   └── write_data.csv         # Mapeamento de escrita (testes)
│
├── .gitignore
├── requirements.txt
├── notas.txt                  # Comandos de setup (Windows e Linux)
└── CLAUDE.md                  # Este arquivo
```

---

## Processos

### Delfos — Leitor do CLP (`Delfos/delfos.py`)

- **Loop:** time-tracking com tick de ~50ms; cada grupo de variáveis tem delay próprio configurado em `group_config.json`
- **Lê:** coils e holding registers do CLP via Modbus TCP, por grupo
- **Publica:** canais segmentados `plc_alarmes`, `plc_process`, `plc_visual`, `plc_config` + `plc_data` (legado, backward-compatible)
- **Assina:** `user_status` (estado do usuário), `config_reload` (hot-reload de config sem reiniciar)
- **CSV:** `operacao.csv` para dados operacionais, `configuracao.csv` para alarmes

**Formato da mensagem publicada:**
```json
{
    "coils":     { "Extrusora": { "extrusoraLigadoDesligado": true }, ... },
    "registers": { "Extrusora": { "extrusoraFeedBackSpeed": 1450 }, ... },
    "timestamp": "2026-02-25T14:23:45.123456"
}
```

**Otimização Modbus:** `find_contiguous_groups()` agrupa endereços contíguos por grupo para minimizar roundtrips de rede.

**Hot-reload:** ao receber `config_reload`, Delfos recarrega `group_config.json` e `variable_overrides.json` sem reiniciar o processo.

---

### Atena — Escritor do CLP (`Atena/atena.py`)

- **Loop:** blocking `pubsub.listen()` — orientado a eventos
- **Assina:** `user_status`, `plc_commands`, `ia_status`, `ia_data`
- **Escreve:** coils e holding registers no CLP via Modbus TCP

| Canal | Handler | Função | Status |
|-------|---------|--------|--------|
| `user_status` | `handle_user_status_message` | Atualiza `user_state` | Completo |
| `plc_commands` | `handle_plc_commands_message` | Escreve no CLP se `user_state=True` | Completo |
| `ia_status` | `handle_ia_status_message` | Atualiza `ia_mode` | Completo |
| `ia_data` | `handle_ia_data_message` | Processa dados da IA | **STUB — sem implementação** |

**Lookup de endereço:** `find_values_by_object_tag()` busca no CSV pelo campo `ObjecTag` e retorna endereços Modbus correspondentes.

---

### Hub — Bridge Redis ↔ WebSocket (`Hub/main.py`)

- **Protocolo:** FastAPI + python-socketio (ASGI), inicia com `uvicorn Hub.main:asgi_app --port 8000`
- **Bridge:** `redis_bridge.py` faz `psubscribe('plc_*', 'alarms')` e emite `plc:data` para os rooms Socket.IO correspondentes
- **Rooms:** cada canal `plc_<sufixo>` mapeia para o room `<sufixo>` (ex.: `plc_alarmes` → room `alarmes`)
- **Painel web:** serve `templates/index.html` em `GET /` — tabela AG Grid com edição inline, upload/export `.xlsx`, preview em tempo real

**Endpoints REST:**

| Método | Rota | Função |
|--------|------|--------|
| `GET` | `/api/variables` | Lista todos os tags com config mesclada |
| `PATCH` | `/api/variables/{tag}` | Atualiza override de um tag |
| `GET` | `/api/channels` | Lista canais com `delay_ms` e `history_size` |
| `PATCH` | `/api/channels/{channel}/history` | Atualiza `history_size` + aplica `ltrim` imediato no Redis |
| `GET` | `/api/groups` | Lista grupos e configurações |
| `POST` | `/api/upload` | Parseia `.xlsx` e retorna preview |
| `POST` | `/api/upload/confirm` | Aplica `.xlsx` como nova configuração |
| `GET` | `/api/export` | Retorna `.xlsx` com configuração atual |

**Eventos Socket.IO (client → server):** `join`, `plc_write`, `user_status`, `config_save`, `config_get`, `history_set`, `history_get`

**Nota de nomenclatura:** eventos client→server usam underscore (`plc_write`); eventos server→client usam colon (`plc:data`, `config:updated`).

---

## Canais Redis

| Canal | Direção | Produtor | Consumidor | Freq. típica | Conteúdo |
|-------|---------|----------|------------|--------------|----------|
| `plc_alarmes` | → | Delfos | Hub, externos | 200ms | Grupos de alarme |
| `plc_process` | → | Delfos | Hub, externos | 500ms–2s | Extrusora, Puxador, producao, dosador, alimentador, saidasDigitais |
| `plc_visual` | → | Delfos | Hub, externos | 1s | threeJs (visualização 3D) |
| `plc_config` | → | Delfos | Hub, externos | 5s–10s | totalizadores, configuracao |
| `plc_data` | → | Delfos | Legado | igual ao grupo mais rápido | Todos os dados (backward-compatible) |
| `alarms` | → | Delfos | Hub, externos | igual `plc_config` | Dados de alarmes + timestamp |
| `plc_commands` | → | Hub/UI | Atena | sob demanda | Comandos de escrita no CLP |
| `user_status` | ↔ | Hub/UI | Delfos, Atena | sob demanda | `{"user_state": true/false}` |
| `config_reload` | → | Hub | Delfos | sob demanda | `{"reload": true}` — aciona hot-reload |
| `ia_status` | → | IA/Cloud | Atena | sob demanda | `{"ia_state": true/false}` |
| `ia_data` | → | IA/Cloud | Atena | sob demanda | Dados do modelo de IA (stub) |

**Persistência adicional (só Delfos):**
- `last_message:{channel}` — último valor publicado (SET Redis)
- `history:{channel}` — histórico com tamanho configurável por canal em `group_config.json` (LIST Redis)

---

## Variáveis de ambiente

Ambos os processos usam o mesmo conjunto de variáveis. Copie `.env.example` para `.env` em cada diretório:

```bash
MODBUS_HOST=192.168.1.2
MODBUS_PORT=502
MODBUS_UNIT_ID=2
REDIS_HOST=localhost
REDIS_PORT=6379
TABLES_DIR=../tables
```

**Regra:** `.env` nunca entra no git. `.env.example` sim.

---

## Tabelas CSV

### `operacao.csv` — mapeamento operacional

Colunas relevantes:

| Coluna | Descrição |
|--------|-----------|
| `key` | Namespace lógico (`Extrusora`, `Puxador`, `threeJs`, etc.) |
| `ObjecTag` | Nome da variável no JSON e na HMI |
| `Tipo` | `M` = coil, `D` = register |
| `Modbus` | Endereço Modbus inteiro |
| `At` | `%MB` = coil, `%MW` = holding register |

**Domínios:** Extrusora (7 tags), Puxador (7 tags), producao (14 tags), threeJs (30 tags), saidasDigitais (5 tags), dosador, alimentador, totalizadores, alarmes.

### `configuracao.csv` — parâmetros de configuração

Parâmetros de calibração, PID, receitas e limites. Mesma estrutura de colunas.

### `group_config.json` — configuração de canais e grupos

Mapeia cada grupo (campo `key` do CSV) para um canal Redis, delay de publicação e tamanho de histórico. Lido pelo Delfos na inicialização e a cada `config_reload`.

```json
{
  "_meta": { "aggregate_channel": "plc_data", "backward_compatible": true },
  "groups": {
    "alarmes":        { "channel": "plc_alarmes", "delay_ms": 200,   "history_size": 100 },
    "saidasDigitais": { "channel": "plc_process", "delay_ms": 500,   "history_size": 100 },
    "Extrusora":      { "channel": "plc_process", "delay_ms": 1000,  "history_size": 100 },
    "threeJs":        { "channel": "plc_visual",  "delay_ms": 1000,  "history_size": 100 },
    "totalizadores":  { "channel": "plc_config",  "delay_ms": 5000,  "history_size": 100 },
    "_configuracao":  { "channel": "plc_config",  "delay_ms": 10000, "history_size": 100 }
  }
}
```

**Regra de precedência:** `variable_overrides.json` > `group_config.json` > padrão do grupo.

### `variable_overrides.json` — exceções por tag

Sobrescreve a configuração do grupo para tags individuais. Editável pelo painel web ou via `PATCH /api/variables/{tag}`.

```json
{
  "emergencia":     { "enabled": true,  "channel": "plc_alarmes", "delay_ms": 100   },
  "densidadeMedia": { "enabled": false, "channel": "plc_config",  "delay_ms": 10000 }
}
```

---

## Dependências

```
pyModbusTCP==0.2.1      # cliente Modbus TCP síncrono (em uso)
pymodbus==3.6.4         # servidor Modbus TCP (simulador de testes)
redis==5.0.3            # pub/sub + store (inclui redis.asyncio para o Hub)
pandas==3.0.1           # leitura de CSV
python-dotenv==1.2.1    # carregamento de .env
numpy==2.4.2            # suporte numérico
pytest==9.0.2           # execução dos testes
fastapi                 # Hub — framework web ASGI
uvicorn[standard]       # Hub — servidor ASGI
python-socketio==5.x    # Hub — Socket.IO server
openpyxl                # Hub — leitura/escrita de .xlsx
python-multipart>=0.0.5 # Hub — upload de arquivos (FastAPI File)
```

---

## Como executar

### 1. Configurar ambiente

**Windows:**
```powershell
python -m venv Gateway
Set-ExecutionPolicy RemoteSigned   # PowerShell como admin
Gateway\Scripts\activate
pip install -r requirements.txt
```

**Linux:**
```bash
python -m venv gateway
source gateway/bin/activate
pip install -r requirements.txt
find . -type f -iname "*.py" -exec chmod +x {} \;
```

### 2. Configurar variáveis de ambiente

```bash
cp Delfos/.env.example Delfos/.env
cp Atena/.env.example  Atena/.env
cp Hub/.env.example    Hub/.env
# Editar os .env com os valores reais do ambiente
```

**Hub `.env` adicional:**
```bash
REDIS_HOST=localhost
REDIS_PORT=6379
TABLES_DIR=../tables
HUB_HOST=0.0.0.0
HUB_PORT=8000
```

### 3. Iniciar Redis

```bash
redis-server
```

### 4. Iniciar os processos (terminais separados)

```bash
# Terminal 1
cd Delfos && python delfos.py

# Terminal 2
cd Atena && python atena.py

# Terminal 3
uvicorn Hub.main:asgi_app --host 0.0.0.0 --port 8000
# Painel web disponível em http://localhost:8000
```

---

## Testes

```bash
# Requer: Redis rodando (docker run -d -p 6379:6379 redis:alpine)
python -m pytest tests/ -v
```

| Arquivo | Cobre |
|---------|-------|
| `tests/modbus_simulator.py` | Servidor Modbus TCP que lê os CSVs e simula o CLP |
| `tests/test_integration.py` | Simulador — leitura/escrita Modbus direta (15 testes) |
| `tests/test_segmented_reading.py` | Delfos — leitura segmentada por grupo, delays, hot-reload (27 testes) |
| `tests/test_atena.py` | Atena — loop Redis → Modbus (6 testes, inicia subprocessos) |
| `tests/test_full_loop.py` | Loop completo Delfos+Atena simultâneos (7 testes, inicia subprocessos) |
| `tests/test_hub.py` | Hub — bridge Redis→Socket.IO, endpoints REST, upload/export (53 testes) |

Para apontar Delfos/Atena ao simulador localmente:
```bash
cp tests/.env.test Delfos/.env
cp tests/.env.test Atena/.env
```

---

## Padrões do projeto

- **Logging:** usar `logging` em vez de `print()`. Formato: `%(asctime)s [%(name)s] %(levelname)s: %(message)s`
- **Retry:** 3 tentativas com `sleep()` em todas as operações de I/O externo
- **Env vars:** nunca hardcodar IPs, portas ou credenciais — sempre via `os.environ.get()`
- **Timestamps:** ISO 8601 (`datetime.datetime.now().isoformat()`)
- **Segurança de escrita:** Atena só escreve no CLP se `user_state=True`

---

## Problemas conhecidos

1. **`handle_ia_data_message`** é um stub — lógica de processamento de dados da IA não implementada
2. **Código duplicado:** `redis_config_functions.py` e `modbus_functions.py` são idênticos em Delfos e Atena — candidatos a um módulo compartilhado
3. **Eventos Socket.IO — nomenclatura inconsistente:** client→server usa underscore (`plc_write`, `user_status`); server→client usa colon (`plc:data`, `config:updated`). Frontends devem seguir a implementação, não o ROADMAP.
4. **Sem gerenciamento de processos:** não há supervisor/systemd para reinício automático em produção
5. **Redis sem replicação:** ponto único de falha

---

## O que NÃO fazer

- Não commitar arquivos `.env`
- Não hardcodar IPs, portas ou credenciais no código
- Não usar `print()` — usar `logging`
- Não modificar `operacao.csv` sem entender o impacto nos endereços Modbus
- Não alterar a ordem das colunas nos CSVs (a leitura depende dos nomes das colunas)
