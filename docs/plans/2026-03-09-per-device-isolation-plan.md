# Per-Device Isolation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Isolate each Modbus device with its own Delfos/Atena processes, Redis channels, and variable overrides.

**Architecture:** Each device defined in `group_config.json` becomes a fully autonomous unit: channels move inside the device config, overrides are stored per-device, ProcessManager manages N Delfos + N Atena processes keyed by `{proc_type}:{device_id}`, and the frontend uses tabs to switch device context.

**Tech Stack:** Python 3, FastAPI, python-socketio, redis.asyncio, pyModbusTCP, pymodbus, AG Grid, Bootstrap 5.3

---

### Task 1: Migrate `config_store.py` — per-device channels and overrides

**Files:**
- Modify: `Hub/config_store.py`
- Test: `tests/test_hub.py`

This is the foundation — all other changes depend on these functions.

**Step 1: Write failing tests for new config_store functions**

Add to `tests/test_hub.py` (or a new section). Tests should cover:

```python
def test_get_channels_from_devices():
    """get_channels() reads channels from inside each device, not global section."""
    # Setup: group_config with channels inside devices, no global channels
    cfg = {
        "_meta": {"default_delay_ms": 1000, "default_history_size": 100},
        "devices": {
            "sim": {
                "label": "Sim", "protocol": "tcp", "host": "localhost",
                "port": 5020, "unit_id": 1, "csv_files": [],
                "channels": {"sim_alarm": {"delay_ms": 200, "history_size": 50}},
                "command_channel": "sim_cmd"
            }
        }
    }
    # Write config, call get_channels(), assert returns {"sim_alarm": {...}}

def test_get_device_channels():
    """New function: get_device_channels(device_id) returns only that device's channels."""

def test_overrides_per_device_load():
    """load_overrides(device_id) loads variable_overrides_{device_id}.json"""

def test_overrides_per_device_save():
    """save_overrides(overrides, device_id) saves to variable_overrides_{device_id}.json"""

def test_patch_variable_override_per_device():
    """patch_variable_override(tag, fields, device_id) writes to per-device file."""

def test_load_all_variables_per_device():
    """load_all_variables() reads per-device overrides for each device."""

def test_get_all_channels_aggregated():
    """get_channels() aggregates channels from ALL devices."""

def test_create_channel_in_device():
    """create_channel(channel, delay_ms, history_size, device_id) adds to device.channels."""

def test_delete_channel_from_device():
    """delete_channel(channel, device_id) removes from device.channels."""
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hub.py -k "test_get_channels_from_devices or test_get_device_channels or test_overrides_per_device" -v`
Expected: FAIL

**Step 3: Implement config_store changes**

Modify `Hub/config_store.py`:

1. **`_overrides_path(device_id=None)`** — if device_id given, returns `variable_overrides_{device_id}.json`; if None, returns legacy `variable_overrides.json`

2. **`load_overrides(device_id=None)`** — loads from per-device file when device_id provided

3. **`save_overrides(overrides, device_id=None)`** — saves to per-device file

4. **`patch_variable_override(tag, fields, device_id=None)`** — operates on per-device overrides

5. **`get_channels()`** — reads channels from `devices[*].channels` instead of global `channels` section. Returns aggregated `{channel: {delay_ms, history_size, device_id}}`.

6. **`get_device_channels(device_id)`** — returns only channels for that device.

7. **`create_channel(channel, delay_ms, history_size, device_id)`** — adds to `devices[device_id].channels`.

8. **`delete_channel(channel, device_id)`** — removes from `devices[device_id].channels`.

9. **`update_channel_delay(channel, delay_ms, device_id)`** — updates in device.channels.

10. **`update_channel_history_size(channel, size, device_id)`** — updates in device.channels.

11. **`get_channel_history_sizes()`** — aggregates from all devices.

12. **`load_all_variables()`** — for each device, loads its per-device overrides file. Falls back to global overrides if per-device file doesn't exist (migration support).

