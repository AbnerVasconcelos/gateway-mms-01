# Gateway IoT Industrial — Integration Guide

This document describes how external systems (dashboards, AI/ML pipelines, SCADA, custom applications) can integrate with the Gateway IoT Industrial to read real-time Modbus data and send commands to PLCs.

## Table of Contents

1. [System Overview](#system-overview)
2. [Data Format](#data-format)
3. [Redis Interface](#redis-interface)
4. [Socket.IO Interface](#socketio-interface)
5. [REST API](#rest-api)
6. [Writing Commands](#writing-commands)
7. [Grafana Integration](#grafana-integration)
8. [Client Examples](#client-examples)

---

## System Overview

```
PLC (Modbus TCP/RTU)            [multiple devices]
    ^|
  Delfos  (reader)  -->  Redis pub/sub + history
                              |
                            Hub  (FastAPI + Socket.IO, port 4567)
                              |
                         Browser / External Systems / AI
  Atena   (writer)  <--  Redis {device_id}_commands  <--  [Hub / External]
```

**Components:**

- **Delfos** — Reads coils and holding registers from Modbus devices, publishes structured JSON to Redis channels. One instance per device.
- **Atena** — Listens for commands on Redis, writes values to the PLC. One instance per device.
- **Hub** — FastAPI + Socket.IO server on port 4567. Bridges Redis to WebSocket, serves REST API, manages processes and simulators.
- **Redis** — Message broker (pub/sub) and data store (history lists, last-message snapshots).

**Devices** are configured in `tables/group_config.json`. Each device has its own set of channels, CSV mappings, and Modbus connection parameters.

---

## Data Format

Every message published by Delfos follows this canonical structure:

```json
{
  "coils": {
    "group_name": {
      "tag1": true,
      "tag2": false
    }
  },
  "registers": {
    "group_name": {
      "tag3": 1450,
      "tag4": 200
    }
  },
  "timestamp": "2026-01-15T14:23:45.123456"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `coils` | `{group: {tag: bool}}` | Digital I/O values (on/off) |
| `registers` | `{group: {tag: number}}` | Analog values (holding registers) |
| `timestamp` | `string` | ISO 8601 timestamp when the read was performed |

**Groups** are logical namespaces (e.g., `alarmes`, `controle_extrusora`, `producao`) defined in the CSV mapping files.

**Tags** are individual variable names (e.g., `extrusoraLigadoDesligado`, `extrusoraFeedBackSpeed`).

---

## Redis Interface

Redis is the primary data transport. External systems can connect directly to Redis for real-time or historical data.

Default: `localhost:6379`, database 0.

### Pub/Sub Channels (Real-Time)

Subscribe to channels to receive messages as they are published by Delfos.

| Channel Pattern | Description | Example |
|----------------|-------------|---------|
| `plc_*` | Device data channels | `plc_alarmes`, `plc_operacao`, `plc_west1Temperatura` |
| `{device_id}_commands` | Write commands for Atena | `simulador_commands` |
| `user_status` | User state (enable/disable system) | `{"user_state": true}` |
| `config_reload_{device_id}` | Configuration reload signal | `{"reload": true}` |

**Message format:** JSON string matching the [Data Format](#data-format) above.

```python
# Subscribe example
import redis
r = redis.Redis(host='localhost', port=6379)
ps = r.pubsub()
ps.subscribe('plc_alarmes', 'plc_operacao')
for msg in ps.listen():
    if msg['type'] == 'message':
        data = json.loads(msg['data'])
        print(data['timestamp'], data['registers'])
```

### Last Message Snapshot

Each channel's most recent message is stored as a Redis string key.

```
GET last_message:plc_alarmes
```

Returns the JSON string of the last published message on that channel.

### History Lists

Each channel maintains a bounded history list (newest first).

```
LRANGE history:plc_alarmes 0 99    # last 100 messages
LINDEX history:plc_alarmes 0       # most recent message
LLEN   history:plc_alarmes         # total items in history
```

History size is configurable per channel via `group_config.json` or the REST API. Default: 100.

### Channel Discovery

To discover active channels programmatically:

```python
# Via REST API (recommended)
import requests
channels = requests.get('http://localhost:4567/api/channels').json()
# Returns: {"plc_alarmes": {"delay_ms": 1000, "history_size": 100, "device_id": "simulador"}, ...}
```

---

## Socket.IO Interface

The Hub provides a Socket.IO server for real-time WebSocket communication.

### Connection

```
URL: http://<hub_host>:4567
Transport: websocket (preferred), polling (fallback)
```

### Room Structure

After connecting, join rooms to receive specific data:

| Room | Data Received |
|------|---------------|
| `{device_id}` | All channels for the device |
| `{device_id}:{channel}` | Specific channel only |
| `sim:{sim_id}` | Simulator values (LabTest) |
| `scan:{device_id}` | Scanner progress events |

### Client-to-Server Events

| Event | Payload | Description |
|-------|---------|-------------|
| `join` | `{"rooms": ["simulador", "simulador:plc_alarmes"]}` | Join rooms |
| `plc_write` | `{"group": {"tag": value}}` | Write command to PLC |
| `user_status` | `{"user_state": true}` | Enable/disable the system |
| `config_save` | `{...group_config...}` | Save configuration |
| `config_get` | `{}` | Request current config |
| `history_set` | `{"channel": "plc_alarmes", "size": 50}` | Set history size |
| `history_get` | `{}` | Request history sizes |
| `sim_subscribe` | `{"sim_id": "sim_clp1"}` | Subscribe to simulator data |
| `sim_write` | `{"sim_id": "...", "tag": "...", "value": 42}` | Write to simulator |
| `sim_lock` | `{"sim_id": "...", "tag": "...", "locked": true}` | Lock simulator variable |
| `scan_subscribe` | `{"device_id": "simulador"}` | Subscribe to scan progress |

### Server-to-Client Events

| Event | Payload | Description |
|-------|---------|-------------|
| `device:data` | `{"device_id": "sim", "channel": "plc_alarmes", "data": {...}}` | Data from any device channel |
| `channel:data` | `{coils: {...}, registers: {...}, timestamp: "..."}` | Data for specific channel room |
| `connection_ack` | `{"status": "connected", "available_rooms": [...]}` | Connection confirmation |
| `config:updated` | `{...group_config...}` | Configuration changed |
| `history:sizes` | `{"plc_alarmes": 100, ...}` | History sizes response |
| `proc:status` | `{"proc_id": "delfos:sim", "running": true, ...}` | Process state change |
| `sim:status` | `{"sim_id": "...", "running": true}` | Simulator state change |
| `sim:values` | `{"sim_id": "...", "values": {...}, "timestamp": "..."}` | Simulator periodic values |
| `scan:variable` | `{"tag": "...", "status": "ok", "value": 42, ...}` | Scan progress per variable |
| `scan:complete` | `{"device_id": "...", "status": "completed", ...}` | Scan finished |

**Naming convention:** client-to-server events use underscore (`plc_write`, `sim_write`); server-to-client events use colon (`device:data`, `proc:status`).

---

## REST API

The Hub exposes REST endpoints for polling, configuration, and history access.

Base URL: `http://<hub_host>:4567`

### Device Discovery

```
GET /api/devices
```
Returns `{device_id: {label, protocol, host, port, unit_id, csv_files, channels, enabled, ...}}`.

### Channel Listing

```
GET /api/channels
```
Returns `{channel_name: {delay_ms, history_size, enabled, device_id}}` for all channels.

### Variable Listing

```
GET /api/variables
```
Returns `{variables: [...], overrides: {...}}`.

Each variable object:
```json
{
  "tag": "extrusoraFeedBackSpeed",
  "group": "controle_extrusora",
  "type": "%MW",
  "address": 100,
  "address_raw": "100",
  "bit_index": null,
  "channel": "plc_operacao",
  "history_size": 100,
  "enabled": true,
  "source": "mapeamento_clp",
  "device": "simulador",
  "classe": null
}
```

### Channel History

```
GET /api/channels/{channel}/history?limit=100
```
Returns `{channel, count, items: [...]}`. Items are newest-first (matching Redis LRANGE order).

### Process Status

```
GET /api/processes
```
Returns running Delfos/Atena processes with state.

### Health Check

```
GET /health
```
Returns `{"status": "ok"}`.

---

## Writing Commands

To send write commands to the PLC, the system must have `user_state = true`.

### Via Socket.IO

```javascript
socket.emit('user_status', { user_state: true });
socket.emit('plc_write', {
  "controle_extrusora": {
    "extrusoraRefVelocidade": 1450
  }
});
```

### Via Redis

```python
import redis, json
r = redis.Redis()
# Enable user state first
r.publish('user_status', json.dumps({"user_state": True}))
# Send command to specific device
r.publish('simulador_commands', json.dumps({
    "controle_extrusora": {
        "extrusoraRefVelocidade": 1450
    }
}))
```

**Safety:** Atena only writes to the PLC when `user_state` is `True`. Commands sent while `user_state` is `False` are silently ignored.

---

## Grafana Integration

The Hub includes a Grafana SimpleJSON-compatible API at `/api/grafana/`.

See [grafana-setup.md](grafana-setup.md) for detailed setup instructions.

**Quick overview:**

```
GET  /api/grafana/         — Health check (datasource test)
POST /api/grafana/search   — Available metrics list
POST /api/grafana/query    — Time-series or table data
```

Metric naming: `{device_id}.{channel}.{group}.{tag}`

---

## Client Examples

See the `docs/examples/` directory for ready-to-use code samples:

| File | Language | Description |
|------|----------|-------------|
| `python_socketio_client.py` | Python | Real-time Socket.IO subscriber |
| `python_redis_subscriber.py` | Python | Direct Redis pub/sub consumer |
| `javascript_socketio_client.js` | JavaScript | Browser/Node.js Socket.IO client |
| `python_history_reader.py` | Python | REST API history polling |
| `python_grafana_query.py` | Python | Grafana API programmatic queries |
