# CLAUDE.md — Raspberry Pi Gateway IoT Industrial (Maq01)

Documentacao de contexto do Raspberry Pi `delfos@192.168.196.46`, usado como gateway IoT industrial para a linha de extrusao MMS.

---

## Visao Geral

O Raspberry Pi funciona como **gateway Modbus → Redis → WebSocket**, conectando equipamentos industriais (CLPs e controladores de temperatura) a interfaces web, dashboards e sistemas de IA.

```
Equipamentos Industriais (Modbus TCP/RTU/RS485)
    |
    +-- CLP WEG PLC300 ---- Modbus TCP (192.168.0.10:502, unit_id=255)
    |
    +-- West 24 Zonas ------ RS485 Sniff passivo (/dev/ttyUSB0, 19200,N,8,1, slave=1)
    |
    +-- West 28 Zonas ------ RS485 Sniff passivo (/dev/ttyUSB0, 19200,N,8,1, slave=20)
    |
    v
  Raspberry Pi (192.168.196.46)
    |
    +-- Delfos (leitura) --> Redis (pub/sub por canal) --> Hub (FastAPI + Socket.IO :4567) --> Browser/IA
    +-- Atena  (escrita) <-- Redis ({device}_commands)  <-- Hub/IA
    |
    +-- Cloudflare Tunnel (acesso externo)
```

---

## Hardware e Sistema

| Item | Detalhe |
|------|---------|
| **Dispositivo** | Raspberry Pi (aarch64) |
| **OS** | Debian GNU/Linux, kernel 6.12.47+rpt-rpi-v8 |
| **Usuario** | `delfos` |
| **IP na rede** | `192.168.196.46` |
| **Interface serial** | `/dev/ttyUSB0` (conversor USB-RS485) |
| **Projeto principal** | `/home/delfos/gateway-modbus` |
| **Python** | 3.13 (venv em `/home/delfos/gateway-modbus/.venv`) |

---

## Servicos Ativos (systemd)

| Servico | Tipo | Descricao |
|---------|------|-----------|
| `redis-server.service` | system | Redis 6379 — broker pub/sub central (AOF habilitado) |
| `gateway-hub.service` | system | Hub FastAPI + Socket.IO :4567 — auto-start de Delfos/Atena por device |
| `cloudflared.service` | system | Cloudflare Tunnel — acesso remoto seguro |
| `rpi-connect.service` | user | Raspberry Pi Connect — VNC remoto |

> **Nota:** O Hub sobe automaticamente via systemd e inicia Delfos/Atena para cada device com `enabled=true` (exceto devices com `managed_by`). Processos que crasham sao reiniciados automaticamente com backoff exponencial (5s-80s, max 5 tentativas). O watchdog systemd (60s) reinicia o Hub se Redis ficar inacessivel.

---

## Arquitetura do Projeto (`gateway-modbus`)

### Processos Principais

| Processo | Entry Point | Funcao |
|----------|-------------|--------|
| **Delfos** | `Delfos/delfos.py` | Le coils/registers via Modbus, publica no Redis por canal. Loop 50ms com timer por canal. Heartbeat em `heartbeat:{device_id}:delfos` (TTL 30s). |
| **Atena** | `Atena/atena.py` | Escuta comandos no Redis, escreve no CLP. So escreve se `user_state=True`. Heartbeat em `heartbeat:{device_id}:atena` (TTL 30s). |
| **Hub** | `Hub/main.py` | Bridge Redis<->WebSocket + API REST + painel web. `uvicorn Hub.main:asgi_app --port 4567`. Auto-start de devices no boot, auto-restart de processos crashados, watchdog systemd. |

### Modulos Compartilhados (`shared/`)

| Arquivo | Funcao |
|---------|--------|
| `modbus_functions.py` | `ModbusClientWrapper` unifica 4 protocolos: TCP (`pyModbusTCP`), RTU over TCP (`pymodbus`), RTU serial (`RawRtuClient`), Sniff passivo (`SnifferClient`) |
| `redis_config_functions.py` | Setup Redis, pub/sub helpers, persistencia de historico |
| `bit_addressing.py` | Parsing de enderecos com bit (ex: `1584.01` = register 1584, bit 1) |

### Hub — Submodulos

| Arquivo | Funcao |
|---------|--------|
| `Hub/main.py` | FastAPI + Socket.IO ASGI app, endpoints REST, startup |
| `Hub/redis_bridge.py` | Background task: Redis psubscribe → sio.emit por room |
| `Hub/config_store.py` | CRUD de group_config.json e variable_overrides |
| `Hub/process_manager.py` | Gerencia subprocessos Delfos/Atena (start/stop/status) |
| `Hub/simulator_manager.py` | Gerencia simuladores Modbus embarcados |
| `Hub/scanner_manager.py` | Scanner de registradores Modbus (descoberta) |
| `Hub/grafana_api.py` | Integracao com Grafana para dashboards |

