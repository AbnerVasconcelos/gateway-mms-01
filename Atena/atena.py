#!/usr/bin/env python3
import logging
import os
import sys
from time import sleep
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.redis_config_functions import subscribe_to_channels, setup_redis
from shared.modbus_functions import setup_modbus
from data_handle import (
                        handle_channel1_message,
                        handle_channel3_message,
                        handle_channel5_message,
                        handle_channel7_message,
                         )

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

_TABLES_DIR = os.environ.get('TABLES_DIR', '../tables')


def main():
    csv_path = os.path.join(_TABLES_DIR, 'operacao.csv')

    channels = ['channel1', 'channel3', 'channel5', 'channel7']
    r, pubsub = setup_redis()
    subscribe_to_channels(pubsub, channels)

    client = setup_modbus()

    ia_mode    = True
    user_state = True

    while True:
        for message in pubsub.listen():
            if message and message['type'] == 'message':
                channel = message['channel'].decode()

                if channel == 'channel3':
                    handle_channel3_message(message, user_state, client, csv_path)

                elif channel == 'channel1':
                    user_state = handle_channel1_message(message)
                    logger.info("Estado do usuário atualizado: conectado=%s", user_state)

                elif channel == 'channel5':
                    ia_mode = handle_channel5_message(message)
                    logger.info("Modo IA atualizado: %s", ia_mode)

                elif channel == 'channel7':
                    handle_channel7_message(message, ia_mode)
        else:
            sleep(0.1)


if __name__ == "__main__":
    main()
