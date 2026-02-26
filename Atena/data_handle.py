import json
from datetime import datetime

from table_filter import find_values_by_object_tag
from modbus_functions import write_coils_to_device, write_registers_to_device



def handle_channel3_message(message, user_state, client, csv_path):
    if user_state:
        write_data, timestamp = get_write_data(message)
        matching_modbus_coils, matching_values_coils, matching_modbus_registers, matching_values_registers = find_values_by_object_tag(csv_path, write_data)
        
        print("Coils:")
        print("Matching Modbus:", matching_modbus_coils)
        print("Matching Values:", matching_values_coils)

        print("\nRegisters:")
        print("Matching Modbus:", matching_modbus_registers)
        print("Matching Values:", matching_values_registers)

        write_coils_to_device(client, matching_modbus_coils, matching_values_coils)
        write_registers_to_device(client, matching_modbus_registers, matching_values_registers)
        print("Dados escritos com sucesso")
        


def handle_channel1_message(message):###############status user
    user_state, _ = get_user_state(message)
    # ...
    return user_state

def handle_channel5_message(message):##################status ia
    ia_mode, _ = get_ia_mode(message)
    # ...
    return ia_mode

def handle_channel7_message(message, ia_mode):
    if ia_mode:
        ia_data, timestamp = get_ia_data(message)
        # processa a ia_data
        # ...

def get_write_data(message):
    write_data = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()  # timeStamp de teste
    return write_data, timestamp

def get_user_state(message):
    user_data = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()  # timeStamp de teste
    return user_data.get("user_state"), timestamp

def get_ia_mode(message):
    ia_state = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()  # timeStamp de teste
    return ia_state.get("ia_state"), timestamp

def get_ia_data(message):
    ia_data = json.loads(message['data'].decode('utf-8'))
    timestamp = datetime.now()  # timeStamp de teste
    return ia_data, timestamp