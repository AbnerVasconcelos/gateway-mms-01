# CLAUDE.md — Gateway IoT Industrial

Guia de referência para o projeto gateway-palant-01. Leia antes de fazer qualquer modificação.

---

## O que é este projeto

Gateway IoT industrial genérico. Faz a ponte entre CLPs Modbus e sistemas de nuvem/IA via Redis pub/sub. Pode ser adaptado para qualquer aplicação industrial — o mapeamento de tags é inteiramente definido pelos arquivos CSV em `tables/`, sem necessidade de alterar o código.

**Fluxo principal:**

```
CLP (Modbus TCP)
    ↑↓
  Delfos  (leitura)  →  Redis plc_data / alarms  →  [consumidores externos]
  Atena   (escrita)  ←  Redis plc_commands / ia_status / ia_data  ←  [UI / IA]
```

---

## Estrutura do projeto

```
gateway/
├── Delfos/                    # Processo leitor do CLP
│   ├── delfos.py              # Entry point — loop de leitura Modbus
│   ├── modbus_functions.py    # setup_modbus(), read_coils(), read_registers()
│   ├── redis_config_functions.py  # setup_redis(), publish_to_channel(), get_latest_message()
│   ├── table_filter.py        # find_contiguous_groups(), extract_parameters_from_csv()
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
├── tables/
│   ├── operacao.csv           # Mapeamento principal: 81 tags Modbus ↔ JSON
│   ├── configuracao.csv       # Parâmetros de configuração: 41 tags
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

- **Loop:** 1 Hz quando usuário conectado (`user_state=True`), 0,033 Hz quando inativo
- **Lê:** coils e holding registers do CLP via Modbus TCP
- **Publica:** `plc_data` (dados operacionais), `alarms` (dados de alarmes/configuração)
- **Assina:** `user_status` (estado do usuário)
- **CSV:** `operacao.csv` para dados operacionais, `configuracao.csv` para alarmes

**Formato da mensagem publicada:**
```json
{
    "coils":     { "Extrusora": { "extrusoraLigadoDesligado": true }, ... },
    "registers": { "Extrusora": { "extrusoraFeedBackSpeed": 1450 }, ... },
    "timestamp": "2026-02-25T14:23:45.123456"
}
```

**Otimização Modbus:** `find_contiguous_groups()` agrupa endereços contíguos para minimizar roundtrips de rede.

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

## Canais Redis

| Canal | Direção | Produtor | Consumidor | Conteúdo |
|-------|---------|----------|------------|----------|
| `user_status` | ↔ | UI/Cloud | Delfos, Atena | `{"user_state": true/false}` |
| `plc_data` | → | Delfos | Externos | Dados operacionais + timestamp |
| `plc_commands` | → | UI/Cloud | Atena | Comandos de escrita no CLP |
| `alarms` | → | Delfos | Externos | Dados de alarmes + timestamp |
| `ia_status` | → | UI/Cloud | Atena | `{"ia_state": true/false}` |
| `ia_data` | → | IA/Cloud | Atena | Dados do modelo de IA (stub) |

**Persistência adicional (só Delfos):**
- `last_message:{channel}` — último valor publicado (SET Redis)
- `history:{channel}` — histórico com até 1000 registros (LIST Redis)

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

---

## Dependências

```
pyModbusTCP==0.2.1      # cliente Modbus TCP síncrono (em uso)
pymodbus==3.6.4         # servidor Modbus TCP (simulador de testes)
redis==5.0.3            # pub/sub + store
pandas==3.0.1           # leitura de CSV
python-dotenv==1.2.1    # carregamento de .env
numpy==2.4.2            # suporte numérico
pytest==9.0.2           # execução dos testes
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
# Editar os .env com os valores reais do ambiente
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
| `tests/test_atena.py` | Atena — loop Redis → Modbus (6 testes, inicia subprocessos) |
| `tests/test_full_loop.py` | Loop completo Delfos+Atena simultâneos (7 testes, inicia subprocessos) |

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

1. **`table_filter.py` (Delfos)** usa `print()` em `find_contiguous_groups` — substituir por `logger`
2. **`handle_ia_data_message`** é um stub — lógica de processamento de dados da IA não implementada
4. **Código duplicado:** `redis_config_functions.py` e `modbus_functions.py` são idênticos em Delfos e Atena — candidatos a um módulo compartilhado
5. **Sem gerenciamento de processos:** não há supervisor/systemd para reinício automático em produção
6. **Redis sem replicação:** ponto único de falha

---

## O que NÃO fazer

- Não commitar arquivos `.env`
- Não hardcodar IPs, portas ou credenciais no código
- Não usar `print()` — usar `logging`
- Não modificar `operacao.csv` sem entender o impacto nos endereços Modbus
- Não alterar a ordem das colunas nos CSVs (a leitura depende dos nomes das colunas)