13. **`SYSTEM_CHANNELS`** — update to include `user_status`, `ia_status`, `ia_data` (global). Remove `plc_commands`, `plc_data`, `alarms`, `config_reload` (no longer global).

14. **`create_device(device_id, cfg)`** — auto-generate `command_channel` as `{device_id}_commands` if not provided. Initialize empty `channels: {}` if not provided.

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hub.py -k "test_get_channels_from_devices or test_get_device_channels or test_overrides_per_device" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add Hub/config_store.py tests/test_hub.py
git commit -m "feat: config_store per-device channels and overrides"
```

---

### Task 2: Migrate `group_config.json` and split `variable_overrides.json`

**Files:**
- Modify: `tables/group_config.json`
- Create: `tables/variable_overrides_simulador.json`
- Create: `tables/variable_overrides_west.json`
- Create: `scripts/migrate_config.py`
- Test: Manual verification

**Step 1: Write migration script**

Create `scripts/migrate_config.py` that:
1. Reads current `group_config.json`
2. If global `channels` section exists, moves channels into the first device (or distributes based on overrides mapping)
3. Adds `command_channel` to each device (e.g., `simulador_commands`, `west_commands`)
4. Removes `aggregate_channel` and `backward_compatible` from `_meta`
5. Removes global `channels` section
6. Reads `variable_overrides.json`
7. For each device, reads its CSV files, collects tag names
8. Partitions overrides: tags from device X go to `variable_overrides_X.json`
9. Tags not found in any device CSV get logged as orphans
10. Saves new config and override files
11. Renames old `variable_overrides.json` to `variable_overrides.json.bak`

```python
#!/usr/bin/env python3
"""Migrate group_config.json and variable_overrides.json to per-device format."""
import json
import os
import sys
import pandas as pd

def main():
    tables_dir = sys.argv[1] if len(sys.argv) > 1 else 'tables'
    config_path = os.path.join(tables_dir, 'group_config.json')
    overrides_path = os.path.join(tables_dir, 'variable_overrides.json')

    with open(config_path) as f:
        config = json.load(f)

    # Check if already migrated (devices have channels)
    devices = config.get('devices', {})
    first_dev = next(iter(devices.values()), {})
    if 'channels' in first_dev:
        print("Already migrated (devices have channels section).")
        return

    # Move global channels into first device
    global_channels = config.pop('channels', {})
    meta = config.get('_meta', {})
    meta.pop('aggregate_channel', None)
    meta.pop('backward_compatible', None)

    for dev_id, dev_cfg in devices.items():
        dev_cfg.setdefault('channels', dict(global_channels))
        dev_cfg.setdefault('command_channel', f'{dev_id}_commands')

    # Save migrated config
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"group_config.json migrated.")

    # Split overrides
    if not os.path.exists(overrides_path):
        print("No variable_overrides.json to split.")
        return

    with open(overrides_path) as f:
        overrides = json.load(f)

    # Build tag->device mapping from CSVs
    tag_device = {}
    for dev_id, dev_cfg in devices.items():
        for csv_name in dev_cfg.get('csv_files', []):
            csv_path = os.path.join(tables_dir, csv_name)
            if not os.path.exists(csv_path):
                continue
            df = pd.read_csv(csv_path)
            if 'ObjecTag' in df.columns:
                for tag in df['ObjecTag'].dropna().astype(str).str.strip():
                    tag_device.setdefault(tag, dev_id)

    # Partition
    device_overrides = {dev_id: {} for dev_id in devices}
    orphans = {}
    for tag, ov in overrides.items():
        dev = tag_device.get(tag)
        if dev:
            device_overrides[dev][tag] = ov
        else:
            orphans[tag] = ov

    # Save per-device overrides
    for dev_id, ovs in device_overrides.items():
        path = os.path.join(tables_dir, f'variable_overrides_{dev_id}.json')
        with open(path, 'w') as f:
            json.dump(ovs, f, indent=2, ensure_ascii=False)
        print(f"  {path}: {len(ovs)} tags")

    if orphans:
        path = os.path.join(tables_dir, 'variable_overrides_orphans.json')
        with open(path, 'w') as f:
            json.dump(orphans, f, indent=2, ensure_ascii=False)
        print(f"  {path}: {len(orphans)} orphan tags")

    # Backup original
    os.rename(overrides_path, overrides_path + '.bak')
    print(f"Original overrides backed up to {overrides_path}.bak")

