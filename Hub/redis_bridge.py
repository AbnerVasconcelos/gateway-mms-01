"""
redis_bridge -- background task que assina canais Redis e encaminha
mensagens para clientes Socket.IO em tempo real.

Assinatura dinamica baseada no channel map de devices:
  get_channel_map() retorna {channel_name: device_id}
  Ex.: {'plc_alarmes': 'sim', 'west_temp': 'west'}

Rooms Socket.IO:
  device_id       -- todos os canais do device (ex.: 'sim')
  device_id:canal -- canal especifico (ex.: 'sim:plc_alarmes')

Reload dinamico:
  Publicar no canal Redis '_bridge_reload' faz a bridge re-avaliar
  o channel map e atualizar as subscriptions sem reiniciar.
"""

import asyncio
import json
import logging

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Pausa entre tentativas de reconexao ao Redis (segundos)
_RECONNECT_DELAY = 2


async def start_bridge(sio, redis_host: str, redis_port: int, get_channel_map=None) -> None:
    """
    Loop de bridge Redis -> Socket.IO com reconexao automatica.
    Deve ser chamado como asyncio.create_task() no startup da aplicacao.

    Args:
        sio: instancia python-socketio AsyncServer
        redis_host: host do Redis
        redis_port: porta do Redis
        get_channel_map: callable que retorna {channel_name: device_id}.
                         Quando None, a bridge nao assina nenhum canal.
    """
    logger.info("Redis bridge: iniciando em %s:%s", redis_host, redis_port)

    while True:
        try:
            await _run_bridge(sio, redis_host, redis_port, get_channel_map)
        except asyncio.CancelledError:
            logger.info("Redis bridge: cancelado.")
            break
        except Exception as e:
            logger.error("Redis bridge: erro inesperado -- %s. Reconectando em %ss.", e, _RECONNECT_DELAY)
            await asyncio.sleep(_RECONNECT_DELAY)


async def _run_bridge(sio, redis_host: str, redis_port: int, get_channel_map) -> None:
    """Conecta ao Redis e encaminha mensagens ate a conexao cair."""
    r = aioredis.Redis(host=redis_host, port=redis_port, db=0)

    # Verifica conexao antes de entrar no loop
    try:
        await r.ping()
    except Exception as e:
        logger.error("Redis bridge: ping falhou -- %s. Tentando novamente em %ss.", e, _RECONNECT_DELAY)
        await r.aclose()
        await asyncio.sleep(_RECONNECT_DELAY)
        return

    channel_map = get_channel_map() if get_channel_map else {}

    logger.info("Redis bridge: conectado. Assinando %d canais + _bridge_reload...",
                len(channel_map))

    async with r.pubsub() as ps:
        # Subscribe to all device channels + reload signal
        if channel_map:
            await ps.subscribe(*channel_map.keys())
        await ps.subscribe('_bridge_reload')

        while True:
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                continue

            if msg['type'] not in ('message', 'pmessage'):
                continue

            channel = msg['channel'].decode()

            # Handle reload signal
            if channel == '_bridge_reload':
                new_map = get_channel_map() if get_channel_map else {}
                old_channels = set(channel_map.keys())
                new_channels = set(new_map.keys())
                to_unsub = old_channels - new_channels
                to_sub = new_channels - old_channels
                if to_unsub:
                    await ps.unsubscribe(*to_unsub)
                if to_sub:
                    await ps.subscribe(*to_sub)
                channel_map = new_map
                logger.info("Bridge: re-subscribed. Channels: %s", list(new_map.keys()))
                continue

            try:
                data = json.loads(msg['data'])
            except Exception as e:
                logger.warning("Bridge: invalid payload in '%s': %s", channel, e)
                continue

            device_id = channel_map.get(channel, 'unknown')

            # Emit to device room
            await sio.emit('device:data', {
                'device_id': device_id,
                'channel': channel,
                'data': data,
            }, room=device_id)

            # Emit to channel-specific room
            await sio.emit('channel:data', data, room=f'{device_id}:{channel}')

            logger.debug("Bridge: '%s' -> device '%s' (rooms: %s, %s:%s)",
                         channel, device_id, device_id, device_id, channel)
