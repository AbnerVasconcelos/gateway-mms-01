import json
import logging
import os
import sys
from datetime import datetime
from time import sleep

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.modbus_functions import write_coils_to_device, write_registers_to_device
from shared.bit_addressing import set_bit
from table_filter import find_values_by_object_tag

logger = logging.getLogger(__name__)


def _write_bit_to_register(client, addr, bit_index, bit_value):
    """Escreve um bit individual em um holding register via read-modify-write.

    1. Lê o valor atual do registrador
    2. Altera o bit especificado
    3. Escreve o novo valor de volta
    """
    attempts = 3
    delay = 0.2

    for _ in range(attempts):
        try:
            current = client.read_holding_registers(addr, 1)
            if current is None:
                raise Exception(f"Sem resposta ao ler registrador {addr}")
            current_val = current[0]
            new_val = set_bit(current_val, bit_index, bit_value)
            client.write_single_register(addr, new_val)
            logger.debug("Bit write: addr=%d bit=%d val=%s (reg: %d -> %d)",
                         addr, bit_index, bit_value, current_val, new_val)
            return
        except Exception as e:
            logger.error("Erro no bit write addr=%d bit=%d: %s, nova tentativa em %ss.",
                         addr, bit_index, e, delay)
            sleep(delay)

    logger.error("Falha no bit write addr=%d bit=%d após %d tentativas.", addr, bit_index, attempts)


def handle_plc_commands_message(message, user_state, client, csv_paths):
    """csv_paths is now a list of paths (backward-compat: also accepts a single string)."""
    if user_state:
        write_data, timestamp = get_write_data(message)

        all_coils_addr, all_coils_vals = [], []
        all_regs_addr, all_regs_vals = [], []
        all_bit_writes = []

        # Support both single path (backward compat) and list
        if isinstance(csv_paths, str):
            csv_paths = [csv_paths]

        for csv_path in csv_paths:
            try:
                c_addr, c_vals, r_addr, r_vals, bit_writes = find_values_by_object_tag(csv_path, write_data)
                all_coils_addr.extend(c_addr)
                all_coils_vals.extend(c_vals)
                all_regs_addr.extend(r_addr)
                all_regs_vals.extend(r_vals)
                all_bit_writes.extend(bit_writes)
            except Exception as e:
                logger.error("Erro ao buscar tags em '%s': %s", csv_path, e)

        logger.info("Coils -- enderecos: %s | valores: %s", all_coils_addr, all_coils_vals)
        logger.info("Registers -- enderecos: %s | valores: %s", all_regs_addr, all_regs_vals)
        if all_bit_writes:
            logger.info("Bit writes -- %d operações", len(all_bit_writes))

        write_coils_to_device(client, all_coils_addr, all_coils_vals)
        write_registers_to_device(client, all_regs_addr, all_regs_vals)

        # Bit writes via read-modify-write
        for addr, bit_index, bit_value in all_bit_writes:
            _write_bit_to_register(client, addr, bit_index, bit_value)

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
