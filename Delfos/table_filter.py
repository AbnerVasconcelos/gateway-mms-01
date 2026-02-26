import pandas as pd
import itertools

import time

def find_contiguous_groups(file_path, attempts=5, pause=5):
    for _ in range(attempts):
        try:
            # Carrega a planilha csv
            df = pd.read_csv(file_path, sep=',')
            break  # Se conseguiu ler o arquivo, sai do loop
        except Exception as e:
            print(f"Erro ao tentar ler o arquivo .csv: {e}")
            print(f"Tentando novamente em {pause} segundos.")
            time.sleep(pause)
    else:  # Se esgotou todas as tentativas e não conseguiu ler o arquivo
        print(f"Não foi possível ler o arquivo após {attempts} tentativas.")
        return None, None, None, None, None, None


    # Remove as linhas onde a coluna 'Modbus' é vazia ou NaN
    df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])

    # Cria dois DataFrames: um para 'coils' e outro para 'registers'
    df_coils = df[df['At'] == '%MB']
    df_registers = df[df['At'] != '%MB']

    # Função para encontrar grupos contíguos em uma lista
    def find_contiguous(lst):
        groups = []
        for k, g in itertools.groupby(enumerate(lst), lambda x: x[1] - x[0]):
            group = [int(x[1]) for x in g]  # Convert elements to integers
            groups.append(group)
        return groups

    # Define as variáveis 'coils_groups' e 'registers_groups'
    coils_groups = find_contiguous(df_coils['Modbus'].tolist())
    registers_groups = find_contiguous(df_registers['Modbus'].tolist())

    coils_tags = [df_coils.loc[df_coils['Modbus'].isin(group)]['ObjecTag'].tolist() for group in coils_groups]
    registers_tags = [df_registers.loc[df_registers['Modbus'].isin(group)]['ObjecTag'].tolist() for group in registers_groups]
    coils_keys = [df_coils.loc[df_coils['Modbus'].isin(group)]['key'].tolist() for group in coils_groups]
    registers_keys = [df_registers.loc[df_registers['Modbus'].isin(group)]['key'].tolist() for group in registers_groups]

    return coils_groups, registers_groups, coils_tags, registers_tags, coils_keys, registers_keys

def extract_parameters_from_csv(csv_file):
    
    coils_groups1, registers_groups1, coils_tags1, registers_tags1, coils_keys1, registers_keys1 = find_contiguous_groups(csv_file)

    return coils_groups1, registers_groups1, coils_tags1, registers_tags1, coils_keys1, registers_keys1
