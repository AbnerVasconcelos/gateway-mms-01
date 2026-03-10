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
  Atena   (escrita)  ←  Redis {device_id}_commands / ia_status / ia_data  ←  [Hub / IA]
```

---

## Estrutura do projeto

```
gateway/
├── shared/                    # Módulos compartilhados entre Delfos e Atena
│   └── modbus_functions.py    # ModbusClientWrapper, setup_modbus(protocol=), read/write unificados
│
├── Delfos/                    # Processo leitor do CLP
│   ├── delfos.py              # Entry point — time-tracking loop 50ms
│   ├── modbus_functions.py    # (symlink/import de shared) setup_modbus(), read_coils(), read_registers()
│   ├── redis_config_functions.py  # setup_redis(), publish_to_channel(), get_latest_message()
│   ├── table_filter.py        # find_contiguous_groups(), extract_parameters_by_group(), extract_parameters_by_channel()
│   ├── .env                   # Credenciais locais (NÃO commitar)
│   └── .env.example           # Template de variáveis
│
├── Atena/                     # Processo escritor do CLP
│   ├── atena.py               # Entry point — loop de eventos Redis
│   ├── data_handle.py         # Handlers por canal (user_status, plc_commands, ia_status, ia_data)
│   ├── modbus_functions.py    # (symlink/import de shared) setup_modbus(), write_coils_to_device(), write_registers_to_device()
│   ├── redis_config_functions.py  # setup_redis(), subscribe_to_channels()
│   ├── table_filter.py        # extract_deep_keys(), find_values_by_object_tag()
│   ├── .env                   # Credenciais locais (NÃO commitar)
│   └── .env.example           # Template de variáveis
│
├── Hub/                       # Processo bridge Redis ↔ WebSocket + painel web
│   ├── main.py                # FastAPI + Socket.IO + endpoints REST
│   ├── redis_bridge.py        # subscrição dinâmica por device → sio.emit por room (device:data + channel:data)
│   ├── config_store.py        # leitura/escrita de group_config.json e variable_overrides_{device_id}.json
│   ├── process_manager.py     # ProcessManager — subprocessos Delfos/Atena com log capture
│   ├── simulator_manager.py   # SimulatorManager — simuladores Modbus embarcados (LabTest)
│   ├── scanner_manager.py     # ScannerManager — scan individual de variáveis Modbus
│   ├── templates/
│   │   ├── index.html         # Painel web (sidebar + AG Grid + Bootstrap 5.3 + dark mode)
│   │   ├── labtest.html       # Painel LabTest — gerenciamento de simuladores Modbus
│   │   └── monitor.html       # Monitor — visualizador de dados Redis/Socket.IO em tempo real
│   ├── .env                   # Credenciais locais (NÃO commitar)
│   └── .env.example           # Template de variáveis
│
├── tables/
│   ├── mapeamento_clp.csv     # Mapeamento principal do CLP (io_fisicas + retentivas + globais)
│   ├── io_fisicas.csv         # I/O físicas: DI, DO, AI, AO
│   ├── retentivas.csv         # Variáveis retentivas do CLP
│   ├── globais.csv            # Variáveis globais do CLP
│   ├── temperatura_24z.csv    # Mapeamento controlador temperatura 24 zonas
│   ├── temperatura_28z.csv    # Mapeamento controlador temperatura 28 zonas
│   ├── group_config.json      # Devices com canais por device (delay_ms, history_size)
│   ├── variable_overrides_{device_id}.json  # Overrides por device (canal, enabled)
│   ├── simulator_config.json  # Config dos simuladores embarcados (gerado pelo Hub)
│   ├── scan_results_{device_id}.json  # Resultados de scan por device (gerado pelo Hub)
│   └── csv_individuais/       # Backup dos CSVs individuais originais
│
├── scripts/
│   ├── transform_tables.py    # Script de transformação de tabelas brutas → CSVs formatados
│   └── migrate_config.py      # Migração: global variable_overrides.json → per-device files
│
├── .gitignore
├── requirements.txt
├── notas.txt                  # Comandos de setup (Windows e Linux)
└── CLAUDE.md                  # Este arquivo
```

---

## Processos

### Delfos — Leitor do CLP (`Delfos/delfos.py`)

- **Isolamento por device:** cada instância Delfos opera sobre um único device. Requer `DEVICE_ID` (obrigatório) como variável de ambiente.
- **Loop:** time-tracking com tick de ~50ms; **o canal é a unidade de publicação** — cada canal tem seu próprio timer configurado nos `channels` do device em `group_config.json`
- **CSVs:** lê apenas os CSVs do seu device (`group_config["devices"][DEVICE_ID]["csv_files"]`)
- **Lê:** coils e holding registers do CLP via Modbus TCP/RTU, **por canal** (todos os grupos mapeados para o canal são lidos juntos numa única passagem)
- **Publica:** canais segmentados conforme configurados no device (ex.: `plc_alarmes`, `plc_process`, `plc_visual`, `plc_config`)
- **Assina:** `user_status` (estado do usuário), `config_reload_{device_id}` (hot-reload de config sem reiniciar)

**Formato da mensagem publicada:**
```json
{
    "coils":     { "Extrusora": { "extrusoraLigadoDesligado": true }, ... },
    "registers": { "Extrusora": { "extrusoraFeedBackSpeed": 1450 }, ... },
    "timestamp": "2026-02-25T14:23:45.123456"
}
```

**Otimização Modbus:** `extract_parameters_by_channel()` (em `table_filter.py`) agrega endereços num pool cross-group por canal e calcula grupos contíguos globais, minimizando roundtrips de rede. **Apenas variáveis com canal explícito em `variable_overrides_{device_id}.json` são lidas** — sem canal = ignorada.

**Hot-reload:** ao receber `config_reload_{device_id}`, Delfos recarrega `group_config.json` e `variable_overrides_{device_id}.json`, re-computa `channel_data` e preserva os timers de canais já existentes.

---

### Atena — Escritor do CLP (`Atena/atena.py`)

- **Isolamento por device:** cada instância Atena opera sobre um único device. Requer `DEVICE_ID` (obrigatório) e `COMMAND_CHANNEL` (obrigatório) como variáveis de ambiente.
- **Loop:** blocking `pubsub.listen()` — orientado a eventos
- **CSVs:** lê apenas os CSVs do seu device (`group_config["devices"][DEVICE_ID]["csv_files"]`)
- **Assina:** `user_status`, `{device_id}_commands` (via `COMMAND_CHANNEL`), `ia_status`, `ia_data`
- **Escreve:** coils e holding registers no CLP via Modbus TCP ou RTU over TCP

| Canal | Handler | Função | Status |
|-------|---------|--------|--------|
| `user_status` | `handle_user_status_message` | Atualiza `user_state` | Completo |
| `{device_id}_commands` | `handle_plc_commands_message` | Escreve no CLP se `user_state=True` | Completo |
| `ia_status` | `handle_ia_status_message` | Atualiza `ia_mode` | Completo |
| `ia_data` | `handle_ia_data_message` | Processa dados da IA | **STUB — sem implementação** |

**Lookup de endereço:** `find_values_by_object_tag()` busca nos CSVs do device pelo campo `ObjecTag` e retorna endereços Modbus correspondentes. `handle_plc_commands_message` aceita lista de caminhos CSV.

---

### Hub — Bridge Redis ↔ WebSocket (`Hub/main.py`)

- **Protocolo:** FastAPI + python-socketio (ASGI), inicia com `uvicorn Hub.main:asgi_app --port 4567`
- **Bridge:** `redis_bridge.py` usa subscrição dinâmica baseada nos canais configurados em cada device. `start_bridge(sio, get_channel_map)` recebe um callable que retorna o mapa de canais ativos. Emite **dois eventos** por mensagem:
  - `device:data` → payload `{channel, data}`, enviado ao room do device (`device_id`)
  - `channel:data` → payload `{channel, data}`, enviado ao room do canal (`device_id:channel`)
- **Rooms:** cada device mapeia para o room `device_id`; cada canal mapeia para o room `device_id:channel` (ex.: `simulador:plc_alarmes`)
- **Hot-reload de subscrições:** ao receber mensagem no canal `_bridge_reload`, a bridge recalcula as subscrições via `get_channel_map()` e atualiza dinamicamente
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
| `POST` | `/api/variables/bulk-enable` | Habilita/desabilita múltiplas tags (`{tags, enabled}`) |
| `GET` | `/api/channels` | Lista canais: `{channel: {delay_ms, history_size}}` |
| `POST` | `/api/channels` | Cria canal com prefixo `plc_` |
| `DELETE` | `/api/channels/{channel}` | Remove canal (bloqueia canais de sistema) |
| `GET` | `/api/channels/system` | Lista canais de sistema que não podem ser removidos |
| `PATCH` | `/api/channels/{channel}/delay` | Atualiza `delay_ms` do canal + publica `config_reload_{device_id}` |
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
| `POST` | `/api/devices/{id}/scan` | Inicia scan de variáveis. Body: `{interval_ms, retries, channel}`. 409 se Delfos rodando ou scan ativo |
| `POST` | `/api/devices/{id}/scan/cancel` | Cancela scan em andamento |
| `GET` | `/api/devices/{id}/scan` | Retorna sessão de scan atual com resultados |
| `GET` | `/api/devices/{id}/scan/results` | Retorna `{tag: resultado}` para o grid |
| `GET` | `/api/processes` | Lista processos Delfos/Atena com estado |
| `POST` | `/api/processes/{proc_type}/start` | Inicia Delfos ou Atena (body: `{device_id}`) |
| `POST` | `/api/processes/{proc_type}/stop` | Para o processo (body: `{device_id}`) |
| `GET` | `/api/processes/{proc_type}/logs` | Últimas linhas de log do processo (`?device_id=`) |
| `GET` | `/labtest` | Serve página LabTest (simuladores) |
| `GET` | `/monitor` | Serve página Monitor — visualizador de dados Redis/Socket.IO em tempo real |
| `GET` | `/api/simulators` | Lista simuladores com estado |
| `POST` | `/api/simulators` | Cria novo simulador Modbus |
| `DELETE` | `/api/simulators/{sim_id}` | Remove simulador (para se rodando) |
| `PATCH` | `/api/simulators/{sim_id}` | Atualiza config do simulador (deve estar parado) |
| `POST` | `/api/simulators/{sim_id}/start` | Inicia servidor Modbus TCP |
| `POST` | `/api/simulators/{sim_id}/stop` | Para servidor Modbus TCP |
| `GET` | `/api/simulators/{sim_id}/variables` | Lista variáveis com valores atuais e estado de lock |
| `POST` | `/api/simulators/{sim_id}/upload-csv` | Upload CSV para o simulador |

**Eventos Socket.IO (client → server):** `join`, `plc_write`, `user_status`, `config_save`, `config_get`, `history_set`, `history_get`, `sim_subscribe`, `sim_write`, `sim_lock`, `scan_subscribe`

**Eventos Socket.IO (server → client):**
- `device:data` — dados por device, payload `{channel, data}`, enviado ao room `device_id`
- `channel:data` — dados por canal, payload `{channel, data}`, enviado ao room `device_id:channel`
- `config:updated` — broadcast ao salvar configuração
- `connection_ack` — enviado ao conectar, contém `available_rooms`
- `history:sizes` — resposta ao `history_get`
- `proc:status` — mudança de estado de processo Delfos/Atena (start/stop/crash)
- `sim:status` — mudança de estado de simulador (start/stop/delete)
- `sim:values` — broadcast periódico (500ms) de valores do simulador ao room `sim:{sim_id}`
- `scan:variable` — resultado de scan por variável, enviado ao room `scan:{device_id}`
- `scan:complete` — scan finalizado (completed/cancelled/error), enviado ao room `scan:{device_id}`

**Nota de nomenclatura:** eventos client→server usam underscore (`plc_write`, `sim_write`, `scan_subscribe`); eventos server→client usam colon (`device:data`, `channel:data`, `proc:status`, `sim:values`, `scan:variable`, `scan:complete`).

---

### ProcessManager — Controle de subprocessos (`Hub/process_manager.py`)

Permite iniciar e parar Delfos e Atena diretamente do painel web, sem terminais separados. Suporta **múltiplos processos do mesmo tipo** para devices diferentes.

- **Assinatura:** `start_process(proc_type, device_id, config)` — `proc_id` é derivado como `{proc_type}:{device_id}` (ex.: `delfos:simulador`, `atena:clp2`)
- **Abordagem:** lança subprocessos OS via `asyncio.create_subprocess_exec()`
- **Env vars:** herda `os.environ` e sobrescreve `MODBUS_HOST`, `MODBUS_PORT`, `MODBUS_UNIT_ID`, `MODBUS_PROTOCOL`, `REDIS_HOST`, `REDIS_PORT`, `TABLES_DIR` a partir da config do device selecionado. Adicionalmente passa `DEVICE_ID`, `COMMAND_CHANNEL` (ex.: `{device_id}_commands`) e `CONFIG_RELOAD_CHANNEL` (ex.: `config_reload_{device_id}`). `load_dotenv()` não sobrescreve vars já existentes, então funciona sem alterar Delfos/Atena.
- **`cwd`:** diretório do processo (`Delfos/` ou `Atena/`) para imports relativos
- **Log capture:** stdout/stderr capturados linha a linha (buffer de 200 linhas)
- **Exit detection:** task `_watch_exit()` detecta crash e notifica via `proc:status`
- **Stop:** `terminate()` → timeout 5s → `kill()`
- **Python:** detecta `.venv/Scripts/python.exe` (Win) ou `.venv/bin/python` (Linux), fallback `sys.executable`
- **Multi-device:** pode rodar simultaneamente `delfos:simulador`, `delfos:clp2`, `atena:simulador`, etc.

**Sem persistência** — processos são efêmeros e param junto com o Hub (`shutdown_all()` no lifecycle).

---

### ModbusClientWrapper — Cliente unificado TCP/RTU (`shared/modbus_functions.py`)

Abstração que unifica a API de `pyModbusTCP` (TCP puro) e `pymodbus` (RTU over TCP) num único wrapper.

- **`setup_modbus(protocol=)`** — aceita `"tcp"` (default, usa `pyModbusTCP.client.ModbusClient`) ou `"rtu_tcp"` (usa `pymodbus.client.ModbusTcpClient` com `ModbusRtuFramer`)
- **API unificada:** `read_coils()`, `read_holding_registers()`, `write_single_coil()`, `write_single_register()`, `open()`, `close()` — mesma assinatura independente do protocolo
- **RTU error handling:** chamadas RTU verificam `result.isError()` e levantam `Exception` com contexto
- **Seleção de protocolo:** Delfos e Atena leem `MODBUS_PROTOCOL` do env e passam para `setup_modbus()`; o ProcessManager propaga o `protocol` do device config

---

### LabTest — Simuladores Modbus embarcados (`Hub/simulator_manager.py`)

Substitui `tests/modbus_simulator.py` standalone — simuladores rodam dentro do Hub com interface web.

- **Acesso:** `GET /labtest` → painel web em `templates/labtest.html`
- **Persistência:** `tables/simulator_config.json` (criado dinamicamente)
- **Auto-start:** simuladores com `auto_start: true` iniciam no startup do Hub

**SimulatorInstance:**
- Encapsula um `ModbusTcpServer` (pymodbus) com contexto carregado de CSVs
- Suporta coils (tipo M) e holding registers (tipo D)
- **Simulação automática:** varia registers por onda senoidal, coils por toggle aleatório — parâmetros configuráveis:
  - `sim_interval` — segundos entre ciclos (default 2.0)
  - `sim_registers` — quantos registers variar por ciclo (default 8, 0=todos)
  - `sim_coils` — quantos coils variar por ciclo (default 12, 0=todos)
  - `sim_coil_prob` — probabilidade de toggle por coil (default 0.3)
- **Lock de tags:** `sim_lock` trava variável — simulação não sobrescreve, permite escrita manual via `sim_write`
- **Broadcast:** valores emitidos via `sim:values` a cada 500ms ao room `sim:{sim_id}`
- **Edição em runtime:** modal de configurações no painel LabTest (botão engrenagem) permite alterar todos os parâmetros do simulador (deve estar parado)

**SimulatorManager:**
- CRUD de simuladores com persistência em JSON
- Inicialização via `init_from_config()` no startup do Hub
- Reutiliza `LoggingDataBlock`, `load_csv`, `_initial_register_value` de `tests/modbus_simulator.py`

---

### Scanner — Leitura individual de variáveis (`Hub/scanner_manager.py`)

Lê variáveis de um device uma a uma, registra quais retornam valor válido e quais dão erro. Permite identificar e desabilitar variáveis problemáticas antes de colocar em produção.

- **Acesso:** botão "Scanner" no card de device em `templates/index.html` → abre modal
- **Proteção cruzada:** scan bloqueia início do Delfos e vice-versa (409 HTTP)
- **Persistência:** `tables/scan_results_{device_id}.json` — gravado ao finalizar scan, carregado no startup do Hub

**ScanSession** — dataclass com estado de uma sessão de scan:
- Campos: `device_id`, `status` (running/completed/cancelled/error), `config`, `total`, `scanned`, `ok_count`, `error_count`, `results`, `started_at`, `finished_at`
- Métodos: `cancel()`, `to_dict()`, `to_summary()`

**ScannerManager** — gerenciador de sessões (padrão `SimulatorManager`):
- `start_scan(device_id, device_cfg, variables, config, progress_callback)` — inicia scan async
- `cancel_scan(device_id)` — cancela scan em andamento
- `get_scan(device_id)` / `is_scanning(device_id)` — consulta estado
- `get_results_for_grid(device_id)` — retorna `{tag: {status, latency_ms, error, value}}`
- `load_cached_results(device_id)` / `load_all_cached()` — lê JSONs persistidos

**Fluxo de scan:**
1. Cria conexão Modbus via `_create_modbus_client()` (mesma do `shared/modbus_functions.py`)
2. Itera variáveis, lê uma por vez via `run_in_executor`
3. Emite `scan:variable` via callback a cada variável
4. Respeita `interval_ms` entre leituras com `asyncio.sleep()`
5. Salva resultados em JSON ao finalizar
6. Emite `scan:complete` ao finalizar

---

## Canais Redis

| Canal | Direção | Produtor | Consumidor | Freq. típica | Conteúdo |
|-------|---------|----------|------------|--------------|----------|
| `plc_alarmes` | → | Delfos | Hub, externos | 200ms | Grupos de alarme |
| `plc_process` | → | Delfos | Hub, externos | 500ms | Extrusora, Puxador, producao, dosador, alimentador, saidasDigitais |
| `plc_visual` | → | Delfos | Hub, externos | 1s | threeJs (visualização 3D) |
| `plc_config` | → | Delfos | Hub, externos | 5s | totalizadores, configuracao |
| `{device_id}_commands` | → | Hub/UI | Atena | sob demanda | Comandos de escrita no CLP (por device) |
| `user_status` | ↔ | Hub/UI | Delfos, Atena | sob demanda | `{"user_state": true/false}` |
| `config_reload_{device_id}` | → | Hub | Delfos | sob demanda | `{"reload": true}` — aciona hot-reload do device |
| `_bridge_reload` | → | Hub | redis_bridge | sob demanda | Aciona recalculo de subscrições da bridge |
| `ia_status` | → | IA/Cloud | Atena | sob demanda | `{"ia_state": true/false}` |
| `ia_data` | → | IA/Cloud | Atena | sob demanda | Dados do modelo de IA (stub) |

**Persistência adicional (só Delfos):**
- `last_message:{channel}` — último valor publicado (SET Redis)
- `history:{channel}` — histórico com tamanho configurável por canal nos `channels` do device em `group_config.json` (LIST Redis)

---

## Variáveis de ambiente

Delfos e Atena usam o mesmo conjunto de variáveis Modbus. O Hub usa variáveis Redis + porta. Copie `.env.example` para `.env` em cada diretório:

**Delfos / Atena `.env`:**
```bash
DEVICE_ID=simulador            # (obrigatório) ID do device em group_config.json
MODBUS_HOST=192.168.1.2
MODBUS_PORT=502
MODBUS_UNIT_ID=2
MODBUS_PROTOCOL=tcp            # tcp ou rtu_tcp (RTU over TCP)
REDIS_HOST=localhost
REDIS_PORT=6379
TABLES_DIR=../tables
COMMAND_CHANNEL=simulador_commands      # (Atena only) canal de comandos do device
CONFIG_RELOAD_CHANNEL=config_reload_simulador  # canal de hot-reload do device
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

