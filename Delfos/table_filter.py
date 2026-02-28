import logging
import itertools
import time
import pandas as pd

logger = logging.getLogger(__name__)


def _find_contiguous(lst):
    """Agrupa uma lista de inteiros em sublistas de valores contíguos."""
    groups = []
    for _, g in itertools.groupby(enumerate(lst), lambda x: x[1] - x[0]):
        groups.append([int(x[1]) for x in g])
    return groups


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

    df_coils     = df[df['At'] == '%MB']
    df_registers = df[df['At'] != '%MB']

    coils_groups     = _find_contiguous(df_coils['Modbus'].tolist())
    registers_groups = _find_contiguous(df_registers['Modbus'].tolist())

    coils_tags     = [df_coils.loc[df_coils['Modbus'].isin(g)]['ObjecTag'].tolist()     for g in coils_groups]
    registers_tags = [df_registers.loc[df_registers['Modbus'].isin(g)]['ObjecTag'].tolist() for g in registers_groups]
    coils_keys     = [df_coils.loc[df_coils['Modbus'].isin(g)]['key'].tolist()         for g in coils_groups]
    registers_keys = [df_registers.loc[df_registers['Modbus'].isin(g)]['key'].tolist()     for g in registers_groups]

    return coils_groups, registers_groups, coils_tags, registers_tags, coils_keys, registers_keys


def extract_parameters_from_csv(csv_file):
    return find_contiguous_groups(csv_file)


def extract_parameters_by_group(csv_file, attempts=5, pause=5):
    """
    Lê um CSV de mapeamento e retorna um dict keyed pelo nome do grupo (coluna 'key').

    Cada entrada contém os grupos de endereços Modbus contíguos calculados
    dentro do escopo do próprio grupo — sem cruzar com endereços de outros grupos.

    Retorno:
        {
            "Extrusora": {
                "coil_groups": [[2156], [2169, 2170, 2171, 2172, 2173]],
                "reg_groups":  [[39810], [40123]],
                "coil_tags":   [["extrusoraErro"], ["extrusoraAutManEstado", ...]],
                "reg_tags":    [["extrusoraFeedBackSpeed"], ["extrusoraRefVelocidade"]],
                "coil_keys":   [["Extrusora"], ["Extrusora", ...]],
                "reg_keys":    [["Extrusora"], ["Extrusora"]],
            },
            ...
        }
    """
    for _ in range(attempts):
        try:
            df = pd.read_csv(csv_file, sep=',')
            break
        except Exception as e:
            logger.error("Erro ao tentar ler o arquivo .csv: %s", e)
            logger.info("Tentando novamente em %s segundos.", pause)
            time.sleep(pause)
    else:
        logger.critical("Não foi possível ler o arquivo após %s tentativas.", attempts)
        return {}

    df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])
    df['Modbus'] = pd.to_numeric(df['Modbus'], errors='coerce')
    df = df.dropna(subset=['Modbus'])
    df['Modbus'] = df['Modbus'].astype(int)

    result = {}

    for group_name, group_df in df.groupby('key', sort=False):
        df_coils = group_df[group_df['At'] == '%MB'].copy()
        df_regs  = group_df[group_df['At'] != '%MB'].copy()

        coil_groups = _find_contiguous(df_coils['Modbus'].tolist())
        reg_groups  = _find_contiguous(df_regs['Modbus'].tolist())

        coil_tags = [df_coils.loc[df_coils['Modbus'].isin(g)]['ObjecTag'].tolist() for g in coil_groups]
        reg_tags  = [df_regs.loc[df_regs['Modbus'].isin(g)]['ObjecTag'].tolist()   for g in reg_groups]
        coil_keys = [df_coils.loc[df_coils['Modbus'].isin(g)]['key'].tolist()       for g in coil_groups]
        reg_keys  = [df_regs.loc[df_regs['Modbus'].isin(g)]['key'].tolist()         for g in reg_groups]

        result[group_name] = {
            'coil_groups': coil_groups,
            'reg_groups':  reg_groups,
            'coil_tags':   coil_tags,
            'reg_tags':    reg_tags,
            'coil_keys':   coil_keys,
            'reg_keys':    reg_keys,
        }

    logger.info("CSV '%s': %d grupos carregados — %s",
                csv_file, len(result), list(result.keys()))
    return result
