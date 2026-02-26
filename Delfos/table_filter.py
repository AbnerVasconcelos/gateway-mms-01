import logging
import itertools
import time
import pandas as pd

logger = logging.getLogger(__name__)


def find_contiguous_groups(file_path, attempts=5, pause=5):
    for _ in range(attempts):
        try:
            df = pd.read_csv(file_path, sep=',')
            break
        except Exception as e:
            logger.error("Erro ao tentar ler o arquivo .csv: %s", e)
            logger.info("Tentando novamente em %s segundos.", pause)
            time.sleep(pause)
    else:
        logger.critical("Não foi possível ler o arquivo após %s tentativas.", attempts)
        return None, None, None, None, None, None

    df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])

    df_coils = df[df['At'] == '%MB']
    df_registers = df[df['At'] != '%MB']

    def find_contiguous(lst):
        groups = []
        for k, g in itertools.groupby(enumerate(lst), lambda x: x[1] - x[0]):
            group = [int(x[1]) for x in g]
            groups.append(group)
        return groups

    coils_groups = find_contiguous(df_coils['Modbus'].tolist())
    registers_groups = find_contiguous(df_registers['Modbus'].tolist())

    coils_tags = [df_coils.loc[df_coils['Modbus'].isin(group)]['ObjecTag'].tolist() for group in coils_groups]
    registers_tags = [df_registers.loc[df_registers['Modbus'].isin(group)]['ObjecTag'].tolist() for group in registers_groups]
    coils_keys = [df_coils.loc[df_coils['Modbus'].isin(group)]['key'].tolist() for group in coils_groups]
    registers_keys = [df_registers.loc[df_registers['Modbus'].isin(group)]['key'].tolist() for group in registers_groups]

    return coils_groups, registers_groups, coils_tags, registers_tags, coils_keys, registers_keys


def extract_parameters_from_csv(csv_file):
    return find_contiguous_groups(csv_file)
