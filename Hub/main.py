#!/usr/bin/env python3
"""
Hub — Socket.IO bridge + Config Panel REST API.

Inicia com:
    cd Hub && uvicorn main:asgi_app --host 0.0.0.0 --port 4567 --reload
    ou
    uvicorn Hub.main:asgi_app --host 0.0.0.0 --port 4567  (a partir de gateway/)

Variáveis de ambiente (.env):
    REDIS_HOST, REDIS_PORT, TABLES_DIR, HUB_HOST, HUB_PORT
"""

import asyncio
import datetime
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import redis.asyncio as aioredis
import socketio
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

# Garante que Hub/ e gateway/ estejam no path para imports locais
_HUB_DIR     = os.path.dirname(os.path.abspath(__file__))
_GATEWAY_DIR = os.path.dirname(_HUB_DIR)
sys.path.insert(0, _HUB_DIR)
sys.path.insert(0, _GATEWAY_DIR)

import config_store          # noqa: E402  (Hub/config_store.py)
import grafana_api           # noqa: E402  (Hub/grafana_api.py)
from redis_bridge import start_bridge  # noqa: E402  (Hub/redis_bridge.py)
from process_manager import ProcessManager      # noqa: E402
from simulator_manager import SimulatorManager  # noqa: E402
from scanner_manager import ScannerManager      # noqa: E402

load_dotenv(os.path.join(_HUB_DIR, '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

_REDIS_HOST  = os.environ.get('REDIS_HOST', 'localhost')
_REDIS_PORT  = int(os.environ.get('REDIS_PORT', 6379))
_thread_pool = ThreadPoolExecutor(max_workers=4)

# SimulatorManager — simuladores Modbus embarcados
sim_manager: SimulatorManager | None = None

# ProcessManager — subprocessos Delfos/Atena
proc_manager: ProcessManager | None = None

# ScannerManager — scanner de variáveis Modbus
scan_manager: ScannerManager | None = None

def _derive_rooms() -> list[str]:
    """Deriva rooms Socket.IO a partir dos canais configurados.

    Retorna rooms no formato device_id e device_id:channel.
    Ex.: ['sim', 'sim:plc_alarmes', 'sim:plc_process', 'west', 'west:west_data']
    """
    rooms: list[str] = []
    channels = config_store.get_channels()
    device_ids: set[str] = set()
    for ch, info in channels.items():
        dev_id = info.get('device_id', 'unknown')
        if dev_id not in device_ids:
            device_ids.add(dev_id)
            rooms.append(dev_id)
        room = f'{dev_id}:{ch}'
        if room not in rooms:
            rooms.append(room)
    return rooms


# ── Socket.IO + FastAPI ───────────────────────────────────────────────────────

sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    logger=False,
    engineio_logger=False,
)
app = FastAPI(title='Gateway Hub', version='2.0.0')
app.include_router(grafana_api.router)

# ASGI app: Socket.IO roteia WebSockets; FastAPI roteia HTTP
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# Redis para publicação de comandos (user_status, plc_commands, config_reload)
redis_pub: aioredis.Redis | None = None


async def _publish_config_reload(device_id: str | None = None) -> None:
    """Publica config_reload nos canais corretos (per-device) + _bridge_reload."""
    if not redis_pub:
        return
    payload = json.dumps({'reload': True})
    if device_id:
        await redis_pub.publish(f'config_reload_{device_id}', payload)
    else:
        # Broadcast para todos os devices
        for dev_id in config_store.get_devices():
            await redis_pub.publish(f'config_reload_{dev_id}', payload)
    await redis_pub.publish('_bridge_reload', '1')


# ── Ciclo de vida ─────────────────────────────────────────────────────────────

@app.on_event('startup')
async def on_startup():
    global redis_pub, sim_manager, proc_manager, scan_manager
    redis_pub = aioredis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=0)
    logger.info('Hub: Redis publisher pronto em %s:%s', _REDIS_HOST, _REDIS_PORT)
    def _get_channel_map():
        """Build {channel_name: device_id} from config_store.get_channels().
        Excludes disabled channels."""
        channels = config_store.get_channels()
        return {ch: info.get('device_id', 'unknown')
                for ch, info in channels.items()
                if info.get('enabled', True)}

    asyncio.create_task(start_bridge(sio, _REDIS_HOST, _REDIS_PORT,
                                     get_channel_map=_get_channel_map))

    # Inicializa SimulatorManager
    sim_manager = SimulatorManager(config_store._TABLES_DIR)
    await sim_manager.init_from_config()
    asyncio.create_task(_sim_broadcast_loop())

    # Inicializa ProcessManager
    proc_manager = ProcessManager(_GATEWAY_DIR)
    proc_manager.set_status_callback(_proc_status_broadcast)

    # Inicializa ScannerManager
    scan_manager = ScannerManager(config_store._TABLES_DIR)
    scan_manager.load_all_cached()

    # Inicializa Grafana API
    grafana_api.init(redis_pub, config_store.get_channels, config_store.load_all_variables)

    logger.info('Hub iniciado.')


@app.on_event('shutdown')
async def on_shutdown():
    if scan_manager:
        await scan_manager.shutdown()
    if proc_manager:
        await proc_manager.shutdown_all()
    if sim_manager:
        await sim_manager.shutdown_all()
    if redis_pub:
        await redis_pub.aclose()
    logger.info('Hub encerrado.')


