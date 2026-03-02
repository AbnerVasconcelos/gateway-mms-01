# CLAUDE.md — Gateway IoT Industrial

Guia de referência para o projeto gateway-palant-01. Leia antes de fazer qualquer modificação.

---

## O que é este projeto

Gateway IoT industrial modbus. Faz a ponte de CLPs Modbus TCP/IP e RTU para Redis pub/sub. Pode ser adaptado para qualquer aplicação industrial — o mapeamento de tags é inteiramente definido pelos arquivos CSV em `tables/`, sem necessidade de alterar o código.

**Fluxo principal:**

```
CLP (Modbus TCP/RTU)          [múltiplos devices]
    ↑↓
  Delfos  (leitura)  →  Redis plc_alarmes / plc_process / plc_visual / plc_config
                                                    ↓
                                                   Hub  (FastAPI + Socket.IO)
                                                    ↓
                                              Browser / Frontend / IA
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
│   ├── table_filter.py        # find_contiguous_groups(), extract_parameters_by_group(), extract_parameters_by_channel()
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
│   ├── redis_bridge.py        # psubscribe('plc_*') → sio.emit por room (plc:data + plc:<canal>)
│   ├── config_store.py        # leitura/escrita de group_config.json e variable_overrides.json
│   ├── process_manager.py     # ProcessManager — subprocessos Delfos/Atena com log capture
│   ├── simulator_manager.py   # SimulatorManager — simuladores Modbus embarcados (LabTest)
│   ├── templates/
│   │   ├── index.html         # Painel web (sidebar + AG Grid + Bootstrap 5.3 + dark mode)
│   │   └── labtest.html       # Painel LabTest — gerenciamento de simuladores Modbus
│   ├── .env                   # Credenciais locais (NÃO commitar)
│   └── .env.example           # Template de variáveis
│
├── tables/
│   ├── operacao.csv           # Mapeamento principal: 81 tags Modbus ↔ JSON
│   ├── configuracao.csv       # Parâmetros de configuração: 41 tags
│   ├── group_config.json      # Devices, canais (delay_ms, history_size)
│   ├── variable_overrides.json# Exceções por tag individual (enabled, channel)
│   ├── simulator_config.json   # Config dos simuladores embarcados (gerado pelo Hub)
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

- **Loop:** time-tracking com tick de ~50ms; **o canal é a unidade de publicação** — cada canal tem seu próprio timer configurado em `group_config.json["channels"]`
- **Multi-device:** `_build_csv_paths(tables_dir, group_config)` lê a seção `devices` e constrói a lista de CSVs a ler, **pulando devices com `enabled=False`**
- **Lê:** coils e holding registers do CLP via Modbus TCP/RTU, **por canal** (todos os grupos mapeados para o canal são lidos juntos numa única passagem)
- **Publica:** canais segmentados `plc_alarmes`, `plc_process`, `plc_visual`, `plc_config` + `plc_data` (legado, backward-compatible)
- **Assina:** `user_status` (estado do usuário), `config_reload` (hot-reload de config sem reiniciar)

**Formato da mensagem publicada:**
```json
{
    "coils":     { "Extrusora": { "extrusoraLigadoDesligado": true }, ... },
    "registers": { "Extrusora": { "extrusoraFeedBackSpeed": 1450 }, ... },
    "timestamp": "2026-02-25T14:23:45.123456"
}
```

**Otimização Modbus:** `extract_parameters_by_channel()` (em `table_filter.py`) agrega endereços num pool cross-group por canal e calcula grupos contíguos globais, minimizando roundtrips de rede. **Apenas variáveis com canal explícito em `variable_overrides.json` são lidas** — sem canal = ignorada.

**Hot-reload:** ao receber `config_reload`, Delfos recarrega `group_config.json` e `variable_overrides.json`, re-computa `channel_data` (respeitando `enabled` por device) e preserva os timers de canais já existentes.

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

- **Protocolo:** FastAPI + python-socketio (ASGI), inicia com `uvicorn Hub.main:asgi_app --port 4567`
- **Bridge:** `redis_bridge.py` faz `psubscribe('plc_*', 'alarms')` e emite **dois eventos** por mensagem:
  - `plc:data` → backward-compat, payload `{channel, data}`, enviado ao room do canal
  - `plc:<canal>` → específico do canal (ex.: `plc:alarmes`, `plc:process`), payload `data` direto
- **Rooms:** cada canal `plc_<sufixo>` mapeia para o room `<sufixo>` (ex.: `plc_alarmes` → room `alarmes`)
- **Painel web:** serve `templates/index.html` em `GET /` — layout sidebar + grid fullscreen:
  - **Sidebar esquerda (340px):** cards de Processos (Delfos/Atena), Canais Redis, Devices — scroll independente
  - **Área principal (direita):** tabela AG Grid ocupando todo espaço vertical, com toolbar de filtros
  - **Dark mode:** toggle no navbar, persiste em `localStorage`, Bootstrap 5.3 nativo + `ag-theme-alpine-dark`
  - Upload/export `.xlsx`/`.csv`, edição inline, preview em tempo real, navbar de abas por device

**Endpoints REST:**

| Método | Rota | Função |
|--------|------|--------|
| `GET` | `/health` | Health check |
| `GET` | `/api/variables` | Lista todos os tags; `channel=null` quando não atribuída |
| `PATCH` | `/api/variables/{tag}` | Atualiza override de um tag (`enabled`, `channel`); `channel=""` remove atribuição |
| `POST` | `/api/variables/bulk-assign` | Atribui/remove canal de múltiplas tags (`{tags, channel}`; `channel=""` remove) |
| `GET` | `/api/channels` | Lista canais: `{channel: {delay_ms, history_size}}` |
| `POST` | `/api/channels` | Cria canal com prefixo `plc_` |
| `DELETE` | `/api/channels/{channel}` | Remove canal |
| `PATCH` | `/api/channels/{channel}/delay` | Atualiza `delay_ms` do canal + publica `config_reload` |
| `PATCH` | `/api/channels/{channel}/history` | Atualiza `history_size` + aplica `ltrim` imediato no Redis |
| `GET` | `/api/groups` | Retorna `{}` — seção groups removida na Fase 5 |
| `POST` | `/api/upload` | Parseia `.xlsx` ou `.csv` e retorna preview |
| `POST` | `/api/upload/confirm` | Aplica arquivo como nova configuração (só atualiza overrides) |
| `GET` | `/api/export` | Retorna `.xlsx` com configuração atual |
| `GET` | `/api/devices` | Lista todos os devices configurados |
| `POST` | `/api/devices` | Cria novo device |
| `PATCH` | `/api/devices/{id}` | Atualiza campos de um device (`enabled`, `label`, `protocol`, etc.) |
| `DELETE` | `/api/devices/{id}` | Remove device |
| `POST` | `/api/devices/{id}/ping` | Testa conectividade Modbus TCP ou RTU, retorna latência |
| `POST` | `/api/devices/{id}/toggle` | Alterna `enabled` — pausa/retoma leitura do device no Delfos |
| `POST` | `/api/devices/{id}/clear` | Remove overrides de todas as tags do device; `?delete_files=true` apaga CSVs do disco |
| `POST` | `/api/devices/{id}/upload-csv` | Salva CSV em `tables/` e adiciona a `device.csv_files` |
| `GET` | `/api/processes` | Lista processos Delfos/Atena com estado |
| `POST` | `/api/processes/{proc_type}/start` | Inicia Delfos ou Atena (body: `{device_id}`) |
| `POST` | `/api/processes/{proc_type}/stop` | Para o processo |
| `GET` | `/api/processes/{proc_type}/logs` | Últimas linhas de log do processo |
| `GET` | `/labtest` | Serve página LabTest (simuladores) |
| `GET` | `/api/simulators` | Lista simuladores com estado |
| `POST` | `/api/simulators` | Cria novo simulador Modbus |
| `DELETE` | `/api/simulators/{sim_id}` | Remove simulador (para se rodando) |
| `PATCH` | `/api/simulators/{sim_id}` | Atualiza config do simulador (deve estar parado) |
| `POST` | `/api/simulators/{sim_id}/start` | Inicia servidor Modbus TCP |
| `POST` | `/api/simulators/{sim_id}/stop` | Para servidor Modbus TCP |
| `GET` | `/api/simulators/{sim_id}/variables` | Lista variáveis com valores atuais e estado de lock |
| `POST` | `/api/simulators/{sim_id}/upload-csv` | Upload CSV para o simulador |

**Eventos Socket.IO (client → server):** `join`, `plc_write`, `user_status`, `config_save`, `config_get`, `history_set`, `history_get`, `sim_subscribe`, `sim_write`, `sim_lock`

**Eventos Socket.IO (server → client):**
- `plc:data` — todos os canais, payload `{channel, data}` (backward-compat)
- `plc:alarmes`, `plc:process`, `plc:visual`, `plc:config` — por canal, payload `data` direto
- `config:updated` — broadcast ao salvar configuração
- `connection_ack` — enviado ao conectar, contém `available_rooms`
- `history:sizes` — resposta ao `history_get`
- `proc:status` — mudança de estado de processo Delfos/Atena (start/stop/crash)
- `sim:status` — mudança de estado de simulador (start/stop/delete)
- `sim:values` — broadcast periódico (500ms) de valores do simulador ao room `sim:{sim_id}`

**Nota de nomenclatura:** eventos client→server usam underscore (`plc_write`, `sim_write`); eventos server→client usam colon (`plc:data`, `proc:status`, `sim:values`).

---

### ProcessManager — Controle de subprocessos (`Hub/process_manager.py`)

Permite iniciar e parar Delfos e Atena diretamente do painel web, sem terminais separados.

- **Abordagem:** lança subprocessos OS via `asyncio.create_subprocess_exec()`
- **Env vars:** herda `os.environ` e sobrescreve `MODBUS_HOST`, `MODBUS_PORT`, `MODBUS_UNIT_ID`, `REDIS_HOST`, `REDIS_PORT`, `TABLES_DIR` a partir da config do device selecionado. `load_dotenv()` não sobrescreve vars já existentes, então funciona sem alterar Delfos/Atena.
- **`cwd`:** diretório do processo (`Delfos/` ou `Atena/`) para imports relativos
- **Log capture:** stdout/stderr capturados linha a linha (buffer de 200 linhas)
- **Exit detection:** task `_watch_exit()` detecta crash e notifica via `proc:status`
- **Stop:** `terminate()` → timeout 5s → `kill()`
- **Python:** detecta `.venv/Scripts/python.exe` (Win) ou `.venv/bin/python` (Linux), fallback `sys.executable`

**Sem persistência** — processos são efêmeros e param junto com o Hub (`shutdown_all()` no lifecycle).

---

### LabTest — Simuladores Modbus embarcados (`Hub/simulator_manager.py`)

Substitui `tests/modbus_simulator.py` standalone — simuladores rodam dentro do Hub com interface web.

- **Acesso:** `GET /labtest` → painel web em `templates/labtest.html`
- **Persistência:** `tables/simulator_config.json` (criado dinamicamente)
- **Auto-start:** simuladores com `auto_start: true` iniciam no startup do Hub

**SimulatorInstance:**
- Encapsula um `ModbusTcpServer` (pymodbus) com contexto carregado de CSVs
- Suporta coils (tipo M) e holding registers (tipo D)
- **Simulação automática:** varia registers por onda senoidal, coils por toggle aleatório (a cada 2s)
- **Lock de tags:** `sim_lock` trava variável — simulação não sobrescreve, permite escrita manual via `sim_write`
- **Broadcast:** valores emitidos via `sim:values` a cada 500ms ao room `sim:{sim_id}`

**SimulatorManager:**
- CRUD de simuladores com persistência em JSON
- Inicialização via `init_from_config()` no startup do Hub
- Reutiliza `LoggingDataBlock`, `load_csv`, `_initial_register_value` de `tests/modbus_simulator.py`

---

## Canais Redis

| Canal | Direção | Produtor | Consumidor | Freq. típica | Conteúdo |
|-------|---------|----------|------------|--------------|----------|
| `plc_alarmes` | → | Delfos | Hub, externos | 200ms | Grupos de alarme |
| `plc_process` | → | Delfos | Hub, externos | 500ms | Extrusora, Puxador, producao, dosador, alimentador, saidasDigitais |
| `plc_visual` | → | Delfos | Hub, externos | 1s | threeJs (visualização 3D) |
| `plc_config` | → | Delfos | Hub, externos | 5s | totalizadores, configuracao |
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

Delfos e Atena usam o mesmo conjunto de variáveis Modbus. O Hub usa variáveis Redis + porta. Copie `.env.example` para `.env` em cada diretório:

**Delfos / Atena `.env`:**
```bash
MODBUS_HOST=192.168.1.2
MODBUS_PORT=502
MODBUS_UNIT_ID=2
REDIS_HOST=localhost
REDIS_PORT=6379
TABLES_DIR=../tables
```

**Hub `.env`:**
```bash
REDIS_HOST=localhost
REDIS_PORT=6379
TABLES_DIR=../tables
HUB_HOST=0.0.0.0
HUB_PORT=4567
```

**Regra:** `.env` nunca entra no git. `.env.example` sim.

**Nota:** quando Delfos/Atena são iniciados via ProcessManager (painel web), as env vars são passadas programaticamente a partir da config do device — o `.env` local é ignorado (pois `load_dotenv()` não sobrescreve vars já definidas).

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

### `group_config.json` — devices e canais

Define devices Modbus e canais Redis. Lido pelo Delfos na inicialização e a cada `config_reload`.

```json
{
  "_meta": {
    "aggregate_channel": "plc_data",
    "backward_compatible": true,
    "default_delay_ms": 1000,
    "default_history_size": 100
  },
  "devices": {
    "default": {
      "label": "CLP Principal",
      "protocol": "tcp",
      "host": "192.168.1.2",
      "port": 502,
      "unit_id": 2,
      "enabled": true,
      "csv_files": ["operacao.csv", "configuracao.csv"]
    },
    "clp2": {
      "label": "CLP Secundário",
      "protocol": "rtu",
      "serial_port": "COM3",
      "baudrate": 9600,
      "parity": "N",
      "stopbits": 1,
      "unit_id": 1,
      "enabled": true,
      "csv_files": ["clp2_tags.csv"]
    }
  },
  "channels": {
    "plc_alarmes": { "delay_ms": 200,  "history_size": 55  },
    "plc_process": { "delay_ms": 500,  "history_size": 100 },
    "plc_visual":  { "delay_ms": 1000, "history_size": 100 },
    "plc_config":  { "delay_ms": 5000, "history_size": 100 }
  }
}
```

**Campos de device:**
- TCP: `protocol="tcp"`, `host`, `port`, `unit_id`
- RTU: `protocol="rtu"`, `serial_port`, `baudrate`, `parity` ("N"/"E"/"O"), `stopbits`, `unit_id`
- Comum: `label`, `csv_files` (lista de arquivos em `tables/`), `enabled` (default `true`)

**Regras:**
- `delay_ms` e `history_size` ficam exclusivamente na seção `channels`
- Canais sem entrada em `channels` usam os defaults de `_meta`
- **Não existe mais seção `groups`** — nunca adicionar mapeamento grupo→canal aqui
- `enabled: false` em um device → Delfos ignora todos os seus CSVs no próximo hot-reload
- Devices com `csv_files` idênticos causam variáveis duplicadas — **cada device deve ter CSVs exclusivos**

### `variable_overrides.json` — atribuição direta por tag

**Única fonte de roteamento.** Define qual canal cada tag publica. Tags ausentes deste arquivo não são lidas pelo Delfos. Editável pelo painel web ou via `PATCH /api/variables/{tag}`.

```json
{
  "extrusoraLigadoDesligado": { "channel": "plc_process" },
  "emergencia":               { "channel": "plc_alarmes" },
  "extrusoraErro":            { "enabled": false, "channel": "plc_process" }
}
```

**Campos suportados por tag:**
- `channel` — canal Redis onde a tag será publicada (string não-vazia)
- `enabled` — `false` exclui a tag da leitura mesmo que tenha canal atribuído

**Regra de roteamento (Fase 5):**
- Tag com `channel` definido → lida e publicada no canal correspondente
- Tag sem `channel` (ausente do arquivo ou `channel` removido) → **não lida, não publicada**
- `enabled: false` → excluída mesmo com canal atribuído

### `simulator_config.json` — configuração dos simuladores embarcados

Gerado e gerenciado pelo Hub (LabTest). Persistido em `tables/`. Cada chave é um `sim_id`.

```json
{
  "sim_clp1": {
    "label": "Simulador CLP Principal",
    "protocol": "tcp",
    "port": 5020,
    "unit_id": 1,
    "csv_files": ["operacao.csv"],
    "simulate": true,
    "auto_start": false
  }
}
```

**Campos:**
- `protocol` — `"tcp"` ou `"rtu_tcp"` (RTU framer sobre TCP)
- `port` — porta TCP do servidor Modbus
- `csv_files` — lista de CSVs em `tables/` para carregar contexto
- `simulate` — `true` = variação automática de valores; `false` = valores estáticos
- `auto_start` — `true` = inicia automaticamente no startup do Hub

---

## Upload de planilhas

O endpoint `POST /api/upload` e o botão "📂 Upload .xlsx/.csv" aceitam dois formatos:

| Formato | Colunas identificadoras | Uso |
|---------|------------------------|-----|
| **Exportado pelo Hub** | `Tag`, `Canal`, `History size`, `Habilitado` | Re-importar configuração exportada |
| **CSV nativo Modbus** | `ObjecTag`, `key`, `At`, `Modbus` | Importar CSV de mapeamento diretamente |

O formato é detectado automaticamente pela presença de `ObjecTag` no cabeçalho. O upload nativo Modbus só lê metadados (tag, grupo, tipo, endereço) — não altera overrides de canal.

Cada device também aceita upload individual via `POST /api/devices/{id}/upload-csv`, que salva o CSV em `tables/` e adiciona ao `device.csv_files`.

---

## Dependências

```
pyModbusTCP==0.2.1      # cliente Modbus TCP síncrono (em uso)
pymodbus==3.6.4         # servidor Modbus TCP (simulador de testes) + cliente RTU
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
python -m venv .venv
Set-ExecutionPolicy RemoteSigned   # PowerShell como admin
.venv\Scripts\activate
pip install -r requirements.txt
```

**Linux:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configurar variáveis de ambiente

```bash
cp Delfos/.env.example Delfos/.env
cp Atena/.env.example  Atena/.env
cp Hub/.env.example    Hub/.env
# Editar os .env com os valores reais do ambiente
```

### 3. Iniciar Redis

```bash
# Docker (recomendado):
docker run -d -p 6379:6379 --name redis redis:alpine

