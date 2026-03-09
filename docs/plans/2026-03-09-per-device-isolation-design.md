# Design: Isolamento por Device (Per-Device Architecture)

**Data:** 2026-03-09
**Status:** Aprovado

## Contexto

A arquitetura atual usa processos Delfos/Atena compartilhados e canais Redis globais para todos os devices. Isso causa acoplamento entre devices (crash de um afeta todos, protocolos diferentes competem pelo mesmo loop) e dificulta rastreamento de dados por device.

## Decisao

Isolamento total por device: cada device possui seus proprios processos Delfos/Atena, canais Redis dedicados, e overrides independentes.

## Arquitetura

### group_config.json — nova estrutura

Canais movidos de nivel global para dentro de cada device. Campo `command_channel` adicionado.

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
      "enabled": true,
      "channels": {
        "sim_alarmes": { "delay_ms": 200, "history_size": 55 },
        "sim_process": { "delay_ms": 500, "history_size": 100 }
      },
      "command_channel": "sim_commands"
    }
  }
}
```

Removidos de `_meta`: `aggregate_channel`, `backward_compatible`.
Removida secao `channels` global.

### Overrides per-device

- `variable_overrides_{device_id}.json` por device
- Formato interno identico: `{"tag": {"channel": "...", "enabled": true}}`
- Canal atribuido deve existir nos `channels` do device

### System channels

- `{command_channel}` — canal de escrita do Atena (definido no device)
- `user_status` — global (cross-device)
- `config_reload_{device_id}` — hot-reload per-device

## ProcessManager

### Modelo de processos

proc_id composto: `{proc_type}:{device_id}` (ex: `delfos:simulador`, `atena:west`).

Cada device pode ter ate 2 processos (Delfos + Atena).

### Env vars

Novas variaveis passadas aos processos:
- `DEVICE_ID` — identificador do device
- `COMMAND_CHANNEL` — canal que Atena escuta
- `CONFIG_RELOAD_CHANNEL` — canal de hot-reload

### REST API

```
POST   /api/processes/{device_id}/{proc_type}/start
POST   /api/processes/{device_id}/{proc_type}/stop
GET    /api/processes/{device_id}/{proc_type}/logs
GET    /api/processes
```

### Ciclo de vida

- Start device: Hub inicia Delfos + Atena
- Stop device: Hub para ambos
- Toggle device: `enabled: false` para processos
- Crash: `_watch_exit()` independente, emite `proc:status` com `device_id`

## Delfos — mudancas

- Le `DEVICE_ID` do env
- Carrega apenas CSVs do seu device
- Le canais de `device_cfg['channels']`
- Carrega overrides de `variable_overrides_{device_id}.json`
- Escuta `config_reload_{device_id}` em vez de `config_reload`
- Publica apenas nos canais do seu device
- Nao publica em `plc_data` (removido)

## Atena — mudancas

- Le `DEVICE_ID` do env
- Carrega CSVs do device (nao mais hardcoded `operacao.csv`)
- Subscribe em `COMMAND_CHANNEL`, `user_status`, `ia_status`, `ia_data`
- Lookup de tags nos CSVs do device

## Redis Bridge e Socket.IO

### Bridge

Subscribe dinamico nos canais de todos os devices (nao mais `psubscribe('plc_*')`).
Re-subscribe quando device e criado/removido.

### Rooms

- `{device_id}` — todas as mensagens do device
- `{device_id}:{canal}` — canal especifico

### Eventos

Server -> Client:
- `device:data` — `{device_id, channel, data}` (room: device_id)
- `channel:data` — `data` (room: device_id:canal)
- `proc:status` — `{device_id, proc_type, running, ...}`

Client -> Server:
- `join` — `{device_id}` ou `{device_id, channel}`
- `leave` — `{device_id}`
- `plc_write` — `{device_id, tag, value}` (Hub roteia para command_channel)

## Frontend

### Layout

Tab bar abaixo da navbar: uma tab por device + tab Config.
Indicador de status na tab (verde = Delfos rodando).

### Sidebar contextual

- Processos: Delfos/Atena do device (sem dropdown)
- Canais: apenas canais do device
- Device Info: host, port, protocol, ping, edit

### Grid

Mostra apenas variaveis dos CSVs do device selecionado.
Overrides do device correspondente.

### Tab Config

CRUD de devices: criar, editar, remover, upload CSV.

## Migracao

### group_config.json

Se existir `channels` no nivel raiz + devices sem `channels`: migrar automaticamente.

### variable_overrides.json

Particionar por device: ler CSVs de cada device, mover tags para arquivo correspondente.

## O que NAO muda

- `shared/modbus_functions.py` — ModbusClientWrapper intacto
- Formato de mensagem: `{coils, registers, timestamp}`
- CSVs de mapeamento Modbus — formato das colunas
- Simulator manager e LabTest
- Monitor page
