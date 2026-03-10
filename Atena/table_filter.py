import os
import sys

import pandas as pd

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
    df = pd.read_csv(file_path, sep=',', dtype={'Modbus': str})
    df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])

    # Parse endereços Modbus com suporte a bit addressing
    parsed = []
    for _, row in df.iterrows():
        tag = str(row['ObjecTag']).strip()
        at = str(row.get('At', '')).strip()
        modbus_raw = str(row['Modbus']).strip()
        try:
            register_addr, bit_index = parse_modbus_address(modbus_raw)
        except (ValueError, TypeError):
            continue
        parsed.append({
            'tag': tag,
            'at': at,
            'register_addr': register_addr,
            'bit_index': bit_index,
        })

    # Extrair chaves mais profundas e seus valores do objeto JSON
    deep_keys_values = dict(extract_deep_keys(json_object))

    # Build lookup by tag
    tag_lookup = {p['tag']: p for p in parsed}

    # Iniciar arrays para armazenar os resultados
    matching_modbus_coils = []
    matching_values_coils = []
    matching_modbus_registers = []
    matching_values_registers = []
    matching_bit_writes = []  # [(register_addr, bit_index, bool_value)]

    # Comparar as chaves do objeto JSON com as tags parseadas
    for key, value in deep_keys_values.items():
        if key not in tag_lookup:
            continue
        p = tag_lookup[key]

        if p['bit_index'] is not None:
            # Variável bit-addressed: precisa de read-modify-write
            matching_bit_writes.append((p['register_addr'], p['bit_index'], bool(value)))
        elif p['at'] == '%MB':
            # Coil genuíno
            matching_modbus_coils.append(p['register_addr'])
            matching_values_coils.append(value)
        else:
            # Register normal
            matching_modbus_registers.append(p['register_addr'])
            matching_values_registers.append(value)

    return matching_modbus_coils, matching_values_coils, matching_modbus_registers, matching_values_registers, matching_bit_writes