### CSVs de mapeamento Modbus

Os CSVs de mapeamento foram reorganizados em arquivos individuais por domínio. O mapeamento ativo está nos CSVs individuais referenciados por cada device em `group_config.json`.

**CSVs ativos:**
- `mapeamento_clp.csv` — mapeamento consolidado do CLP principal (I/O, retentivas, globais)
- `io_fisicas.csv` — entradas/saídas digitais e analógicas (DI, DO, AI, AO)
- `retentivas.csv` — variáveis retentivas
- `globais.csv` — variáveis globais do processo (extrusora, bomba, calandras, puxador, bobinadores, alarmes)
- `temperatura_24z.csv` — controlador de temperatura 24 zonas (protocolo RTU over TCP)
- `temperatura_28z.csv` — controlador de temperatura 28 zonas (protocolo RTU over TCP)

Colunas relevantes (mesmo formato em todos):

| Coluna | Descrição |
|--------|-----------|
| `key` | Namespace lógico (`alarmes`, `producao`, `controle_extrusora`, `corte`, etc.) |
| `ObjecTag` | Nome da variável no JSON e na HMI |
| `Tipo` | `M` = coil, `D` = register |
| `Modbus` | Endereço Modbus inteiro |
| `At` | `%MB` = coil, `%MW` = holding register |
| `Classe` | (opcional) Classificação da variável — exibida na API `/api/variables` |

