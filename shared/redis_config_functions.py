import logging
import redis
import time
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
_REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))


def setup_redis():
    for _ in range(3):
        try:
            r = redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=0)
            pubsub = r.pubsub()
            return r, pubsub
        except Exception as e:
            logger.error("Erro ao configurar Redis: %s, nova tentativa em 1 segundo.", e)
            time.sleep(1)
    logger.critical("Falha ao configurar Redis após 3 tentativas.")
    return None, None


def publish_to_channel(r, data, channel):
    for _ in range(3):
        try:
            r.publish(channel, data)
            r.set(f"last_message:{channel}", data)
            r.lpush(f"history:{channel}", data)
            r.ltrim(f"history:{channel}", 0, 999)
            break
        except Exception as e:
            logger.error("Erro ao publicar no %s: %s, nova tentativa em 1 segundo.", channel, e)
            time.sleep(1)
    else:
        logger.error("Falha ao publicar no %s após 3 tentativas.", channel)


def subscribe_to_channels(pubsub, channels):
    for _ in range(3):
        try:
            for channel in channels:
                pubsub.subscribe(channel)
            return pubsub
        except Exception as e:
            logger.error("Erro ao subscrever para canais: %s, nova tentativa em 1 segundo.", e)
            time.sleep(1)
    logger.critical("Falha ao subscrever para canais após 3 tentativas.")
    return None


def get_latest_message(pubsub):
    latest_message = None
    while True:
        message = pubsub.get_message()
        if message is None:
            break
        latest_message = message
    return latest_message