if __name__ == '__main__':
    main()
```

**Step 2: Run migration script**

Run: `python scripts/migrate_config.py tables`
Expected: Config migrated, per-device override files created

**Step 3: Verify migrated files**

Run: `cat tables/group_config.json | python -m json.tool` — verify channels inside devices, no global channels
Run: `ls tables/variable_overrides_*.json` — verify per-device files exist

**Step 4: Commit**

```bash
git add scripts/migrate_config.py tables/group_config.json tables/variable_overrides_*.json
git commit -m "feat: migrate config to per-device channels and overrides"
```

---

### Task 3: Update ProcessManager for per-device process management

**Files:**
- Modify: `Hub/process_manager.py`
- Test: `tests/test_hub.py`

**Step 1: Write failing tests**

```python
def test_start_process_per_device():
    """ProcessManager uses proc_id = '{proc_type}:{device_id}' and passes DEVICE_ID env."""

def test_multiple_delfos_processes():
    """Can run delfos:sim and delfos:west simultaneously."""

def test_stop_process_per_device():
    """stop_process('delfos:sim') stops only that device's Delfos."""

def test_process_state_includes_device_id():
    """to_state_dict() includes device_id field."""
```

**Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_hub.py -k "test_start_process_per_device or test_multiple_delfos or test_stop_process_per_device or test_process_state_includes_device_id" -v`

**Step 3: Implement ProcessManager changes**

Modify `Hub/process_manager.py`:

1. **`ProcessInstance.__init__`** — add `device_id` parameter, store as `self.device_id`

2. **`ProcessInstance.start`** — add env vars:
   - `DEVICE_ID` = device_id
   - `COMMAND_CHANNEL` = config.get('command_channel')
   - `CONFIG_RELOAD_CHANNEL` = f'config_reload_{device_id}'

3. **`ProcessInstance.to_state_dict`** — add `device_id` to returned dict

4. **`ProcessManager.start_process`** — accept `device_id` param, construct `proc_id = f"{proc_type}:{device_id}"`, pass device_id to ProcessInstance

**Step 4: Run tests**

Run: `python -m pytest tests/test_hub.py -k "test_start_process_per_device or test_multiple_delfos" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add Hub/process_manager.py tests/test_hub.py
git commit -m "feat: ProcessManager per-device process isolation"
```

---

### Task 4: Update Hub REST API for per-device processes and channels

**Files:**
- Modify: `Hub/main.py`
- Test: `tests/test_hub.py`

**Step 1: Write failing tests for new API routes**

```python
# Process endpoints - new URL structure
def test_start_process_new_api():
    """POST /api/processes/{device_id}/{proc_type}/start"""

def test_stop_process_new_api():
    """POST /api/processes/{device_id}/{proc_type}/stop"""

def test_get_process_logs_new_api():
    """GET /api/processes/{device_id}/{proc_type}/logs"""

# Channel endpoints - device-scoped
def test_create_channel_in_device():
    """POST /api/channels with device_id in body"""

def test_delete_channel_from_device():
    """DELETE /api/channels/{channel}?device_id=sim"""

def test_get_device_channels():
    """GET /api/devices/{device_id}/channels"""

# Variable endpoints - device-scoped
def test_patch_variable_with_device():
    """PATCH /api/variables/{tag}?device_id=sim"""

def test_bulk_assign_with_device():
    """POST /api/variables/bulk-assign with device_id in body"""

def test_get_variables_per_device():
    """GET /api/variables?device_id=sim returns only that device's variables"""
```

**Step 2: Run tests to verify they fail**