# Ou nativo:
redis-server
```

### 4. Iniciar o Hub

```bash
uvicorn Hub.main:asgi_app --host 0.0.0.0 --port 4567
# Painel web disponível em http://localhost:4567
```

### 5. Iniciar Delfos e Atena

**Opção A — Via painel web (recomendado para desenvolvimento):**
1. Acesse http://localhost:4567
2. Crie ou selecione um simulador em http://localhost:4567/labtest e inicie-o
3. No card "Processos" (sidebar), selecione o device e clique "Iniciar" para Delfos e/ou Atena
4. Monitore via botão "Logs"

**Opção B — Terminais separados (produção ou debug direto):**
```bash
# Terminal 1 — simulador (desenvolvimento/testes)
python tests/modbus_simulator.py --port 5020 --simulate

# Terminal 2
cd Delfos && python delfos.py

# Terminal 3
cd Atena && python atena.py
```

**Atenção Windows/Cursor IDE:** o Cursor pode interceptar conexões `localhost`. Use o IP da máquina (`192.168.x.x`) para acessar o Hub via browser.

---

## Testes

```bash
# Requer: Redis rodando (docker run -d -p 6379:6379 redis:alpine)
python -m pytest tests/ -v

# Apenas unit tests (sem deps externas):
python -m pytest tests/test_hub.py tests/test_segmented_reading.py -v
```

| Arquivo | Cobre | Testes |
|---------|-------|--------|
| `tests/modbus_simulator.py` | Servidor Modbus TCP que lê os CSVs e simula o CLP | — |
| `tests/test_integration.py` | Simulador — leitura/escrita Modbus direta | 15 |
| `tests/test_segmented_reading.py` | Delfos — leitura segmentada por canal, delays, hot-reload | 30 |
| `tests/test_atena.py` | Atena — loop Redis → Modbus | 6 |
| `tests/test_full_loop.py` | Loop completo Delfos+Atena simultâneos | 7 |
| `tests/test_hub.py` | Hub — bridge, endpoints REST, upload/export, device CRUD, ping, simulators | 61 |

**Total unit tests (sem deps externas):** 91 passando (`test_hub` + `test_segmented_reading`)

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
- **CSVs por device:** cada device deve ter arquivos CSV exclusivos — não compartilhar CSVs entre devices para evitar variáveis duplicadas

---

## Problemas conhecidos

1. **`handle_ia_data_message`** é um stub — lógica de processamento de dados da IA não implementada
2. **Código duplicado:** `redis_config_functions.py` e `modbus_functions.py` são idênticos em Delfos e Atena — candidatos a um módulo compartilhado
3. **Eventos Socket.IO — nomenclatura intencional:** client→server usa underscore (`plc_write`, `sim_write`); server→client usa colon (`plc:data`, `proc:status`, `sim:values`). Frontends devem seguir esta convenção.
4. **Redis sem replicação:** ponto único de falha
5. **Delfos — device único por instância:** Delfos usa um único cliente Modbus; para múltiplos devices físicos simultâneos seria necessário múltiplos clientes (atualmente lê devices em série, não em paralelo)
6. **ProcessManager sem reinício automático:** processos que crasham são detectados (`proc:status` com `exit_code`) mas não reiniciam sozinhos — o usuário precisa clicar "Iniciar" novamente

---

## O que NÃO fazer

- Não commitar arquivos `.env`
- Não hardcodar IPs, portas ou credenciais no código
- Não usar `print()` — usar `logging`
- Não modificar `operacao.csv` sem entender o impacto nos endereços Modbus
- Não alterar a ordem das colunas nos CSVs (a leitura depende dos nomes das colunas)
- Não adicionar o mesmo CSV a múltiplos devices (causa variáveis duplicadas nos overrides)
- Não adicionar seção `groups` ao `group_config.json` — removida na Fase 5
