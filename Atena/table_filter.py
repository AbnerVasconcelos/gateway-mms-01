import pandas as pd
import json

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
    df = pd.read_csv(file_path, sep=',')
    df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])

    # Criar dataframes distintos
    df_coils = df[df['At'] == '%MB'].copy()
    df_coils['Modbus'] = df_coils['Modbus'].astype(int)

    df_registers = df[df['At'] != '%MB'].copy()
    df_registers['Modbus'] = df_registers['Modbus'].astype(int)



    # Extrair chaves mais profundas e seus valores do objeto JSON
    deep_keys_values = dict(extract_deep_keys(json_object))
 

    # Iniciar arrays para armazenar os resultados
    matching_modbus_coils = []
    matching_values_coils = []
    matching_modbus_registers = []
    matching_values_registers = []

    # Comparar as chaves do objeto JSON com a coluna "ObjecTag" nos dataframes
    for key, value in deep_keys_values.items():
        if key in df_coils['ObjecTag'].values:
            matching_modbus_coils.append(df_coils.loc[df_coils['ObjecTag'] == key, 'Modbus'].values[0])
            matching_values_coils.append(value)
        if key in df_registers['ObjecTag'].values:
            matching_modbus_registers.append(df_registers.loc[df_registers['ObjecTag'] == key, 'Modbus'].values[0])
            matching_values_registers.append(value)

    return matching_modbus_coils, matching_values_coils, matching_modbus_registers, matching_values_registers