**Step 3: Implement Hub main.py changes**

1. **Process endpoints** — new URL structure:
   ```python
   @app.post('/api/processes/{device_id}/{proc_type}/start')
   async def start_process(device_id: str, proc_type: str):
       # No more body.device_id - device_id is in URL
       # proc_id = f"{proc_type}:{device_id}"
       # Also pass command_channel and config_reload_channel to config

   @app.post('/api/processes/{device_id}/{proc_type}/stop')
   async def stop_process(device_id: str, proc_type: str):
       proc_id = f"{proc_type}:{device_id}"

   @app.get('/api/processes/{device_id}/{proc_type}/logs')
   async def get_process_logs(device_id: str, proc_type: str, last_n: int = 100):
       proc_id = f"{proc_type}:{device_id}"
   ```

2. **Channel endpoints** — add device_id parameter:
   ```python
   class ChannelCreate(BaseModel):
       channel: str
       delay_ms: int = 1000
       history_size: int = 100
       device_id: str  # required - which device owns this channel

   @app.post('/api/channels', status_code=201)
   async def create_channel(body: ChannelCreate):
       # Remove plc_ prefix requirement
       config_store.create_channel(body.channel, body.delay_ms, body.history_size, body.device_id)

   @app.delete('/api/channels/{channel}')
   async def delete_channel(channel: str, device_id: str):
       config_store.delete_channel(channel, device_id)
   ```

3. **Variable endpoints** — add device_id:
   ```python
   @app.patch('/api/variables/{tag}')
   async def patch_variable(tag: str, body: VariablePatch):
       # body now includes device_id
       config_store.patch_variable_override(tag, fields, body.device_id)
       # Publish config_reload_{device_id} instead of config_reload
       await redis_pub.publish(f'config_reload_{body.device_id}', ...)

   @app.get('/api/variables')
   async def get_variables(device_id: str = None):
       # Optional filter by device
   ```

4. **Device creation** — auto-generate command_channel:
   ```python
   @app.post('/api/devices', status_code=201)
   async def create_device(body: DeviceCreate):
       cfg = body.model_dump(exclude={'device_id'})
       cfg.setdefault('channels', {})
       cfg.setdefault('command_channel', f'{body.device_id}_commands')
   ```

5. **`plc_write` Socket.IO event** — route to device-specific command channel:
   ```python
   @sio.event
   async def plc_write(sid, data):
       device_id = data.get('device_id')
       if device_id:
           devices = config_store.get_devices()
           cmd_channel = devices[device_id].get('command_channel', f'{device_id}_commands')
           await redis_pub.publish(cmd_channel, json.dumps(data))
   ```

6. **`config_reload` publishing** — use per-device channel:
   - Replace all `redis_pub.publish('config_reload', ...)` with `redis_pub.publish(f'config_reload_{device_id}', ...)`
   - For operations affecting all devices (e.g., bulk operations), publish to each device's reload channel

7. **Socket.IO `join` event** — support device_id:
   ```python
   @sio.event
   async def join(sid, data):
       device_id = data.get('device_id')
       channel = data.get('channel')
       if device_id:
           await sio.enter_room(sid, device_id)
           if channel:
               await sio.enter_room(sid, f'{device_id}:{channel}')
   ```

8. **`_proc_status_broadcast`** — include device_id from ProcessInstance state

9. **New endpoint: `GET /api/devices/{device_id}/channels`**:
   ```python
   @app.get('/api/devices/{device_id}/channels')
   async def get_device_channels(device_id: str):
       return config_store.get_device_channels(device_id)
   ```

**Step 4: Run tests**

Run: `python -m pytest tests/test_hub.py -v`
Expected: New tests PASS, verify old tests updated

**Step 5: Commit**

```bash
git add Hub/main.py tests/test_hub.py
git commit -m "feat: Hub REST API per-device processes, channels, and variables"
```

---

### Task 5: Update Redis Bridge for dynamic channel subscription