---

## Devices Configurados (`tables/group_config.json`)

### 1. West 24 Zonas (`west_24z`)

| Propriedade | Valor |
|-------------|-------|
| **Protocolo** | `sniff` (RS485 passivo hibrido) |
| **Porta serial** | `/dev/ttyUSB0` |
| **Baudrate** | 19200, N, 8, 1 |
| **Slave ID** | 1 |
| **CSVs** | `temperatura_24z.csv`, `temperatura_28z.csv` |
| **Canais** | `plc_west1Temperatura`, `plc_west1LigDeslZona`, `plc_west1Alertas`, `west1Corrente`, `plc_west1SetPoint`, `plc_west2SetPoint`, `plc_west2pressoes`, `plc_west2alertas`, `plc_west2ligDeslZona`, `plc_west2corrente`, `plc_wes2temperatura` |

Controlador de temperatura West com 24 zonas. Registradores Modbus:
- **PV (Temperaturas):** `0x0600` — 24 registradores (scale 0.1 = graus C)
- **SP (Setpoints):** `0x0700` — 24 registradores (scale 0.1 = graus C)
- **Flag:** `0x0730`

### 2. West 28 Zonas (`west_28z`)

| Propriedade | Valor |
|-------------|-------|
| **Protocolo** | `sniff` (RS485 passivo hibrido) |
| **Porta serial** | `/dev/ttyUSB0` (mesma porta, barramento compartilhado) |
| **Slave ID** | 20 |
| **Canais** | `plc_west2SetPoint`, `plc_west2pressoes`, `plc_west2alertas`, `plc_west2ligDeslZona`, `plc_west2corrente`, `plc_wes2temperatura` |
| **Managed by** | `west_24z` (processo unico le ambos os slaves) |

### 3. CLP PLC300 WEG (`plc300`)

| Propriedade | Valor |
|-------------|-------|
| **Protocolo** | `tcp` (Modbus TCP) |
| **Host** | `192.168.0.10` |
| **Porta** | 502 |
| **Unit ID** | 255 |
| **CSV** | `mapeamento_clp.csv` |
| **Canais** | `plc_retentivas` (100s), `plc_alarmes` (180s), `plc_operacao` (5s), `plc_redeCan` (180s), `plc_preArraste` (6s), `plc_io` (10s), `plc_producao` (10s) |

CLP WEG PLC300 que controla a linha de extrusao (extrusora, puxador, dosador, alimentador, producao).

---

## Protocolo Sniff — RS485 Passivo Hibrido

O barramento RS485 ja possui um mestre (HMI/supervisorio). O gateway opera em modo **passivo hibrido**:

1. **Thread listener (daemon):** captura trafego RS485 existente, decodifica frames FC01/FC03, armazena em cache thread-safe
2. **Leitura:** cache hit → retorna do cache; cache miss → leitura ativa via `RawRtuClient` (fallback)
3. **Escrita:** sempre ativa via `RawRtuClient` com `wait_for_silence` para evitar colisao

### Cache do Sniffer
- Chave: `(tipo, slave, endereco)` — tipo = `'reg'` ou `'coil'`
- Valor: `{value, timestamp}` (monotonic clock)
- `stale_timeout`: 10s (dado obsoleto → fallback para leitura ativa)
- Thread-safe via `threading.Lock`

---

## Canais Redis

| Canal | Direcao | Funcao |
|-------|---------|--------|
| `plc_west1Temperatura` | Delfos → Hub | Temperaturas PV das 24 zonas West |
| `plc_west1SetPoint` | Delfos → Hub | Setpoints e correntes baixas |
| `plc_operacao` | Delfos → Hub | Dados operacionais do CLP |
| `plc_alarmes` | Delfos → Hub | Alarmes do CLP |
| `{device_id}_commands` | Hub → Atena | Comandos de escrita |
| `user_status` | Hub → Delfos/Atena | Estado de conexao do usuario |
| `config_reload_{device_id}` | Hub → Delfos | Hot-reload de configuracao |
| `_bridge_reload` | Hub interno | Recalcula subscricoes da bridge |
| `last_message:{channel}` | Delfos | Ultimo valor publicado |
| `history:{channel}` | Delfos | Historico de publicacoes |
| `heartbeat:{device_id}:delfos` | Delfos | Heartbeat com TTL 30s (ISO 8601) |
| `heartbeat:{device_id}:atena` | Atena | Heartbeat com TTL 30s (ISO 8601) |

