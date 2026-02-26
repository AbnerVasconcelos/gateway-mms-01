#!/usr/bin/env python3
import json
import logging
import os
import sys
from time import sleep
import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.redis_config_functions import setup_redis, publish_to_channel, subscribe_to_channels, get_latest_message
from shared.modbus_functions import setup_modbus, read_coils, read_registers
from table_filter import extract_parameters_from_csv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

_TABLES_DIR = os.environ.get('TABLES_DIR', '../tables')


def retry_on_failure(fn, attempts=3, delay=1):
    for _ in range(attempts):
        try:
            return fn()
        except Exception as e:
            logger.warning("Erro: %s, nova tentativa em %ss.", e, delay)
            sleep(delay)
    logger.error("Falha após %s tentativas.", attempts)
    return None


def main():
    csv_read_data   = os.path.join(_TABLES_DIR, 'operacao.csv')
    csv_alarms_data = os.path.join(_TABLES_DIR, 'configuracao.csv')

    try:
        coils_groups1, registers_groups1, coils_tags1, registers_tags1, coils_keys1, registers_keys1 = extract_parameters_from_csv(csv_read_data)
        coils_groups2, registers_groups2, coils_tags2, registers_tags2, coils_keys2, registers_keys2 = extract_parameters_from_csv(csv_alarms_data)
    except FileNotFoundError as e:
        logger.critical("Arquivo CSV não encontrado: %s", e)
        return
    except Exception as e:
        logger.critical("Erro inesperado ao processar arquivos CSV: %s", e)
        return

    channels = ['channel1']
    redis_result = retry_on_failure(setup_redis)
    if redis_result is None:
        return
    r, pubsub = redis_result
    if r is None or pubsub is None:
        return

    subscribe_to_channels(pubsub, channels)

    client = retry_on_failure(setup_modbus)
    if client is None:
        return

    user_state = True
    successful_attempts = 0
    unsuccessful_attempts = 0

    while True:
        publish_time = 1 if user_state else 30

        for _ in range(publish_time):
            message = get_latest_message(pubsub)
            if message and message['type'] == 'message':
                channel = message['channel'].decode()
                if channel == 'channel1':
                    data = json.loads(message['data'].decode())
                    user_state = data['payload']['connected']
                    logger.info("Estado do usuário atualizado: conectado=%s", user_state)
                    break
            sleep(0.5)

        # Leitura de dados operacionais
        try:
            coil_data, total_coils_read = read_coils(client, coils_groups1, coils_tags1, coils_keys1)
            data_coils = coil_data if coil_data else {}
            successful_attempts += 1
        except Exception as e:
            data_coils = {}
            logger.error("Erro ao ler bobinas (coils): %s", e)
            unsuccessful_attempts += 1

        try:
            register_data, total_registers_read = read_registers(client, registers_groups1, registers_tags1, registers_keys1)
            data_registers = register_data if register_data else {}
            successful_attempts += 1
        except Exception as e:
            data_registers = {}
            logger.error("Erro ao ler registros (registers): %s", e)
            unsuccessful_attempts += 1

        # Leitura de dados de alarmes
        try:
            alarms_coil_data, total_alarms_coils = read_coils(client, coils_groups2, coils_tags2, coils_keys2)
            data_alarms_coils = alarms_coil_data if alarms_coil_data else {}
            successful_attempts += 1
        except Exception as e:
            data_alarms_coils = {}
            logger.error("Erro ao ler alarmes das bobinas: %s", e)
            unsuccessful_attempts += 1

        try:
            alarms_register_data, total_alarms_registers = read_registers(client, registers_groups2, registers_tags2, registers_keys2)
            data_alarms_registers = alarms_register_data if alarms_register_data else {}
            successful_attempts += 1
        except Exception as e:
            data_alarms_registers = {}
            logger.error("Erro ao ler alarmes dos registros: %s", e)
            unsuccessful_attempts += 1

        logger.info("Leituras bem-sucedidas: %s | mal-sucedidas: %s", successful_attempts, unsuccessful_attempts)

        read_register_and_coil_data = {
            "coils": data_coils,
            "registers": data_registers,
            "timestamp": datetime.datetime.now().isoformat()
        }

        alarms_register_and_coil_data = {
            "coils": data_alarms_coils,
            "registers": data_alarms_registers,
            "timestamp": datetime.datetime.now().isoformat()
        }

        publish_to_channel(r, json.dumps(read_register_and_coil_data, indent=4), "channel2")
        publish_to_channel(r, json.dumps(alarms_register_and_coil_data, indent=4), "channel4")


if __name__ == "__main__":
    main()