**Files:**
- Modify: `Hub/redis_bridge.py`
- Test: `tests/test_hub.py`

**Step 1: Write failing tests**

```python
def test_bridge_subscribes_device_channels():
    """Bridge subscribes to channels defined inside each device, not plc_*."""

def test_bridge_emits_device_data():
    """Bridge emits 'device:data' with device_id to device room."""

def test_bridge_resubscribes_on_config_change():
    """Bridge re-subscribes when config_reload is received."""
```

**Step 2: Implement bridge changes**

Modify `Hub/redis_bridge.py`:

1. Accept `config_store` reference (or read config internally)
2. Build channel list from all `devices[*].channels` keys
3. Also build a `channel_to_device` mapping: `{"sim_alarmes": "simulador", ...}`
4. Subscribe explicitly to each channel (not `psubscribe('plc_*')`)
5. Also subscribe to a meta channel `_bridge_reload` for re-subscription
6. When emitting, derive `device_id` from `channel_to_device` map
7. Emit to room `device_id` with event `device:data` payload `{device_id, channel, data}`
8. Emit to room `{device_id}:{channel}` with event `channel:data` payload `data`
9. Remove the old `plc:data` and `plc:{channel}` events

```python
async def _run_bridge(sio, redis_host, redis_port, get_channel_map):
    """get_channel_map is a callable returning {channel: device_id}."""
    r = aioredis.Redis(host=redis_host, port=redis_port, db=0)
    await r.ping()

    channel_map = get_channel_map()
    async with r.pubsub() as ps:
        if channel_map:
            await ps.subscribe(*channel_map.keys())
        await ps.subscribe('_bridge_reload')

        while True:
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue

            channel = msg['channel'].decode()

            if channel == '_bridge_reload':
                # Re-subscribe with new channel list
                new_map = get_channel_map()
                old_channels = set(channel_map.keys())
                new_channels = set(new_map.keys())
                to_unsub = old_channels - new_channels
                to_sub = new_channels - old_channels
                if to_unsub:
                    await ps.unsubscribe(*to_unsub)
                if to_sub:
                    await ps.subscribe(*to_sub)
                channel_map = new_map
                continue

            data = json.loads(msg['data'])
            device_id = channel_map.get(channel, 'unknown')

            await sio.emit('device:data', {
                'device_id': device_id, 'channel': channel, 'data': data
            }, room=device_id)

            await sio.emit('channel:data', data, room=f'{device_id}:{channel}')
```

3. In `Hub/main.py`, update `start_bridge` call to pass channel map getter:
```python
def _get_channel_map():
    devices = config_store.get_devices()
    result = {}
    for dev_id, dev_cfg in devices.items():
        for ch in dev_cfg.get('channels', {}):
            result[ch] = dev_id
    return result

asyncio.create_task(start_bridge(sio, _REDIS_HOST, _REDIS_PORT, _get_channel_map))
```

4. When config changes, publish `_bridge_reload` to trigger re-subscription:
```python
await redis_pub.publish('_bridge_reload', '1')
```

**Step 3: Run tests**

Run: `python -m pytest tests/test_hub.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add Hub/redis_bridge.py Hub/main.py tests/test_hub.py
git commit -m "feat: redis bridge dynamic per-device channel subscription"
```

---

### Task 6: Update Delfos for per-device operation

**Files:**
- Modify: `Delfos/delfos.py`
- Modify: `Delfos/table_filter.py`
- Test: `tests/test_segmented_reading.py`

**Step 1: Write failing tests**

```python
def test_delfos_reads_device_id_from_env():
    """Delfos reads DEVICE_ID env var and loads only that device's config."""

def test_delfos_reads_device_channels():
    """Delfos reads channels from device_cfg['channels'], not global channels."""

def test_delfos_uses_device_overrides():
    """Delfos loads variable_overrides_{device_id}.json."""

def test_delfos_publishes_to_device_channels_only():
    """Delfos publishes only to channels defined in its device config."""

def test_delfos_no_backward_compat():
    """Delfos does not publish to plc_data or alarms."""

def test_delfos_listens_device_config_reload():
    """Delfos subscribes to config_reload_{device_id}, not config_reload."""

def test_extract_parameters_uses_device_channels():
    """extract_parameters_by_channel uses device channels config, not global."""
```

