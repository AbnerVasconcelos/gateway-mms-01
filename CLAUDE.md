# CLAUDE.md — Gateway IoT Industrial

Gateway Modbus TCP/RTU → Redis pub/sub → WebSocket. Mapeamento de tags definido por CSVs em `tables/`.

```
CLP (Modbus TCP/RTU)  [múltiplos devices]
    ↑↓
  Delfos (leitura)  →  Redis (canais por device)  →  Hub (FastAPI + Socket.IO)  →  Browser/IA
  Atena  (escrita)  ←  Redis ({device_id}_commands)  ←  Hub/IA
```

---

## Arquitetura

| Processo | Entry point | Função |
|----------|-------------|--------|
| **Delfos** | `Delfos/delfos.py` | Lê coils/registers do CLP, publica no Redis por canal. Loop 50ms, timer por canal. |
| **Atena** | `Atena/atena.py` | Escuta Redis, escreve no CLP. Só escreve se `user_state=True`. |
| **Hub** | `Hub/main.py` | Bridge Redis↔WebSocket + API REST + painel web. `uvicorn Hub.main:asgi_app --port 4567` |

Cada instância Delfos/Atena opera sobre **um único device** (`DEVICE_ID` obrigatório). Canais são definidos dentro de cada device em `group_config.json`.

**Módulos compartilhados (`shared/`):**
- `modbus_functions.py` — `ModbusClientWrapper` unifica `pyModbusTCP` (TCP) e `pymodbus` (RTU over TCP)
- `redis_config_functions.py` — setup Redis, pub/sub helpers
- `bit_addressing.py` — parsing de endereços com bit (ex: `1584.01` = register 1584, bit 1)

---

## Arquivos de configuração (`tables/`)

**`group_config.json`** — devices com canais Redis por device. Canais são **sempre** dentro de cada device (não existe seção global). Cada device deve ter CSVs exclusivos.

**`variable_overrides_{device_id}.json`** — única fonte de roteamento. Tag com `channel` → lida e publicada. Sem `channel` → ignorada. `enabled: false` → excluída.

**`simulator_config.json`** — simuladores Modbus embarcados (LabTest, gerenciado pelo Hub).

**CSVs Modbus** — colunas: `key`, `ObjecTag`, `Tipo` (M=coil, D=register), `Modbus` (endereço), `At` (%MB/%MW), `Classe`.

---

## Bit Addressing

Endereços com sufixo decimal = bits individuais em holding registers de 16 bits:
- `1584` → register inteiro
- `1584.01` → register 1584, bit 1
- `1584.1` → register 1584, bit **10** (caso legado: 1 dígito × 10)
- 2 dígitos = literal, 1 dígito = ×10. Range: 0-15.

Afeta: `temperatura_24z.csv`, `temperatura_28z.csv`. Demais CSVs são backward compatible.

---

## Canais Redis

- `plc_*` — Delfos → Hub (dados do CLP, configuráveis por device)
- `{device_id}_commands` — Hub → Atena (escritas no CLP)
- `user_status` / `ia_status` / `ia_data` — canais de sistema (protegidos contra remoção)
- `config_reload_{device_id}` — hot-reload de config no Delfos
- `_bridge_reload` — recalcula subscrições da bridge
- `last_message:{channel}` / `history:{channel}` — persistência Redis (Delfos)

**Socket.IO:** client→server usa underscore (`plc_write`), server→client usa colon (`device:data`).

---

## Como executar

```bash
# 1. Setup
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp Delfos/.env.example Delfos/.env && cp Atena/.env.example Atena/.env && cp Hub/.env.example Hub/.env

# 2. Redis
docker run -d -p 6379:6379 --name redis redis:alpine

# 3. Hub (painel em http://localhost:4567)
uvicorn Hub.main:asgi_app --host 0.0.0.0 --port 4567

# 4. Delfos/Atena — via painel web (recomendado) ou:
cd Delfos && python delfos.py   # Terminal 2
cd Atena && python atena.py     # Terminal 3
```

---

## Testes

```bash
# Unit tests (sem deps externas):
python -m pytest tests/test_hub.py tests/test_segmented_reading.py tests/test_bit_addressing.py tests/test_grafana_api.py -v

# Todos (requer Redis):
python -m pytest tests/ -v
```

---

## Padrões e regras

- **Logging:** `logging` (nunca `print()`). Formato: `%(asctime)s [%(name)s] %(levelname)s: %(message)s`
- **Retry:** 3 tentativas em I/O externo
- **Env vars:** nunca hardcodar IPs/portas/credenciais — sempre `os.environ.get()`
- **Timestamps:** ISO 8601
- **`.env` nunca no git** — `.env.example` sim
- **CSVs exclusivos por device** — não compartilhar entre devices
- **Não adicionar seção `channels` global** ao `group_config.json`
- **Atena só escreve se `user_state=True`**

## Problemas conhecidos

- `handle_ia_data_message` é stub (IA não implementada)
- Redis sem replicação (ponto único de falha)
- ProcessManager sem reinício automático de processos crashados
