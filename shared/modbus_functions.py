import logging
import os
from collections import defaultdict
from time import sleep
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_MODBUS_HOST     = os.environ.get('MODBUS_HOST', '192.168.1.2')
_MODBUS_PORT     = int(os.environ.get('MODBUS_PORT', 502))
_MODBUS_UNIT_ID  = int(os.environ.get('MODBUS_UNIT_ID', 2))
_MODBUS_PROTOCOL = os.environ.get('MODBUS_PROTOCOL', 'tcp')


class ModbusClientWrapper:
    """Wrapper que unifica a API de pyModbusTCP (TCP puro) e pymodbus (RTU over TCP)."""

    def __init__(self, client, protocol, unit_id):
        self._client = client
        self._protocol = protocol
        self._unit_id = unit_id

    def read_coils(self, address, count):
        if self._protocol == 'tcp':
            return self._client.read_coils(address, count)
        result = self._client.read_coils(address, count, slave=self._unit_id)
        if result.isError():
            raise Exception(f"Modbus RTU error (read_coils addr={address}): {result}")
        return result.bits[:count]

    def read_holding_registers(self, address, count):
        if self._protocol == 'tcp':
            return self._client.read_holding_registers(address, count)
        result = self._client.read_holding_registers(address, count, slave=self._unit_id)
        if result.isError():
            raise Exception(f"Modbus RTU error (read_holding_registers addr={address}): {result}")
        return result.registers[:count]

    def write_single_coil(self, address, value):
        if self._protocol == 'tcp':
            return self._client.write_single_coil(address, value)
        result = self._client.write_coil(address, bool(value), slave=self._unit_id)
        if result.isError():
            raise Exception(f"Modbus RTU error (write_coil addr={address}): {result}")
        return result

    def write_single_register(self, address, value):
        if self._protocol == 'tcp':
            return self._client.write_single_register(address, value)
        result = self._client.write_register(address, int(value), slave=self._unit_id)
        if result.isError():
            raise Exception(f"Modbus RTU error (write_register addr={address}): {result}")
        return result

    def open(self):
        if self._protocol == 'tcp':
            return self._client.open()
        return self._client.connect()

    def close(self):
        return self._client.close()


def setup_modbus(protocol=None):
    if protocol is None:
        protocol = _MODBUS_PROTOCOL

    host = _MODBUS_HOST
    port = _MODBUS_PORT
    unit_id = _MODBUS_UNIT_ID
    attempts = 3
    delay = 1

    for _ in range(attempts):
        try:
            if protocol == 'rtu_tcp':
                from pymodbus.client import ModbusTcpClient
                from pymodbus.framer import ModbusRtuFramer
                raw_client = ModbusTcpClient(host, port=port, framer=ModbusRtuFramer)
                connected = raw_client.connect()
                if not connected:
                    raise ConnectionError(f"Falha ao conectar via RTU over TCP em {host}:{port}")
                client = ModbusClientWrapper(raw_client, 'rtu_tcp', unit_id)
                logger.info("Modbus RTU over TCP conectado em %s:%s (unit_id=%s)", host, port, unit_id)
            else:
                from pyModbusTCP.client import ModbusClient
                raw_client = ModbusClient(host, port, unit_id=unit_id, auto_open=True)
                raw_client.open()
                client = ModbusClientWrapper(raw_client, 'tcp', unit_id)
                logger.info("Modbus TCP conectado em %s:%s (unit_id=%s)", host, port, unit_id)
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


def read_registers_with_bits(client, groups, tags, keys, bit_vars=None):
    """Lê holding registers e extrai bits quando bit_vars indica.

    bit_vars: {register_addr: [{'tag': str, 'key': str, 'bit': int}, ...]}
    Quando um endereço está em bit_vars, o valor do registrador é decomposto
    em bits individuais. Caso contrário, comportamento idêntico a read_registers().
    """
    devices_data = defaultdict(dict)
    total_registers_read = 0

    for group, g_tags, g_keys in zip(groups, tags, keys):
        try:
            first_address = group[0]
            num_addresses = len(group)
            result = client.read_holding_registers(first_address, num_addresses)

            for addr, key, tag, value in zip(group, g_keys, g_tags, result):
                if bit_vars and addr in bit_vars:
                    for bv in bit_vars[addr]:
                        devices_data[bv['key']][bv['tag']] = bool((value >> bv['bit']) & 1)
                else:
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