**Step 2: Implement Delfos changes**

Modify `Delfos/delfos.py`:

1. Read `DEVICE_ID` from env (required, fail if not set)
2. Load `group_config.json`, extract only `devices[DEVICE_ID]`
3. Build CSV paths from `device_cfg['csv_files']` only
4. Read channel config from `device_cfg['channels']` instead of global `channels`
5. Load overrides from `variable_overrides_{device_id}.json`
6. Subscribe to `user_status` and `config_reload_{device_id}`
7. Remove backward_compatible / plc_data / alarms logic entirely
8. On config_reload, reload only this device's config and overrides

Key changes to `main()`:
```python
def main():
    device_id = os.environ.get('DEVICE_ID')
    if not device_id:
        logger.critical("DEVICE_ID env var required. Encerrando.")
        return

    # Load config
    group_config = _load_group_config(group_config_path)
    device_cfg = group_config.get('devices', {}).get(device_id)
    if not device_cfg:
        logger.critical("Device '%s' not found in config.", device_id)
        return

    # CSV paths from device only
    csv_paths = [os.path.join(_TABLES_DIR, f) for f in device_cfg.get('csv_files', [])]

    # Overrides from per-device file
    overrides_path = os.path.join(_TABLES_DIR, f'variable_overrides_{device_id}.json')
    overrides = _load_variable_overrides(overrides_path)

    # Channel config from device
    channels_cfg = device_cfg.get('channels', {})

    # Config reload channel
    reload_channel = f'config_reload_{device_id}'
    subscribe_to_channels(pubsub, ['user_status', reload_channel])

    # No backward compat
    # Remove agg_operacao, agg_configuracao, plc_data, alarms publishing
```

Modify `Delfos/table_filter.py` `extract_parameters_by_channel()`:
- Accept `channels_cfg` dict directly (device channels) instead of reading from `group_config.get('channels', {})`
- Or: keep function signature, but callers pass device-scoped group_config

**Step 3: Run tests**

Run: `python -m pytest tests/test_segmented_reading.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add Delfos/delfos.py Delfos/table_filter.py tests/test_segmented_reading.py
git commit -m "feat: Delfos per-device isolation (DEVICE_ID, device channels, device overrides)"
```

---

### Task 7: Update Atena for per-device operation

**Files:**
- Modify: `Atena/atena.py`
- Modify: `Atena/data_handle.py`
- Test: `tests/test_atena.py`

**Step 1: Write failing tests**

```python
def test_atena_reads_device_id_from_env():
    """Atena reads DEVICE_ID and loads that device's CSVs."""

def test_atena_subscribes_command_channel():
    """Atena subscribes to COMMAND_CHANNEL env var, not plc_commands."""

def test_atena_uses_device_csvs():
    """Atena loads CSVs from device config, not hardcoded operacao.csv."""

def test_atena_handles_commands_from_device_channel():
    """Commands arriving on device command_channel are processed."""
```

**Step 2: Implement Atena changes**

Modify `Atena/atena.py`:

```python
def main():
    device_id = os.environ.get('DEVICE_ID')
    if not device_id:
        logger.critical("DEVICE_ID env var required.")
        return

    command_channel = os.environ.get('COMMAND_CHANNEL', f'{device_id}_commands')

    # Load device config to get CSV files
    group_config_path = os.path.join(_TABLES_DIR, 'group_config.json')
    with open(group_config_path) as f:
        group_config = json.load(f)
    device_cfg = group_config.get('devices', {}).get(device_id, {})
    csv_files = device_cfg.get('csv_files', [])
    csv_paths = [os.path.join(_TABLES_DIR, f) for f in csv_files]

    # Subscribe to device-specific command channel
    channels = ['user_status', command_channel, 'ia_status', 'ia_data']
    # ...

    for message in pubsub.listen():
        if message and message['type'] == 'message':
            channel = message['channel'].decode()

            if channel == command_channel:
                handle_plc_commands_message(message, user_state, client, csv_paths)
            # ... rest same
```