### `group_config.json` — devices e canais

Define devices Modbus com canais Redis **por device**. Lido pelo Delfos na inicialização e a cada `config_reload_{device_id}`.

```json
{
  "_meta": {
    "default_delay_ms": 1000,
    "default_history_size": 100
  },
  "devices": {
    "simulador": {
      "label": "CLP Principal",
      "protocol": "tcp",
      "host": "localhost",
      "port": 5020,
      "unit_id": 1,
      "csv_files": ["mapeamento_clp.csv"],
      "command_channel": "simulador_commands",
      "channels": {
        "plc_alarmes": { "delay_ms": 200,  "history_size": 55  },
        "plc_process": { "delay_ms": 500,  "history_size": 100 },
        "plc_visual":  { "delay_ms": 1000, "history_size": 100 },
        "plc_config":  { "delay_ms": 1000, "history_size": 100 }
      }
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
      "csv_files": ["clp2_tags.csv"],
      "command_channel": "clp2_commands",
      "channels": {
        "plc_process": { "delay_ms": 500,  "history_size": 100 }
      }
    }
  }
}
```

**Campos de device:**
- TCP: `protocol="tcp"`, `host`, `port`, `unit_id`
- RTU: `protocol="rtu"`, `serial_port`, `baudrate`, `parity` ("N"/"E"/"O"), `stopbits`, `unit_id`
- Comum: `label`, `csv_files` (lista de arquivos em `tables/`), `enabled` (default `true`), `command_channel` (canal de comandos do device), `channels` (canais Redis com `delay_ms` e `history_size`)

