#!/usr/bin/env python3
import logging
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.redis_config_functions import subscribe_to_channels, setup_redis
from shared.modbus_functions import setup_modbus
from data_handle import (
                        handle_user_status_message,
                        handle_plc_commands_message,
                        handle_ia_status_message,
                        handle_ia_data_message,
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
    device_id = os.environ.get('DEVICE_ID')
    if not device_id:
        logger.critical("DEVICE_ID env var obrigatoria. Encerrando.")
        return

    command_channel = os.environ.get('COMMAND_CHANNEL', f'{device_id}_commands')

    import json
    group_config_path = os.path.join(_TABLES_DIR, 'group_config.json')
    try:
        with open(group_config_path) as f:
            group_config = json.load(f)
    except Exception as e:
        logger.critical("Erro ao carregar group_config.json: %s", e)
        return

    device_cfg = group_config.get('devices', {}).get(device_id, {})
    csv_files = device_cfg.get('csv_files', [])
    csv_paths = [os.path.join(_TABLES_DIR, f) for f in csv_files]

    if not csv_paths:
        logger.warning("Device '%s' nao tem csv_files configurados.", device_id)

    channels = ['user_status', command_channel, 'ia_status', 'ia_data']
    r, pubsub = setup_redis()
    if r is None or pubsub is None:
        logger.critical("Falha ao conectar ao Redis. Encerrando.")
        return

    subscribe_to_channels(pubsub, channels)

    _modbus_protocol = os.environ.get('MODBUS_PROTOCOL', 'tcp')
    client = setup_modbus(protocol=_modbus_protocol)
    if client is None:
        logger.critical("Falha ao conectar ao Modbus. Encerrando.")
        return

    logger.info("Atena iniciado para device '%s'. Command channel: '%s'", device_id, command_channel)

    ia_mode    = False
    user_state = False

    for message in pubsub.listen():
        if message and message['type'] == 'message':
            channel = message['channel'].decode()

            if channel == command_channel:
                handle_plc_commands_message(message, user_state, client, csv_paths)

            elif channel == 'user_status':
                user_state = handle_user_status_message(message)
                logger.info("Estado do usuário atualizado: conectado=%s", user_state)

            elif channel == 'ia_status':
                ia_mode = handle_ia_status_message(message)
                logger.info("Modo IA atualizado: %s", ia_mode)

            elif channel == 'ia_data':
                handle_ia_data_message(message, ia_mode)


if __name__ == "__main__":
    main()