Modify `Atena/data_handle.py`:

```python
def handle_plc_commands_message(message, user_state, client, csv_paths):
    """csv_paths is now a list of paths instead of single path."""
    if user_state:
        write_data, timestamp = get_write_data(message)
        # Search across all device CSVs
        all_coils_addr, all_coils_vals = [], []
        all_regs_addr, all_regs_vals = [], []
        for csv_path in csv_paths:
            c_addr, c_vals, r_addr, r_vals = find_values_by_object_tag(csv_path, write_data)
            all_coils_addr.extend(c_addr)
            all_coils_vals.extend(c_vals)
            all_regs_addr.extend(r_addr)
            all_regs_vals.extend(r_vals)

        write_coils_to_device(client, all_coils_addr, all_coils_vals)
        write_registers_to_device(client, all_regs_addr, all_regs_vals)
```

**Step 3: Run tests**

Run: `python -m pytest tests/test_atena.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add Atena/atena.py Atena/data_handle.py tests/test_atena.py
git commit -m "feat: Atena per-device isolation (DEVICE_ID, COMMAND_CHANNEL, multi-CSV)"
```

---

### Task 8: Update Frontend — tabs per device

**Files:**
- Modify: `Hub/templates/index.html`

This is the largest single file change. The frontend needs to become device-contextual.

**Step 1: Restructure HTML layout**

Replace the current device tabs navbar and sidebar with:

1. **Tab bar** below navbar: one tab per device + "Config" tab
2. Each tab click sets `activeDevice` and reloads sidebar content
3. **Sidebar** becomes contextual:
   - **Processes section**: shows Delfos/Atena for selected device (no dropdown)
   - **Channels section**: only shows channels of the selected device
   - **Device Info section**: connection details, ping, edit
4. **Config tab**: shows CRUD table of all devices (replaces old devices sidebar section)
5. **Grid**: automatically filtered to selected device's variables

**Step 2: Update JavaScript state management**

```javascript
// New state
let activeDevice = null;  // device_id string, null = Config tab
const deviceProcesses = {}; // {device_id: {delfos: state, atena: state}}

// Socket.IO - join/leave device rooms
function switchToDevice(deviceId) {
    if (activeDevice) {
        socket.emit('leave', {device_id: activeDevice});
    }
    activeDevice = deviceId;
    if (deviceId) {
        socket.emit('join', {device_id: deviceId});
    }
    renderSidebar();
    applyFilters();
}
```

**Step 3: Update Socket.IO event handlers**

```javascript
socket.on('device:data', ({device_id, channel, data}) => {
    if (device_id !== activeDevice) return;
    // Merge values...
    const all = {};
    for (const section of [data.coils || {}, data.registers || {}]) {
        for (const [group, tags] of Object.entries(section)) {
            all[group] = {...(all[group] || {}), ...tags};
        }
    }
    Object.entries(all).forEach(([, tags]) => {
        Object.entries(tags).forEach(([tag, val]) => {
            lastValues[tag] = {value: val, ts: data.timestamp};
        });
    });
    if (varGrid) varGrid.refreshCells({columns: ['last_value'], force: true});
});

socket.on('proc:status', (state) => {
    const devId = state.device_id;
    if (!deviceProcesses[devId]) deviceProcesses[devId] = {};
    deviceProcesses[devId][state.proc_type] = state;
    renderDeviceTab(devId);
    if (devId === activeDevice) renderProcessCards();
});
```

**Step 4: Update channel management**