**Regras:**
- `delay_ms` e `history_size` ficam dentro da seção `channels` de cada device
- Canais sem entrada em `channels` usam os defaults de `_meta`
- `enabled: false` em um device → Delfos ignora todos os seus CSVs no próximo hot-reload
- Devices com `csv_files` idênticos causam variáveis duplicadas — **cada device deve ter CSVs exclusivos**
- **Não existe seção `channels` global** — canais são sempre definidos dentro de cada device

**Canais de sistema (`SYSTEM_CHANNELS`):** `user_status`, `ia_status`, `ia_data` — são protegidos contra remoção via API (`DELETE /api/channels/{channel}` retorna 403). O painel web marca esses canais com badge "SISTEMA" e oculta o botão de remoção. Canais derivados por device (`{device_id}_commands`, `config_reload_{device_id}`) são gerenciados automaticamente.

### `variable_overrides_{device_id}.json` — atribuição direta por tag (per-device)

**Única fonte de roteamento por device.** Cada device tem seu próprio arquivo de overrides (ex.: `variable_overrides_simulador.json`, `variable_overrides_clp2.json`). Define qual canal cada tag publica. Tags ausentes do arquivo do device não são lidas pela instância Delfos correspondente. Editável pelo painel web ou via `PATCH /api/variables/{tag}`.

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