async def _sim_broadcast_loop():
    """Broadcast periódico de valores dos simuladores rodando para rooms sim:{id}."""
    while True:
        await asyncio.sleep(0.5)
        if not sim_manager:
            continue
        for sim_id, sim in sim_manager._simulators.items():
            if not sim.running:
                continue
            try:
                values = sim.read_all_values()
                await sio.emit('sim:values', {
                    'sim_id': sim_id,
                    'values': values,
                    'timestamp': datetime.datetime.now().isoformat(),
                }, room=f'sim:{sim_id}')
            except Exception as exc:
                logger.debug("Erro no broadcast sim:%s: %s", sim_id, exc)


async def _proc_status_broadcast(state: dict) -> None:
    """Callback do ProcessManager — emite proc:status para todos os clientes."""
    await sio.emit('proc:status', state)


# ── Processos (Delfos / Atena) ────────────────────────────────────────────

class ProcessStartBody(BaseModel):
    device_id: str


@app.get('/api/processes')
async def list_processes():
    """Lista todos os processos com estado."""
    if not proc_manager:
        return {}
    return proc_manager.list_processes()


@app.post('/api/processes/{proc_type}/start')
async def start_process(proc_type: str, body: ProcessStartBody):
    """Inicia Delfos ou Atena apontando para um device."""
    if proc_type not in ('delfos', 'atena'):
        raise HTTPException(status_code=422, detail="proc_type deve ser 'delfos' ou 'atena'.")
    if not proc_manager:
        raise HTTPException(status_code=503, detail='ProcessManager nao inicializado.')

    # Verifica se scan está rodando para o device (proteção cruzada)
    if proc_type == 'delfos' and scan_manager and scan_manager.is_scanning(body.device_id):
        raise HTTPException(status_code=409, detail=f"Scan em andamento para device '{body.device_id}'. Cancele o scan primeiro.")

    # Busca config do device
    devices = config_store.get_devices()
    if body.device_id not in devices:
        raise HTTPException(status_code=404, detail=f"Device '{body.device_id}' nao encontrado.")
    dev = devices[body.device_id]

    config = {
        'modbus_host': dev.get('host', ''),
        'modbus_port': dev.get('port', 502),
        'modbus_unit_id': dev.get('unit_id', 1),
        'modbus_protocol': dev.get('protocol', 'tcp'),
        'redis_host': _REDIS_HOST,
        'redis_port': _REDIS_PORT,
        'tables_dir': os.path.abspath(config_store._TABLES_DIR),
        'command_channel': dev.get('command_channel', f'{body.device_id}_commands'),
        'config_reload_channel': f'config_reload_{body.device_id}',
    }

    try:
        proc = await proc_manager.start_process(proc_type, body.device_id, config)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return proc.to_state_dict()