```javascript
async function loadDeviceChannels(deviceId) {
    const data = await apiFetch(`/api/devices/${deviceId}/channels`);
    renderChannelCards(data, deviceId);
}

async function createChannel(deviceId) {
    const name = prompt('Nome do canal:');
    if (!name) return;
    await apiFetch('/api/channels', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({channel: name, device_id: deviceId}),
    });
    loadDeviceChannels(deviceId);
}
```

**Step 5: Update process management**

```javascript
async function startProcess(deviceId, procType) {
    await apiFetch(`/api/processes/${deviceId}/${procType}/start`, {method: 'POST'});
}

async function stopProcess(deviceId, procType) {
    await apiFetch(`/api/processes/${deviceId}/${procType}/stop`, {method: 'POST'});
}

async function showProcessLogs(deviceId, procType) {
    const data = await apiFetch(`/api/processes/${deviceId}/${procType}/logs`);
    // Show in modal...
}
```

**Step 6: Update plc_write**

```javascript
socket.emit('plc_write', {device_id: activeDevice, ...writePayload});
```

**Step 7: Remove channel prefix requirement**

Update the new channel modal to not force `plc_` prefix — channels are now free-form names.

**Step 8: Commit**

```bash
git add Hub/templates/index.html
git commit -m "feat: frontend per-device tabs, contextual sidebar, device-scoped channels"
```

---

### Task 9: Update existing tests and run full test suite

**Files:**
- Modify: `tests/test_hub.py`
- Modify: `tests/test_segmented_reading.py`
- Modify: `tests/test_atena.py`
- Modify: `tests/test_full_loop.py`

**Step 1: Update test_hub.py**

- Update all REST API test calls to use new URL structure
- Existing channel tests: pass `device_id` to channel operations
- Existing variable tests: pass `device_id` to override operations
- Process tests: use `{device_id}/{proc_type}` URL pattern
- Config store tests: use per-device overrides
- Update fixtures to create group_config with channels inside devices

**Step 2: Update test_segmented_reading.py**

- Set `DEVICE_ID` env var in test fixtures
- Create per-device override files
- Update group_config fixtures to have channels inside devices
- Remove backward_compatible tests (plc_data channel)

**Step 3: Update test_atena.py**

- Set `DEVICE_ID` and `COMMAND_CHANNEL` env vars
- Update CSV path handling to use device config
- Test with multi-CSV lookup

**Step 4: Update test_full_loop.py**

- Launch Delfos and Atena with `DEVICE_ID` env vars
- Subscribe to device-specific channels instead of `plc_*`

**Step 5: Run full test suite**

Run: `python -m pytest tests/test_hub.py tests/test_segmented_reading.py tests/test_atena.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add tests/
git commit -m "test: update all tests for per-device isolation architecture"
```

---

### Task 10: Update CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update CLAUDE.md**

Update all sections to reflect the new architecture:
- Project structure: note per-device override files
- `group_config.json` structure: channels inside devices, command_channel
- `variable_overrides.json` section: now per-device files
- Delfos description: reads DEVICE_ID, device-scoped config
- Atena description: reads DEVICE_ID, COMMAND_CHANNEL
- Hub endpoints table: new URL patterns
- Socket.IO events: new event names (device:data, channel:data)
- Redis channels table: per-device channels
- System channels: updated list
- Env vars: add DEVICE_ID, COMMAND_CHANNEL, CONFIG_RELOAD_CHANNEL
- Remove references to backward_compatible, plc_data, alarms channels
- Update "Como executar" section

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for per-device isolation architecture"
```

---

## Task Dependency Graph

```
Task 1 (config_store) ──┬── Task 2 (migration script)
                        ├── Task 3 (ProcessManager) ── Task 4 (Hub API)
                        ├── Task 5 (Redis Bridge)
                        ├── Task 6 (Delfos)
                        ├── Task 7 (Atena)
                        └── Task 8 (Frontend)
                                      │
                               Task 9 (Tests) ── Task 10 (Docs)
```

Tasks 2-8 can be worked on in parallel after Task 1 is complete.
Task 9 integrates all changes.
Task 10 is documentation after everything works.