**Regra de roteamento:**
- Tag com `channel` definido → lida e publicada no canal correspondente
- Tag sem `channel` (ausente do arquivo ou `channel` removido) → **não lida, não publicada**
- `enabled: false` → excluída mesmo com canal atribuído

**Migração:** o script `scripts/migrate_config.py` migra o antigo `variable_overrides.json` global para arquivos per-device, distribuindo as tags conforme os CSVs de cada device.

### `simulator_config.json` — configuração dos simuladores embarcados

Gerado e gerenciado pelo Hub (LabTest). Persistido em `tables/`. Cada chave é um `sim_id`.

```json
{
  "sim_clp1": {
    "label": "Simulador CLP Principal",
    "protocol": "tcp",
    "port": 5020,
    "unit_id": 1,
    "csv_files": ["mapeamento_clp.csv"],
    "simulate": true,
    "auto_start": false,
    "sim_interval": 2.0,
    "sim_registers": 8,
    "sim_coils": 12,
    "sim_coil_prob": 0.3
  }
}
```

**Campos:**
- `protocol` — `"tcp"` ou `"rtu_tcp"` (RTU framer sobre TCP)
- `port` — porta TCP do servidor Modbus
- `csv_files` — lista de CSVs em `tables/` para carregar contexto
- `simulate` — `true` = variação automática de valores; `false` = valores estáticos
- `auto_start` — `true` = inicia automaticamente no startup do Hub
- `sim_interval` — intervalo em segundos entre ciclos de simulação (default 2.0)
- `sim_registers` — quantos registers variar por ciclo (default 8, 0=todos)
- `sim_coils` — quantos coils variar por ciclo (default 12, 0=todos)
- `sim_coil_prob` — probabilidade de toggle por coil por ciclo (default 0.3)

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
| `tests/test_hub.py` | Hub — bridge, endpoints REST, upload/export, device CRUD, ping, simulators, scanner | 76 |
| `tests/test_e2e_rtu.py` | Teste end-to-end RTU over TCP (simulador + Delfos + Atena) | — |

