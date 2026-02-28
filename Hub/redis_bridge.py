"""
redis_bridge — background task que assina canais Redis e encaminha
mensagens para clientes Socket.IO em tempo real.

Padrão de assinatura:
  psubscribe('plc_*')  → todos os canais segmentados do Delfos
  subscribe('alarms')  → canal legado de alarmes/configuração

Mapeamento canal → room:
  'plc_alarmes'  → room 'alarmes'
  'plc_process'  → room 'process'
  'plc_visual'   → room 'visual'
  'plc_config'   → room 'config'
  'alarms'       → room 'alarms'   (legado)
"""

import asyncio
import json
import logging

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Pausa entre tentativas de reconexão ao Redis (segundos)
_RECONNECT_DELAY = 2


async def start_bridge(sio, redis_host: str, redis_port: int) -> None:
    """
    Loop de bridge Redis → Socket.IO com reconexão automática.
    Deve ser chamado como asyncio.create_task() no startup da aplicação.
    """
    logger.info("Redis bridge: iniciando em %s:%s", redis_host, redis_port)

    while True:
        try:
            await _run_bridge(sio, redis_host, redis_port)
        except asyncio.CancelledError:
            logger.info("Redis bridge: cancelado.")
            break
        except Exception as e:
            logger.error("Redis bridge: erro inesperado — %s. Reconectando em %ss.", e, _RECONNECT_DELAY)
            await asyncio.sleep(_RECONNECT_DELAY)


async def _run_bridge(sio, redis_host: str, redis_port: int) -> None:
    """Conecta ao Redis e encaminha mensagens até a conexão cair."""
    r = aioredis.Redis(host=redis_host, port=redis_port, db=0)

    # Verifica conexão antes de entrar no loop
    try:
        await r.ping()
    except Exception as e:
        logger.error("Redis bridge: ping falhou — %s. Tentando novamente em %ss.", e, _RECONNECT_DELAY)
        await r.aclose()
        await asyncio.sleep(_RECONNECT_DELAY)
        return

    logger.info("Redis bridge: conectado. Assinando plc_* e alarms...")

    async with r.pubsub() as ps:
        await ps.psubscribe('plc_*')
        await ps.subscribe('alarms')

        while True:
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue

            if msg['type'] not in ('message', 'pmessage'):
                continue

            channel = msg['channel'].decode()

            try:
                data = json.loads(msg['data'])
            except Exception as e:
                logger.warning("Bridge: payload inválido em '%s': %s", channel, e)
                continue

            # Deriva o room a partir do nome do canal
            room = channel.removeprefix('plc_') if channel.startswith('plc_') else channel

            await sio.emit('plc:data', {'channel': channel, 'data': data}, room=room)
            logger.debug("Bridge: '%s' → room '%s'", channel, room)
