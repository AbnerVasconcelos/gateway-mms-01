import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.modbus_functions import write_coils_to_device, write_registers_to_device
from table_filter import find_values_by_object_tag

logger = logging.getLogger(__name__)


def handle_plc_commands_message(message, user_state, client, csv_path):
    if user_state:
        write_data, timestamp = get_write_data(message)
        matching_modbus_coils, matching_values_coils, matching_modbus_registers, matching_values_registers = find_values_by_object_tag(csv_path, write_data)

        logger.info("Coils — endereços: %s | valores: %s", matching_modbus_coils, matching_values_coils)
        logger.info("Registers — endereços: %s | valores: %s", matching_modbus_registers, matching_values_registers)

        write_coils_to_device(client, matching_modbus_coils, matching_values_coils)
        write_registers_to_device(client, matching_modbus_registers, matching_values_registers)
        logger.info("Dados escritos com sucesso no CLP.")


def handle_user_status_message(message):
    user_state, _ = get_user_state(message)
    return user_state


def handle_ia_status_message(message):
    ia_mode, _ = get_ia_mode(message)
    return ia_mode


def handle_ia_data_message(message, ia_mode):
    if ia_mode:
        ia_data, timestamp = get_ia_data(message)
        # TODO: implementar lógica de processamento dos dados da IA
        logger.warning("handle_ia_data_message: recebido mas sem implementação. Dados: %s", ia_data)


def get_write_data(message):
    write_data = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()
    return write_data, timestamp


def get_user_state(message):
    user_data = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()
    return user_data.get("user_state"), timestamp


def get_ia_mode(message):
    ia_state = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()
    return ia_state.get("ia_state"), timestamp


def get_ia_data(message):
    ia_data = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()
    return ia_data, timestamp