**Total unit tests (sem deps externas):** 106 (`test_hub` + `test_segmented_reading`)

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
2. **Código duplicado parcialmente resolvido:** `modbus_functions.py` foi extraído para `shared/` com `ModbusClientWrapper` unificado; `redis_config_functions.py` ainda é duplicado em Delfos e Atena
3. **Eventos Socket.IO — nomenclatura intencional:** client→server usa underscore (`plc_write`, `sim_write`); server→client usa colon (`device:data`, `proc:status`, `sim:values`). Frontends devem seguir esta convenção.
4. **Redis sem replicação:** ponto único de falha
5. **ProcessManager sem reinício automático:** processos que crasham são detectados (`proc:status` com `exit_code`) mas não reiniciam sozinhos — o usuário precisa clicar "Iniciar" novamente. Suporta múltiplos processos do mesmo tipo para devices diferentes.

---

## O que NÃO fazer

- Não commitar arquivos `.env`
- Não hardcodar IPs, portas ou credenciais no código
- Não usar `print()` — usar `logging`
- Não modificar CSVs de mapeamento Modbus sem entender o impacto nos endereços
- Não alterar a ordem das colunas nos CSVs (a leitura depende dos nomes das colunas)
- Não adicionar o mesmo CSV a múltiplos devices (causa variáveis duplicadas nos overrides)
- Não adicionar seção `groups` nem seção `channels` global ao `group_config.json` — canais são sempre definidos dentro de cada device