---

## Configuracao (`tables/`)

| Arquivo | Funcao |
|---------|--------|
| `group_config.json` | Devices, canais Redis, delays, CSVs por device |
| `variable_overrides_{device_id}.json` | Roteamento de tags para canais, enable/disable |
| `simulator_config.json` | Config de simuladores Modbus embarcados |
| `*.csv` | Mapeamento de tags Modbus (key, ObjecTag, Tipo, Modbus, At, Classe) |

### Regra de roteamento de tags
- `variable_overrides[tag].channel` define o canal Redis de destino
- Tag sem `channel` explicito → **nao e lida** (ignorada)
- Tag com `enabled: false` → excluida
- Scale factors nos CSVs (coluna `Scale`) aplicados antes de publicar

---

## Portas e Servicos

| Servico | Host | Porta |
|---------|------|-------|
| Hub (FastAPI + Socket.IO) | `0.0.0.0` | `4567` |
| Simulador PLC300 | `0.0.0.0` | `5020` |
| Simulador West 24z | `0.0.0.0` | `5021` |
| Simulador West 28z | `0.0.0.0` | `5022` |
| Redis | `localhost` | `6379` |
| CLP PLC300 (externo) | `192.168.0.10` | `502` |

---

## Como Executar

O gateway sobe automaticamente no boot via systemd. Para operacoes manuais:

```bash
# Verificar status
sudo systemctl status gateway-hub

# Reiniciar Hub (mata e reinicia Hub + todos os Delfos/Atena)
sudo systemctl restart gateway-hub

# Logs em tempo real
journalctl -u gateway-hub -f

# Health check (deve retornar status de cada instancia)
curl http://localhost:4567/health

# Execucao manual (debug — parar o service antes):
sudo systemctl stop gateway-hub
cd /home/delfos/gateway-modbus
source .venv/bin/activate
uvicorn Hub.main:asgi_app --host 0.0.0.0 --port 4567
```

---

## Scripts Avulsos na Home

Scripts de diagnostico e desenvolvimento (nao fazem parte do gateway):

| Script | Funcao |
|--------|--------|
| `west_monitor.py` | Monitor passivo RS485 — decodifica frames sem transmitir |
| `west_read.py` | Leitura ativa Modbus RTU do West (slave 1) |
| `west_test.py` | Teste basico com `minimalmodbus` |
| `read_west.py` | Leitura usando `shared.modbus_functions` |
| `read_west24.py` | Leitura das 24 zonas em blocos de 8 |
| `read_west24v2.py` | Leitura das 24 zonas em bloco unico |
| `read_full24.py` | Leitura completa com retry e fallback |
| `probe_west.py` | Probe de diferentes tamanhos de bloco |
| `probe_limit.py` | Teste de limites de leitura |
| `west_monitor.py` | Sniffer passivo do barramento RS485 |
| `patch_*.py` | Scripts de patch aplicados ao projeto (historico) |
| `apply_sniffer.py` | Script que adicionou o SnifferClient ao modbus_functions |

---

## Dependencias Principais

| Pacote | Uso |
|--------|-----|
| `pyModbusTCP` 0.2.1 | Cliente Modbus TCP (CLP PLC300) |
| `pymodbus` 3.6.4 | RTU over TCP |
| `pyserial` | Comunicacao serial RS485 |
| `redis` 5.0.3 | Pub/sub e store |
| `fastapi` | API REST do Hub |
| `python-socketio` | WebSocket real-time |
| `uvicorn` | ASGI server |
| `pandas` 3.0.1 | Leitura de CSVs |
| `openpyxl` | Import/export Excel |

---

## Padroes e Regras

- **Logging:** `logging` (nunca `print()`). Formato: `%(asctime)s [%(name)s] %(levelname)s: %(message)s`
- **Retry:** 3 tentativas em I/O externo
- **Env vars:** nunca hardcodar IPs/portas — sempre `os.environ.get()`
- **Timestamps:** ISO 8601
- **CSVs exclusivos por device**
- **Atena so escreve se `user_state=True`**
- **DEVICE_ID obrigatorio** para Delfos/Atena
- **Acesso remoto:** via SSH (`ssh delfos@192.168.196.46`) ou Cloudflare Tunnel

---

## Problemas Conhecidos

- `handle_ia_data_message` e stub (IA nao implementada)
- Redis sem replicacao (ponto unico de falha, mas AOF habilitado para persistencia)

---

## Contexto Industrial