@app.post('/api/processes/{proc_type}/stop')
async def stop_process(proc_type: str, body: ProcessStartBody):
    """Para o processo Delfos ou Atena de um device especifico."""
    if proc_type not in ('delfos', 'atena'):
        raise HTTPException(status_code=422, detail="proc_type deve ser 'delfos' ou 'atena'.")
    if not proc_manager:
        raise HTTPException(status_code=503, detail='ProcessManager nao inicializado.')
    proc_id = f"{proc_type}:{body.device_id}"
    try:
        proc = await proc_manager.stop_process(proc_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return proc.to_state_dict()


@app.get('/api/processes/{proc_type}/logs')
async def get_process_logs(proc_type: str, device_id: str = '', last_n: int = 100):
    """Retorna as ultimas linhas de log do processo."""
    if proc_type not in ('delfos', 'atena'):
        raise HTTPException(status_code=422, detail="proc_type deve ser 'delfos' ou 'atena'.")
    if not proc_manager:
        return {'lines': []}
    proc_id = f"{proc_type}:{device_id}" if device_id else proc_type
    proc = proc_manager.get_process(proc_id)
    if not proc:
        return {'lines': []}
    return {'lines': proc.get_logs(last_n)}


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/api/channels')
async def get_channels():
    """Retorna {canal: {delay_ms, history_size}} para todos os canais configurados."""
    return config_store.get_channels()


@app.get('/api/groups')
async def get_groups():
    """Fase 5: seção 'groups' removida — retorna dict vazio."""
    return {}


@app.get('/')
async def index():
    """Serve o painel de configuração web."""
    return FileResponse(os.path.join(_HUB_DIR, 'templates', 'index.html'))


@app.get('/api/variables')
async def get_variables():
    """Retorna todas as variáveis com configuração mesclada + overrides brutos (mesclados de todos os devices)."""
    # Merge all per-device overrides into a single dict for the frontend
    merged_overrides: dict = {}
    devices = config_store.get_devices()
    if devices:
        for dev_id in devices:
            dev_ov = config_store.load_overrides(dev_id)
            merged_overrides.update(dev_ov)
    else:
        merged_overrides = config_store.load_overrides()
    return {
        'variables': config_store.load_all_variables(),
        'overrides': merged_overrides,
    }


class VariableCreate(BaseModel):
    device_id: str
    csv_file:  str
    tag:       str
    group:     str
    type:      str          # '%MB' ou '%MW'
    address:   str          # string — suporta bit addressing como '1584.05'
    classe:    str = ''


@app.post('/api/variables', status_code=201)
async def create_variable(body: VariableCreate):
    """Cria uma nova variável no CSV do device e opcionalmente cria override vazio."""
    try:
        result = config_store.add_csv_variable(
            device_id=body.device_id,
            csv_file=body.csv_file,
            tag=body.tag,
            group=body.group,
            at_type=body.type,
            address=body.address,
            classe=body.classe,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Cria override marcando a variável como adicionada manualmente
    config_store.patch_variable_override(body.tag, {'_added': True}, device_id=body.device_id)

    await _publish_config_reload(body.device_id)
    return result


class VariablePatch(BaseModel):
    enabled:   Optional[bool] = None
    channel:   Optional[str]  = None
    device_id: Optional[str]  = None
    # CSV-editable fields
    group:     Optional[str]  = None
    type:      Optional[str]  = None
    address:   Optional[str]  = None
    classe:    Optional[str]  = None
    new_tag:   Optional[str]  = None   # rename tag


@app.patch('/api/variables/{tag}')
async def patch_variable(tag: str, body: VariablePatch):
    """
    Atualiza (ou cria) o override de uma variável individual.
    channel=null ou channel="" → desatribui o canal da variável.
    Campos CSV (group, type, address, classe, new_tag) são escritos no CSV fonte.
    """
    explicit_device = body.device_id
    all_fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k != 'device_id'}
    if not all_fields and hasattr(body, 'model_fields_set'):
        all_fields = {k: getattr(body, k) for k in body.model_fields_set if k != 'device_id'}
    if not all_fields:
        raise HTTPException(status_code=422, detail='Nenhum campo fornecido.')

    tag_device = explicit_device or config_store.find_tag_device(tag)

    # Separate CSV fields from override fields
    csv_field_names = {'group', 'type', 'address', 'classe', 'new_tag'}
    csv_fields = {k: v for k, v in all_fields.items() if k in csv_field_names}
    override_fields = {k: v for k, v in all_fields.items() if k not in csv_field_names}

    # Handle tag rename: map new_tag → tag for CSV update
    if 'new_tag' in csv_fields:
        csv_fields['tag'] = csv_fields.pop('new_tag')

    # Write CSV fields to source file
    if csv_fields:
        updated = config_store.update_csv_variable(tag, csv_fields, device_id=tag_device)
        if not updated:
            raise HTTPException(status_code=404, detail=f"Tag '{tag}' não encontrada nos CSVs do device.")

        # If tag was renamed, update overrides key too
        if 'tag' in csv_fields:
            new_tag = csv_fields['tag']
            overrides = config_store.load_overrides(tag_device)
            if tag in overrides:
                overrides[new_tag] = overrides.pop(tag)
                config_store.save_overrides(overrides, tag_device)

    # Validate cross-device channel assignment
    if 'channel' in override_fields and override_fields['channel']:
        try:
            config_store.validate_channel_device(tag, override_fields['channel'], device_id=explicit_device)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    # Write override fields (channel, enabled)
    if override_fields:
        config_store.patch_variable_override(tag, override_fields, device_id=tag_device)

    await _publish_config_reload(tag_device)
    return {'tag': tag, 'updated': all_fields}


@app.delete('/api/variables/{tag}')
async def delete_variable(tag: str, device_id: Optional[str] = None):
    """Remove uma variável do CSV fonte e seu override."""
    tag_device = device_id or config_store.find_tag_device(tag)
    deleted = config_store.delete_csv_variable(tag, device_id=tag_device)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Tag '{tag}' não encontrada nos CSVs.")
    await _publish_config_reload(tag_device)
    return {'deleted': tag}


class BulkAssignBody(BaseModel):
    tags:      list[str]
    channel:   str = ''   # '' = remover atribuição; 'plc_xxx' = atribuir
    device_id: Optional[str] = None


@app.post('/api/variables/bulk-assign')
async def bulk_assign_channel(body: BulkAssignBody):
    """
    Move uma lista de variáveis para um canal.
    channel='' → remove atribuição (variáveis ficam não-atribuídas).
    """
    # Validate cross-device channel assignment for all tags
    if body.channel:
        for tag in body.tags:
            try:
                config_store.validate_channel_device(tag, body.channel, device_id=body.device_id)
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e))

    # Apply overrides per-device (use explicit device_id if provided)
    affected_devices: set[str] = set()
    for tag in body.tags:
        tag_device = body.device_id or config_store.find_tag_device(tag)
        config_store.patch_variable_override(tag, {'channel': body.channel}, device_id=tag_device)
        if tag_device:
            affected_devices.add(tag_device)

    for dev_id in affected_devices:
        await _publish_config_reload(dev_id)
    if not affected_devices:
        await _publish_config_reload()
    return {'assigned': len(body.tags), 'channel': body.channel}


class BulkEnableBody(BaseModel):
    tags:      list[str]
    enabled:   bool
    device_id: Optional[str] = None


@app.post('/api/variables/bulk-enable')
async def bulk_enable(body: BulkEnableBody):
    """
    Habilita ou desabilita uma lista de variáveis.
    """
    affected_devices: set[str] = set()
    for tag in body.tags:
        tag_device = body.device_id or config_store.find_tag_device(tag)
        config_store.patch_variable_override(tag, {'enabled': body.enabled}, device_id=tag_device)
        if tag_device:
            affected_devices.add(tag_device)

    for dev_id in affected_devices:
        await _publish_config_reload(dev_id)
    if not affected_devices:
        await _publish_config_reload()
    return {'updated': len(body.tags), 'enabled': body.enabled}


@app.post('/api/upload')
async def upload_xlsx(file: UploadFile = File(...)):
    """Parseia .xlsx enviado e retorna preview sem salvar."""
    content = await file.read()
    try:
        preview = config_store.parse_upload_xlsx(content)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {'preview': preview, 'count': len(preview)}


class UploadConfirmBody(BaseModel):
    rows: list[dict[str, Any]]


@app.post('/api/upload/confirm')
async def confirm_upload(body: UploadConfirmBody):
    """Aplica configuração parsed de um upload ao group_config + overrides."""
    config_store.apply_upload_config(body.rows)
    await _publish_config_reload()
    return {'applied': len(body.rows)}


