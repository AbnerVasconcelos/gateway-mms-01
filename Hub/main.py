#!/usr/bin/env python3
"""
Hub — Socket.IO bridge + Config Panel REST API.

Inicia com:
    cd Hub && uvicorn main:asgi_app --host 0.0.0.0 --port 8000 --reload
    ou
    uvicorn Hub.main:asgi_app --host 0.0.0.0 --port 8000  (a partir de gateway/)

Variáveis de ambiente (.env):
    REDIS_HOST, REDIS_PORT, TABLES_DIR, HUB_HOST, HUB_PORT
"""

import asyncio
import json
import logging
import os
import sys
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
from redis_bridge import start_bridge  # noqa: E402  (Hub/redis_bridge.py)

load_dotenv(os.path.join(_HUB_DIR, '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

_REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
_REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))

# Rooms conhecidos — derivados dos canais plc_* configurados
KNOWN_ROOMS = ['alarmes', 'process', 'visual', 'config', 'alarms']


# ── Socket.IO + FastAPI ───────────────────────────────────────────────────────

sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',
    logger=False,
    engineio_logger=False,
)
app = FastAPI(title='Gateway Hub', version='2.0.0')

# ASGI app: Socket.IO roteia WebSockets; FastAPI roteia HTTP
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# Redis para publicação de comandos (user_status, plc_commands, config_reload)
redis_pub: aioredis.Redis | None = None


# ── Ciclo de vida ─────────────────────────────────────────────────────────────

@app.on_event('startup')
async def on_startup():
    global redis_pub
    redis_pub = aioredis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=0)
    logger.info('Hub: Redis publisher pronto em %s:%s', _REDIS_HOST, _REDIS_PORT)
    asyncio.create_task(start_bridge(sio, _REDIS_HOST, _REDIS_PORT))
    logger.info('Hub iniciado.')


@app.on_event('shutdown')
async def on_shutdown():
    if redis_pub:
        await redis_pub.aclose()
    logger.info('Hub encerrado.')


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/api/channels')
async def get_channels():
    """Retorna {canal: history_size} para todos os canais configurados."""
    return config_store.get_channel_history_sizes()


@app.get('/api/groups')
async def get_groups():
    """Retorna a seção 'groups' de group_config.json."""
    return config_store.load_group_config().get('groups', {})


@app.get('/')
async def index():
    """Serve o painel de configuração web."""
    return FileResponse(os.path.join(_HUB_DIR, 'templates', 'index.html'))


@app.get('/api/variables')
async def get_variables():
    """Retorna todas as variáveis com configuração mesclada + overrides brutos."""
    return {
        'variables': config_store.load_all_variables(),
        'overrides': config_store.load_overrides(),
    }


class VariablePatch(BaseModel):
    enabled:  Optional[bool]  = None
    channel:  Optional[str]   = None
    delay_ms: Optional[int]   = None


@app.patch('/api/variables/{tag}')
async def patch_variable(tag: str, body: VariablePatch):
    """Atualiza (ou cria) o override de uma variável individual."""
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=422, detail='Nenhum campo fornecido.')
    config_store.patch_variable_override(tag, fields)
    if redis_pub:
        await redis_pub.publish('config_reload', json.dumps({'reload': True}))
    return {'tag': tag, 'updated': fields}


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
    if redis_pub:
        await redis_pub.publish('config_reload', json.dumps({'reload': True}))
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


class HistoryPatch(BaseModel):
    history_size: int


@app.patch('/api/channels/{channel}/history')
async def set_channel_history(channel: str, body: HistoryPatch):
    """
    Atualiza history_size do canal e aplica ltrim imediato nas listas Redis.
    Publica config_reload para que o Delfos recarregue sem reiniciar.
    """
    if body.history_size < 1:
        raise HTTPException(status_code=422, detail='history_size deve ser >= 1')

    config_store.update_channel_history_size(channel, body.history_size)

    if redis_pub:
        # Trunca imediatamente o histórico existente no Redis
        await redis_pub.ltrim(f'history:{channel}', 0, body.history_size - 1)
        await redis_pub.publish('config_reload', json.dumps({'reload': True}))

    return {'channel': channel, 'history_size': body.history_size}


# ── Eventos Socket.IO ─────────────────────────────────────────────────────────

@sio.event
async def connect(sid, environ):
    logger.info('Cliente conectado: %s', sid)
    await sio.emit('connection_ack', {
        'status': 'connected',
        'available_rooms': KNOWN_ROOMS,
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
    if redis_pub:
        await redis_pub.publish('config_reload', json.dumps({'reload': True}))
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
        await redis_pub.publish('config_reload', json.dumps({'reload': True}))

    updated_cfg = config_store.load_group_config()
    await sio.emit('config:updated', updated_cfg)
    logger.info("history_set: canal='%s' size=%d — broadcast enviado.", channel, size)


@sio.event
async def history_get(sid, data=None):
    """Envia {canal: history_size} para o cliente solicitante."""
    sizes = config_store.get_channel_history_sizes()
    await sio.emit('history:sizes', sizes, to=sid)
