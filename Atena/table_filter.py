import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.bit_addressing import parse_modbus_address


# Função para extrair chaves mais profundas e seus valores do objeto JSON
def extract_deep_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                yield from extract_deep_keys(v)
            else:
                # Verifica se o valor é válido antes de retornar
                if v is not None and v != '' and v != [] and v != {}:
                    yield k, v


def find_values_by_object_tag(file_path, json_object):
    # Carregar arquivo .csv e filtrar as linhas
    with open(file_path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))

    # Parse endereços Modbus com suporte a bit addressing e unit_id
    parsed = []
    for row in rows:
        tag = (row.get('ObjecTag') or '').strip()
        modbus_raw = (row.get('Modbus') or '').strip()
        key = (row.get('key') or '').strip()
        if not tag or not modbus_raw or not key:
            continue
        at = (row.get('At') or '').strip()
        try:
            register_addr, bit_index = parse_modbus_address(modbus_raw)
        except (ValueError, TypeError):
            continue

        # Parse unit_id (only relevant for serial/sniff protocols)
        uid_raw = (row.get('unit_id') or '').strip()
        unit_id = int(uid_raw) if uid_raw else None

        parsed.append({
            'tag': tag,
            'at': at,
            'register_addr': register_addr,
            'bit_index': bit_index,
            'unit_id': unit_id,
        })

    # Extrair chaves mais profundas e seus valores do objeto JSON
    deep_keys_values = dict(extract_deep_keys(json_object))

    # Build lookup by tag
    tag_lookup = {p['tag']: p for p in parsed}

    # Iniciar arrays para armazenar os resultados
    matching_modbus_coils = []
    matching_values_coils = []
    matching_coil_slaves = []
    matching_modbus_registers = []
    matching_values_registers = []
    matching_reg_slaves = []
    matching_bit_writes = []  # [(register_addr, bit_index, bool_value, unit_id)]

    # Comparar as chaves do objeto JSON com as tags parseadas
    for key, value in deep_keys_values.items():
        if key not in tag_lookup:
            continue
        p = tag_lookup[key]

        if p['bit_index'] is not None:
            # Variável bit-addressed: precisa de read-modify-write
            matching_bit_writes.append((p['register_addr'], p['bit_index'], bool(value), p['unit_id']))
        elif p['at'] == '%MB':
            # Coil genuíno
            matching_modbus_coils.append(p['register_addr'])
            matching_values_coils.append(value)
            matching_coil_slaves.append(p['unit_id'])
        else:
            # Register normal
            matching_modbus_registers.append(p['register_addr'])
            matching_values_registers.append(value)
            matching_reg_slaves.append(p['unit_id'])

    return (matching_modbus_coils, matching_values_coils, matching_coil_slaves,
            matching_modbus_registers, matching_values_registers, matching_reg_slaves,
            matching_bit_writes)
