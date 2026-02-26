import logging
import os
from pyModbusTCP.client import ModbusClient
from collections import defaultdict
from time import sleep
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_MODBUS_HOST    = os.environ.get('MODBUS_HOST', '192.168.1.2')
_MODBUS_PORT    = int(os.environ.get('MODBUS_PORT', 502))
_MODBUS_UNIT_ID = int(os.environ.get('MODBUS_UNIT_ID', 2))


def setup_modbus():
    attempts = 3
    delay = 1

    for _ in range(attempts):
        try:
            client = ModbusClient(_MODBUS_HOST, _MODBUS_PORT, unit_id=_MODBUS_UNIT_ID, auto_open=True)
            client.open()
            logger.info("Modbus conectado em %s:%s (unit_id=%s)", _MODBUS_HOST, _MODBUS_PORT, _MODBUS_UNIT_ID)
            return client
        except Exception as e:
            logger.error("Erro ao configurar Modbus: %s, nova tentativa em %ss.", e, delay)
            sleep(delay)

    logger.critical("Falha ao configurar Modbus após %s tentativas.", attempts)
    return None


def read_coils(client, groups, tags, keys):
    devices_data = defaultdict(dict)
    total_coils_read = 0

    for group, tags, keys in zip(groups, tags, keys):
        try:
            first_address = group[0]
            num_addresses = len(group)
            result = client.read_coils(first_address, num_addresses)

            for key, tag, value in zip(keys, tags, result):
                devices_data[key][tag] = bool(value)

            total_coils_read += num_addresses
        except Exception as e:
            logger.error("Erro ao ler bobinas no endereço %s: %s", group[0] if group else '?', e)

    return devices_data, total_coils_read


def read_registers(client, groups, tags, keys):
    devices_data = defaultdict(dict)
    total_registers_read = 0

    for group, tags, keys in zip(groups, tags, keys):
        try:
            first_address = group[0]
            num_addresses = len(group)
            result = client.read_holding_registers(first_address, num_addresses)

            for key, tag, value in zip(keys, tags, result):
                devices_data[key][tag] = value

            total_registers_read += num_addresses
        except Exception as e:
            logger.error("Erro ao ler registros no endereço %s: %s", group[0] if group else '?', e)

    return devices_data, total_registers_read


def write_coils_to_device(client, modbus, values):
    attempts = 3
    delay = 0.2

    for address, value in zip(modbus, values):
        for _ in range(attempts):
            try:
                client.write_single_coil(address, int(value))
                break
            except Exception as e:
                logger.error("Erro ao escrever coil no endereço %s: %s, nova tentativa em %ss.", address, e, delay)
                sleep(delay)
        else:
            logger.error("Falha ao escrever coil no endereço %s após %s tentativas.", address, attempts)


def write_registers_to_device(client, modbus, values):
    attempts = 3
    delay = 0.2

    for address, value in zip(modbus, values):
        for _ in range(attempts):
            try:
                client.write_single_register(address, int(value))
                break
            except Exception as e:
                logger.error("Erro ao escrever registro no endereço %s: %s, nova tentativa em %ss.", address, e, delay)
                sleep(delay)
        else:
            logger.error("Falha ao escrever registro no endereço %s após %s tentativas.", address, attempts)