@app.get('/api/export')
async def export_xlsx():
    """Gera e retorna .xlsx com a configuração mesclada atual."""
    xlsx_bytes = config_store.generate_export_xlsx()
    return Response(
        content=xlsx_bytes,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="gateway_config.xlsx"'},
    )


class ChannelCreate(BaseModel):
    channel: str
    delay_ms: int = 1000
    history_size: int = 100
    device_id: Optional[str] = None


@app.post('/api/channels', status_code=201)
async def create_channel(body: ChannelCreate):
    """Cria um novo canal Redis dentro de um device."""
    if not body.device_id:
        raise HTTPException(status_code=422, detail="device_id obrigatorio para criar canal.")
    if not body.channel or not body.channel.strip():
        raise HTTPException(status_code=422, detail="Nome do canal não pode ser vazio.")
    if body.delay_ms < 1 or body.history_size < 1:
        raise HTTPException(status_code=422, detail='delay_ms e history_size devem ser >= 1.')
    try:
        config_store.create_channel(body.channel, body.delay_ms, body.history_size, device_id=body.device_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _publish_config_reload(body.device_id)
    return {'channel': body.channel, 'delay_ms': body.delay_ms, 'history_size': body.history_size,
            'device_id': body.device_id}


@app.delete('/api/channels/{channel}')
async def delete_channel(channel: str, device_id: Optional[str] = None):
    """Remove um canal criado explicitamente. Não afeta grupos já mapeados."""
    try:
        config_store.delete_channel(channel, device_id=device_id)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _publish_config_reload(device_id)
    return {'deleted': channel}


@app.get('/api/channels/system')
async def get_system_channels():
    """Retorna lista de canais de sistema que não podem ser removidos."""
    return {'channels': sorted(config_store.SYSTEM_CHANNELS)}


class DelayPatch(BaseModel):
    delay_ms: int


@app.patch('/api/channels/{channel}/delay')
async def set_channel_delay(channel: str, body: DelayPatch, device_id: Optional[str] = None):
    """Atualiza delay_ms do canal e publica config_reload."""
    if body.delay_ms < 1:
        raise HTTPException(status_code=422, detail='delay_ms deve ser >= 1')
    config_store.update_channel_delay(channel, body.delay_ms, device_id=device_id)
    await _publish_config_reload()
    return {'channel': channel, 'delay_ms': body.delay_ms}


class HistoryPatch(BaseModel):
    history_size: int


@app.patch('/api/channels/{channel}/history')
async def set_channel_history(channel: str, body: HistoryPatch, device_id: Optional[str] = None):
    """
    Atualiza history_size do canal e aplica ltrim imediato nas listas Redis.
    Publica config_reload para que o Delfos recarregue sem reiniciar.
    """
    if body.history_size < 1:
        raise HTTPException(status_code=422, detail='history_size deve ser >= 1')

    config_store.update_channel_history_size(channel, body.history_size, device_id=device_id)

    if redis_pub:
        # Trunca imediatamente o histórico existente no Redis
        await redis_pub.ltrim(f'history:{channel}', 0, body.history_size - 1)

    await _publish_config_reload(device_id)
    return {'channel': channel, 'history_size': body.history_size}


@app.get('/api/channels/{channel}/history')
async def get_channel_history(channel: str, limit: int = 100):
    """Retorna as últimas `limit` mensagens do histórico Redis de um canal."""
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000
    if not redis_pub:
        raise HTTPException(status_code=503, detail='Redis indisponível.')
    raw = await redis_pub.lrange(f'history:{channel}', 0, limit - 1)
    items = []
    for entry in raw:
        try:
            items.append(json.loads(entry))
        except (json.JSONDecodeError, TypeError):
            items.append(str(entry))
    return {'channel': channel, 'count': len(items), 'items': items}


class ChannelEnabledPatch(BaseModel):
    enabled: bool


@app.patch('/api/channels/{channel}/enabled')
async def set_channel_enabled(channel: str, body: ChannelEnabledPatch, device_id: Optional[str] = None):
    """Habilita ou desabilita um canal. Canal desabilitado não é lido pelo Delfos nem assinado pela bridge."""
    try:
        config_store.update_channel_enabled(channel, body.enabled, device_id=device_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await _publish_config_reload(device_id)
    return {'channel': channel, 'enabled': body.enabled}


class ChannelRename(BaseModel):
    new_name:  str
    device_id: str


@app.patch('/api/channels/{channel}/rename')
async def rename_channel(channel: str, body: ChannelRename):
    """Renomeia um canal dentro de um device, migrando overrides."""
    try:
        config_store.rename_channel(channel, body.new_name, body.device_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _publish_config_reload(body.device_id)
    return {'old_name': channel, 'new_name': body.new_name, 'device_id': body.device_id}


# ── Devices ───────────────────────────────────────────────────────────────────

class DeviceCreate(BaseModel):
    device_id:   str
    label:       str       = ''
    protocol:    str       = 'tcp'
    host:        str       = ''
    port:        int       = 502
    unit_id:     int       = 1
    serial_port: str       = ''
    baudrate:    int       = 9600
    parity:      str       = 'N'
    stopbits:    int       = 1
    csv_files:   list[str] = []


class DevicePatch(BaseModel):
    enabled:     Optional[bool]      = None
    label:       Optional[str]       = None
    protocol:    Optional[str]       = None
    host:        Optional[str]       = None
    port:        Optional[int]       = None
    unit_id:     Optional[int]       = None
    serial_port: Optional[str]       = None
    baudrate:    Optional[int]       = None
    parity:      Optional[str]       = None
    stopbits:    Optional[int]       = None
    csv_files:   Optional[list[str]] = None


@app.get('/api/devices')
async def get_devices():
    """Retorna {device_id: cfg} para todos os devices configurados."""
    return config_store.get_devices()


@app.post('/api/devices', status_code=201)
async def create_device(body: DeviceCreate):
    """Cria um novo device em group_config['devices']."""
    device_id = body.device_id.strip()
    if not device_id:
        raise HTTPException(status_code=422, detail='device_id não pode ser vazio.')
    cfg = body.model_dump(exclude={'device_id'})
    config_store.create_device(device_id, cfg)
    return {'device_id': device_id, **cfg}


@app.patch('/api/devices/{device_id}')
async def patch_device(device_id: str, body: DevicePatch):
    """Atualiza campos de um device existente."""
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail='Nenhum campo fornecido.')
    try:
        config_store.update_device(device_id, fields)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {'device_id': device_id, 'updated': fields}


@app.delete('/api/devices/{device_id}')
async def delete_device(device_id: str):
    """Remove um device."""
    try:
        config_store.delete_device(device_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {'deleted': device_id}


def _do_ping(cfg: dict) -> dict:
    """
    Testa conectividade com um device Modbus (bloqueante — executar em thread pool).
    TCP: usa pyModbusTCP.  RTU: usa pymodbus.client.ModbusSerialClient.
    Retorna {'ok': bool, 'latency_ms': float | None, 'error': str | None}
    """
    protocol = cfg.get('protocol', 'tcp')
    t0 = time.monotonic()
    try:
        if protocol == 'rtu':
            from pymodbus.client import ModbusSerialClient
            client = ModbusSerialClient(
                port=cfg.get('serial_port', ''),
                baudrate=cfg.get('baudrate', 9600),
                parity=cfg.get('parity', 'N'),
                stopbits=cfg.get('stopbits', 1),
            )
            connected = client.connect()
            if not connected:
                return {'ok': False, 'latency_ms': None, 'error': 'Falha ao conectar (RTU)'}
            result = client.read_holding_registers(0, 1, slave=cfg.get('unit_id', 1))
            client.close()
        else:
            from pyModbusTCP.client import ModbusClient
            client = ModbusClient(
                host=cfg.get('host', ''),
                port=cfg.get('port', 502),
                unit_id=cfg.get('unit_id', 1),
                auto_open=True,
                timeout=3,
            )
            result = client.read_holding_registers(0, 1)
            client.close()

        latency = round((time.monotonic() - t0) * 1000, 2)
        ok = result is not None
        return {'ok': ok, 'latency_ms': latency if ok else None,
                'error': None if ok else 'Sem resposta do device'}
    except Exception as exc:
        latency = round((time.monotonic() - t0) * 1000, 2)
        return {'ok': False, 'latency_ms': latency, 'error': str(exc)}


@app.post('/api/devices/{device_id}/ping')
async def ping_device(device_id: str):
    """Testa conectividade com um device e retorna latência."""
    devices = config_store.get_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' não encontrado.")
    cfg = devices[device_id]
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_thread_pool, _do_ping, cfg)
    return result


@app.post('/api/devices/{device_id}/toggle')
async def toggle_device(device_id: str):
    """
    Alterna o campo 'enabled' do device.
    enabled=True  → device ativo: Delfos lê seus CSVs.
    enabled=False → device pausado: Delfos ignora seus CSVs.
    Publica config_reload para que Delfos recarregue imediatamente.
    """
    devices = config_store.get_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' não encontrado.")
    current_enabled = devices[device_id].get('enabled', True)
    new_enabled = not current_enabled
    config_store.update_device(device_id, {'enabled': new_enabled})
    await _publish_config_reload()
    return {'device_id': device_id, 'enabled': new_enabled}


@app.post('/api/devices/{device_id}/clear')
async def clear_device(device_id: str, delete_files: bool = False):
    """
    Remove os overrides de todas as variáveis associadas ao device.
    delete_files=true → remove também os arquivos CSV do disco e limpa csv_files.
    Publica config_reload para que Delfos recarregue.
    """
    devices = config_store.get_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' não encontrado.")

    dev_cfg = devices[device_id]

    # Remove per-device overrides file (reset to empty)
    overrides = config_store.load_overrides(device_id)
    tags_to_clear = list(overrides.keys())
    config_store.save_overrides({}, device_id)
    logger.info("Overrides de %d tag(s) do device '%s' removidos.", len(tags_to_clear), device_id)

    files_deleted = []
    if delete_files:
        for fname in dev_cfg.get('csv_files', []):
            fpath = os.path.join(config_store._TABLES_DIR, fname)
            try:
                os.remove(fpath)
                files_deleted.append(fname)
                logger.info("CSV '%s' removido.", fpath)
            except FileNotFoundError:
                pass
        config_store.update_device(device_id, {'csv_files': []})

    await _publish_config_reload()

    return {
        'device_id':     device_id,
        'cleared_tags':  len(tags_to_clear),
        'files_deleted': files_deleted,
    }


@app.post('/api/devices/{device_id}/upload-csv')
async def upload_device_csv(device_id: str, file: UploadFile = File(...)):
    """
    Faz upload de um CSV de mapeamento Modbus e associa ao device.
    O arquivo é salvo em tables/ e adicionado a device.csv_files (se ainda não estiver).
    Publica config_reload para que Delfos recarregue sem reiniciar.
    """
    devices = config_store.get_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' não encontrado.")

    content = await file.read()

    # Sanitiza o nome do arquivo — sem path traversal
    filename = os.path.basename(file.filename or f'{device_id}.csv')
    if not filename.lower().endswith('.csv'):
        raise HTTPException(status_code=422, detail='Somente arquivos .csv são aceitos aqui.')

    save_path = os.path.join(config_store._TABLES_DIR, filename)
    with open(save_path, 'wb') as f:
        f.write(content)
    logger.info("CSV '%s' salvo em '%s'.", filename, save_path)

    # Adiciona à lista csv_files do device se não estiver presente
    dev_cfg   = devices[device_id]
    csv_files = list(dev_cfg.get('csv_files', []))
    if filename not in csv_files:
        csv_files.append(filename)
        config_store.update_device(device_id, {'csv_files': csv_files})

    await _publish_config_reload()

    return {'device_id': device_id, 'filename': filename, 'csv_files': csv_files}


@app.delete('/api/devices/{device_id}/csv/{filename:path}')
async def remove_device_csv(device_id: str, filename: str, delete_file: bool = False):
    """
    Remove um CSV da lista csv_files do device.
    Remove overrides de tags que pertenciam exclusivamente a esse CSV.
    delete_file=true → apaga o arquivo do disco.
    Publica config_reload para que Delfos recarregue.
    """
    devices = config_store.get_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' não encontrado.")

    dev_cfg = devices[device_id]
    csv_files = list(dev_cfg.get('csv_files', []))
    # Sanitiza — impede path traversal
    safe_name = os.path.basename(filename)
    if safe_name not in csv_files:
        raise HTTPException(status_code=404, detail=f"CSV '{safe_name}' não está associado ao device '{device_id}'.")

    # Coleta tags do CSV a ser removido para limpar overrides
    csv_path = os.path.join(config_store._TABLES_DIR, safe_name)
    tags_removed = []
    if os.path.exists(csv_path):
        try:
            import pandas as pd
            df = pd.read_csv(csv_path, sep=',')
            if 'ObjecTag' in df.columns:
                csv_tags = set(df['ObjecTag'].astype(str).str.strip().values)
                overrides = config_store.load_overrides(device_id)
                tags_to_clean = [t for t in csv_tags if t in overrides]
                for tag in tags_to_clean:
                    del overrides[tag]
                    tags_removed.append(tag)
                if tags_to_clean:
                    config_store.save_overrides(overrides, device_id)
        except Exception:
            pass  # CSV ilegível — prossegue sem limpar overrides

    csv_files.remove(safe_name)
    config_store.update_device(device_id, {'csv_files': csv_files})

    file_deleted = False
    if delete_file and os.path.exists(csv_path):
        os.remove(csv_path)
        file_deleted = True
        logger.info("CSV '%s' removido do disco.", csv_path)

    await _publish_config_reload(device_id)

    return {
        'device_id': device_id,
        'removed': safe_name,
        'csv_files': csv_files,
        'tags_cleaned': len(tags_removed),
        'file_deleted': file_deleted,
    }


# ── Scanner — Leitura individual de variáveis ─────────────────────────────────

class ScanStartBody(BaseModel):
    interval_ms: int = 50
    retries: int = 3
    channel: Optional[str] = None


@app.post('/api/devices/{device_id}/scan')
async def start_scan(device_id: str, body: ScanStartBody):
    """Inicia scan de variáveis do device. 409 se Delfos rodando ou scan ativo."""
    if not scan_manager:
        raise HTTPException(status_code=503, detail='ScannerManager não inicializado.')

    devices = config_store.get_devices()
    if device_id not in devices:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' não encontrado.")

    # Verifica se Delfos está rodando para este device
    if proc_manager:
        proc_id = f"delfos:{device_id}"
        proc = proc_manager.get_process(proc_id)
        if proc and proc.running:
            raise HTTPException(status_code=409, detail=f"Delfos rodando para device '{device_id}'. Pare o Delfos primeiro.")

    # Verifica scan já em andamento
    if scan_manager.is_scanning(device_id):
        raise HTTPException(status_code=409, detail=f"Scan já em andamento para device '{device_id}'.")

    dev_cfg = devices[device_id]
    variables = config_store.load_device_variables(device_id, channel_filter=body.channel or None)

    if not variables:
        raise HTTPException(status_code=422, detail='Nenhuma variável encontrada para este device/canal.')

    scan_config = {
        'interval_ms': body.interval_ms,
        'retries': body.retries,
        'channel_filter': body.channel,
    }

    async def _progress_cb(event: str, data: dict) -> None:
        await sio.emit(event, data, room=f'scan:{device_id}')

    session = scan_manager.start_scan(
        device_id=device_id,
        device_cfg=dev_cfg,
        variables=variables,
        config=scan_config,
        progress_callback=_progress_cb,
    )
    return session.to_summary()


@app.post('/api/devices/{device_id}/scan/cancel')
async def cancel_scan(device_id: str):
    """Cancela scan em andamento."""
    if not scan_manager:
        raise HTTPException(status_code=503, detail='ScannerManager não inicializado.')
    session = scan_manager.cancel_scan(device_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Nenhum scan encontrado para device '{device_id}'.")
    return session.to_summary()


@app.get('/api/devices/{device_id}/scan')
async def get_scan(device_id: str):
    """Retorna sessão de scan atual/última com resultados."""
    if not scan_manager:
        raise HTTPException(status_code=503, detail='ScannerManager não inicializado.')
    session = scan_manager.get_scan(device_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Nenhum scan encontrado para device '{device_id}'.")
    return session.to_dict()


@app.get('/api/devices/{device_id}/scan/results')
async def get_scan_results(device_id: str):
    """Retorna {tag: {status, latency_ms, error, value}} para o AG Grid."""
    if not scan_manager:
        return {}
    return scan_manager.get_results_for_grid(device_id)


# ── LabTest — Simuladores embarcados ──────────────────────────────────────────

@app.get('/labtest')
async def labtest_page():
    """Serve a página LabTest."""
    return FileResponse(os.path.join(_HUB_DIR, 'templates', 'labtest.html'))


@app.get('/monitor')
async def monitor_page():
    """Serve a página Monitor — visualizador de dados Redis/Socket.IO em tempo real."""
    return FileResponse(os.path.join(_HUB_DIR, 'templates', 'monitor.html'))


@app.get('/api/simulators')
async def list_simulators():
    """Lista todos os simuladores com estado."""
    if not sim_manager:
        return {}
    return sim_manager.list_simulators()


class SimulatorCreate(BaseModel):
    sim_id:       str
    label:        str       = ''
    protocol:     str       = 'tcp'
    port:         int       = 5020
    unit_id:      int       = 1
    csv_files:    list[str] = []
    simulate:     bool      = True
    auto_start:   bool      = False
    sim_interval:  float     = 2.0
    sim_registers: int       = 8
    sim_coils:     int       = 12
    sim_coil_prob: float     = 0.3


@app.post('/api/simulators', status_code=201)
async def create_simulator(body: SimulatorCreate):
    """Cria um novo simulador Modbus."""
    if not sim_manager:
        raise HTTPException(status_code=503, detail='SimulatorManager não inicializado.')
    sim_id = body.sim_id.strip()
    if not sim_id:
        raise HTTPException(status_code=422, detail='sim_id não pode ser vazio.')
    cfg = body.model_dump(exclude={'sim_id'})
    try:
        sim = sim_manager.create_simulator(sim_id, cfg)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return sim.to_state_dict()


@app.delete('/api/simulators/{sim_id}')
async def delete_simulator(sim_id: str):
    """Remove simulador (para primeiro se rodando)."""
    if not sim_manager:
        raise HTTPException(status_code=503, detail='SimulatorManager não inicializado.')
    try:
        await sim_manager.delete_simulator(sim_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await sio.emit('sim:status', {'sim_id': sim_id, 'running': False, 'deleted': True})
    return {'deleted': sim_id}


class SimulatorPatch(BaseModel):
    label:         Optional[str]       = None
    protocol:      Optional[str]       = None
    port:          Optional[int]       = None
    unit_id:       Optional[int]       = None
    csv_files:     Optional[list[str]] = None
    simulate:      Optional[bool]      = None
    auto_start:    Optional[bool]      = None
    sim_interval:  Optional[float]     = None
    sim_registers: Optional[int]       = None
    sim_coils:     Optional[int]       = None
    sim_coil_prob: Optional[float]     = None


@app.patch('/api/simulators/{sim_id}')
async def patch_simulator(sim_id: str, body: SimulatorPatch):
    """Atualiza config do simulador (deve estar parado)."""
    if not sim_manager:
        raise HTTPException(status_code=503, detail='SimulatorManager não inicializado.')
    sim = sim_manager.get_simulator(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail=f"Simulador '{sim_id}' não encontrado.")
    if sim.running:
        raise HTTPException(status_code=409, detail='Pare o simulador antes de alterar a configuração.')
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail='Nenhum campo fornecido.')
    sim.config.update(fields)
    # Reconstrói contexto se csv_files mudou
    if 'csv_files' in fields:
        sim.build_context(config_store._TABLES_DIR)
    sim_manager.save_config()
    return sim.to_state_dict()


@app.post('/api/simulators/{sim_id}/start')
async def start_simulator(sim_id: str):
    """Inicia o simulador Modbus."""
    if not sim_manager:
        raise HTTPException(status_code=503, detail='SimulatorManager não inicializado.')
    try:
        await sim_manager.start_simulator(sim_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=409, detail=f'Erro ao iniciar (porta em uso?): {e}')
    sim = sim_manager.get_simulator(sim_id)
    state = sim.to_state_dict() if sim else {'sim_id': sim_id, 'running': True}
    await sio.emit('sim:status', state)
    return state


@app.post('/api/simulators/{sim_id}/stop')
async def stop_simulator(sim_id: str):
    """Para o simulador Modbus."""
    if not sim_manager:
        raise HTTPException(status_code=503, detail='SimulatorManager não inicializado.')
    try:
        await sim_manager.stop_simulator(sim_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    sim = sim_manager.get_simulator(sim_id)
    state = sim.to_state_dict() if sim else {'sim_id': sim_id, 'running': False}
    await sio.emit('sim:status', state)
    return state


@app.get('/api/simulators/{sim_id}/variables')
async def get_simulator_variables(sim_id: str):
    """Lista variáveis com valores atuais e estado de lock."""
    if not sim_manager:
        raise HTTPException(status_code=503, detail='SimulatorManager não inicializado.')
    sim = sim_manager.get_simulator(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail=f"Simulador '{sim_id}' não encontrado.")
    return {'variables': sim.get_variables_info()}


@app.post('/api/simulators/{sim_id}/upload-csv')
async def upload_simulator_csv(sim_id: str, file: UploadFile = File(...)):
    """Upload CSV para o simulador — salva em tables/ e adiciona a csv_files."""
    if not sim_manager:
        raise HTTPException(status_code=503, detail='SimulatorManager não inicializado.')
    sim = sim_manager.get_simulator(sim_id)
    if not sim:
        raise HTTPException(status_code=404, detail=f"Simulador '{sim_id}' não encontrado.")
    if sim.running:
        raise HTTPException(status_code=409, detail='Pare o simulador antes de fazer upload de CSV.')

    content = await file.read()
    filename = os.path.basename(file.filename or f'{sim_id}.csv')
    if not filename.lower().endswith('.csv'):
        raise HTTPException(status_code=422, detail='Somente arquivos .csv são aceitos.')

    save_path = os.path.join(config_store._TABLES_DIR, filename)
    with open(save_path, 'wb') as f:
        f.write(content)

    csv_files = list(sim.config.get('csv_files', []))
    if filename not in csv_files:
        csv_files.append(filename)
        sim.config['csv_files'] = csv_files

    sim.build_context(config_store._TABLES_DIR)
    sim_manager.save_config()
    return {'sim_id': sim_id, 'filename': filename, 'csv_files': csv_files}


# ── Eventos Socket.IO ─────────────────────────────────────────────────────────

@sio.event
async def connect(sid, environ):
    logger.info('Cliente conectado: %s', sid)
    await sio.emit('connection_ack', {
        'status': 'connected',
        'available_rooms': _derive_rooms(),
    }, to=sid)


@sio.event
async def disconnect(sid):
    logger.info('Cliente desconectado: %s', sid)


@sio.event
async def join(sid, data):
    """
    Cliente entra em um ou mais rooms para receber apenas os dados relevantes.
    Payload: {"rooms": ["alarmes", "process"]}
    """
    rooms = data.get('rooms', []) if isinstance(data, dict) else []
    for room in rooms:
        await sio.enter_room(sid, room)
    logger.info("Cliente %s entrou nos rooms: %s", sid, rooms)


@sio.event
async def plc_write(sid, data):
    """
    Encaminha comando de escrita ao CLP via Redis → Atena.
    Payload: {"Extrusora": {"extrusoraRefVelocidade": 1450}}
    """
    if redis_pub:
        await redis_pub.publish('plc_commands', json.dumps(data))
        logger.info("plc_write de %s: %s", sid, data)


@sio.event
async def user_status(sid, data):
    """
    Atualiza estado do usuário (liga/desliga loop do Delfos e escrita do Atena).
    Payload: {"user_state": true}
    """
    if redis_pub:
        await redis_pub.publish('user_status', json.dumps(data))
        logger.info("user_status de %s: %s", sid, data)


@sio.event
async def config_save(sid, data):
    """
    Salva group_config.json com o payload recebido e notifica Delfos via
    config_reload. Faz broadcast de config:updated para todos os clientes.
    Payload: conteúdo completo de group_config.json
    """
    config_store.save_group_config(data)
    await _publish_config_reload()
    await sio.emit('config:updated', data)
    logger.info("config:save de %s — group_config.json atualizado.", sid)


@sio.event
async def config_get(sid, data=None):
    """Envia a configuração atual para o cliente solicitante."""
    cfg = config_store.load_group_config()
    await sio.emit('config:updated', cfg, to=sid)


@sio.event
async def history_set(sid, data):
    """
    Atualiza history_size de um canal e aplica ltrim imediato no Redis.
    Payload: {"channel": "plc_alarmes", "size": 50}
    """
    channel = data.get('channel') if isinstance(data, dict) else None
    size    = data.get('size')    if isinstance(data, dict) else None

    if not channel or not isinstance(size, int) or size < 1:
        logger.warning("history_set inválido de %s: %s", sid, data)
        return

    config_store.update_channel_history_size(channel, size)

    if redis_pub:
        await redis_pub.ltrim(f'history:{channel}', 0, size - 1)

    await _publish_config_reload()
    updated_cfg = config_store.load_group_config()
    await sio.emit('config:updated', updated_cfg)
    logger.info("history_set: canal='%s' size=%d — broadcast enviado.", channel, size)


@sio.event
async def history_get(sid, data=None):
    """Envia {canal: history_size} para o cliente solicitante."""
    sizes = config_store.get_channel_history_sizes()
    await sio.emit('history:sizes', sizes, to=sid)


# ── Eventos Socket.IO — Simuladores ──────────────────────────────────────────

@sio.event
async def sim_subscribe(sid, data):
    """Cliente entra no room sim:{sim_id} para receber valores em tempo real."""
    sim_id = data.get('sim_id') if isinstance(data, dict) else None
    if sim_id:
        await sio.enter_room(sid, f'sim:{sim_id}')
        logger.info("Cliente %s entrou no room sim:%s", sid, sim_id)


@sio.event
async def sim_write(sid, data):
    """Escreve valor no data store do simulador e trava a tag automaticamente."""
    if not sim_manager or not isinstance(data, dict):
        return
    sim_id = data.get('sim_id')
    tag = data.get('tag')
    value = data.get('value')
    if not sim_id or not tag or value is None:
        return
    sim = sim_manager.get_simulator(sim_id)
    if not sim:
        return
    result = sim.write_value(tag, value)
    if result:
        # Confirma escrita e lock ao client que solicitou
        await sio.emit('sim:write_ack', {
            'sim_id': sim_id,
            'tag': tag,
            'value': result['value'],
            'address': result['address'],
            'locked': True,
        }, to=sid)


@sio.event
async def sim_lock(sid, data):
    """Trava/destrava variável do simulador."""
    if not sim_manager or not isinstance(data, dict):
        return
    sim_id = data.get('sim_id')
    tag = data.get('tag')
    locked = data.get('locked', True)
    if not sim_id or not tag:
        return
    sim = sim_manager.get_simulator(sim_id)
    if sim:
        if locked:
            sim.lock_tag(tag)
        else:
            sim.unlock_tag(tag)


# ── Eventos Socket.IO — Scanner ──────────────────────────────────────────────

@sio.event
async def scan_subscribe(sid, data):
    """Cliente entra no room scan:{device_id} para receber progresso do scan."""
    device_id = data.get('device_id') if isinstance(data, dict) else None
    if device_id:
        await sio.enter_room(sid, f'scan:{device_id}')
        logger.info("Cliente %s entrou no room scan:%s", sid, device_id)