Este gateway serve uma **linha de extrusao** da empresa **MMS**, monitorando:
- **Temperaturas de 24+28 zonas** via controladores West (RS485)
- **Operacao da extrusora** (velocidade, corrente, alarmes) via CLP WEG PLC300 (Modbus TCP)
- **Producao** (totalizadores, dosador, alimentador, puxador)
- **Alarmes e estados** de todo o processo

O painel web em `:4567` permite visualizacao em tempo real e configuracao remota dos canais de dados.

---

## SnifferClient — Last-Known Cache e Diagnostico (2026-03-25)

### Problema Resolvido

O SnifferClient preenchia posicoes faltantes com `0` quando a leitura ativa falhava e o cache estava stale. Isso corrompia ~5.5% dos dados de temperatura no InfluxDB.

### Solucao: `_last_known` cache

Adicionado dict `_last_known` ao `SnifferClient` (`shared/modbus_functions.py`) que **nunca expira**. Quando o cache normal esta stale e a leitura ativa falha, usa o ultimo valor real observado no barramento ao inves de `0`.

- `_cache` → expira apos `stale_timeout` (30s) → dispara leitura ativa
- `_last_known` → nunca expira → fallback quando leitura ativa falha

### Diagnostico de Falhas

Adicionado sistema de stats por endereco no `SnifferClient`:
- `stale_hits`: vezes que usou valor stale do `_last_known`
- `active_fails`: vezes que leitura ativa falhou
- `cache_misses`: enderecos nunca vistos no barramento

Stats sao logados a cada 60s (ranking dos piores enderecos) e persistidos no Redis:
```bash
redis-cli GET sniffer:stats:west_24z | jq
```

### Split de Canais de Temperatura (2026-03-25)

Canal `plc_west1Temperatura` (24 zonas, slave 1) dividido em 4 canais de 6 zonas:
- `plc_west1Temp_z1_6` → zonas 1-6
- `plc_west1Temp_z7_12` → zonas 7-12
- `plc_west1Temp_z13_18` → zonas 13-18
- `plc_west1Temp_z19_24` → zonas 19-24

Objetivo: reduzir leituras parciais — blocos menores tem maior chance de estar completos no cache do sniffer.

Arquivos alterados:
- `tables/variable_overrides_west_24z.json` — reatribuicao de tags para novos canais
- `tables/group_config.json` — novos canais com `delay_ms: 10000`
- `shared/modbus_functions.py` — last_known cache + stats
- `Delfos/delfos.py` — passa Redis client para SnifferClient

---

## Fix: Redis Falha no Boot — Race Condition de Rede (2026-03-27)

### Problema

Redis configurado com `bind 127.0.0.1 192.168.196.46` falhava no boot do Raspberry Pi:

```
Could not create server TCP listening socket 192.168.196.46:6379: bind: Cannot assign requested address
Failed listening on port 6379 (tcp), aborting.
```

**Causa raiz:** O servico `redis-server.service` iniciava antes da interface de rede receber o IP `192.168.196.46`. O `After=network.target` padrao do Redis so garante que o subsistema de rede foi *carregado*, nao que os IPs foram *atribuidos*.

Redis crashava 5x em sequencia rapida → systemd atingia `StartLimitBurst` → servico marcado como `failed`. Como `gateway-hub.service` tem `Requires=redis-server.service`, o Hub tambem nao subia. Resultado: gateway inteiro offline apos reboot.

### Solucao

Criado override systemd em `/etc/systemd/system/redis-server.service.d/wait-network.conf`:

```ini
[Unit]
After=network-online.target
Wants=network-online.target

[Service]
RestartSec=3
StartLimitIntervalSec=60
StartLimitBurst=10
```

- `After=network-online.target` — espera o `NetworkManager-wait-online.service` confirmar que os IPs estao atribuidos
- `Wants=network-online.target` — puxa o target como dependencia
- `RestartSec=3` — intervalo entre retentativas (padrao era 100ms, causava burst rapido)
- `StartLimitBurst=10` dentro de `StartLimitIntervalSec=60` — mais tolerante a atrasos de rede

### Cadeia de Dependencias (boot)

```
NetworkManager-wait-online.service
  → network-online.target
    → redis-server.service (bind 127.0.0.1 + 192.168.196.46)
      → gateway-hub.service (FastAPI + Socket.IO :4567)
        → auto-start Delfos + Atena por device
```

### Verificacao

```bash
# Confirmar override aplicado
systemctl cat redis-server.service | grep -A3 wait-network

# Apos reboot, verificar cadeia
systemctl is-active redis-server gateway-hub
redis-cli -h 192.168.196.46 ping
curl -s http://localhost:4567/health | python3 -m json.tool | head 5
```
