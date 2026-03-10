import logging
import itertools
import os
import sys
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.bit_addressing import parse_modbus_address, is_bit_addressed

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


# ── Leitura por canal ─────────────────────────────────────────────────────────

def extract_parameters_by_channel(csv_paths, group_config, overrides,
                                   attempts=5, pause=5) -> dict:
    """
    Lê múltiplos CSVs e retorna {channel: {...}} agrupando variáveis pelo canal efetivo.

    Canal efetivo de cada variável:
      overrides[tag]['channel']  — sem fallback; variáveis sem canal explícito são ignoradas.

    Variáveis com enabled=False são excluídas.

    Suporta endereçamento por bit: variáveis com Modbus "1584.01" são interpretadas
    como bit 1 do registrador 1584. Essas variáveis são reclassificadas como leituras
    de holding register (não coils) e agrupadas em 'bit_vars'.

    Retorno por canal:
        {
            'coil_groups': [[addr, ...], ...],
            'reg_groups':  [[addr, ...], ...],
            'coil_tags':   [[tag, ...], ...],
            'reg_tags':    [[tag, ...], ...],
            'coil_keys':   [[group, ...], ...],
            'reg_keys':    [[group, ...], ...],
            'bit_vars':    {1584: [{'tag': ..., 'key': ..., 'bit': 0}, ...], ...},
            'history_size': int,
            'sources':     set{'operacao', 'configuracao'},
        }

    Endereços contíguos são calculados através de TODOS os grupos do canal
    (pool cross-group), reduzindo roundtrips Modbus.
    """
    channels_cfg = group_config.get('channels', {})
    meta         = group_config.get('_meta', {})
    default_hist = meta.get('default_history_size', 100)

    all_rows: list = []

    for csv_path in csv_paths:
        source = os.path.splitext(os.path.basename(csv_path))[0]

        for attempt in range(attempts):
            try:
                df = pd.read_csv(csv_path, sep=',', dtype={'Modbus': str})
                break
            except Exception as e:
                logger.error("Erro ao ler '%s': %s", csv_path, e)
                time.sleep(pause)
        else:
            logger.critical("Não foi possível ler '%s' após %d tentativas.", csv_path, attempts)
            continue

        df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])

        for _, row in df.iterrows():
            modbus_raw = str(row['Modbus']).strip()
            try:
                register_addr, bit_index = parse_modbus_address(modbus_raw)
            except (ValueError, TypeError):
                logger.warning("Endereço Modbus inválido '%s' no CSV '%s', ignorando.", modbus_raw, csv_path)
                continue

            all_rows.append({
                'key':       str(row['key']).strip(),
                'tag':       str(row['ObjecTag']).strip(),
                'at':        str(row.get('At', '')).strip(),
                'modbus':    register_addr,
                'bit_index': bit_index,
                'source':    source,
            })

    # Acumula linhas por canal efetivo (somente variáveis com canal explícito)
    channel_rows:    dict[str, list] = {}
    channel_sources: dict[str, set]  = {}

    for row in all_rows:
        tag    = row['tag']
        source = row['source']

        ov = overrides.get(tag, {})
        if not ov.get('enabled', True):
            continue   # variável desabilitada — não inclui

        channel = ov.get('channel')
        if not channel:
            continue   # sem canal explícito — não lida

        channel_rows.setdefault(channel, []).append(row)
        channel_sources.setdefault(channel, set()).add(source)

    # Constrói grupos contíguos por canal (cross-group), excluindo canais desabilitados
    result: dict = {}
    for channel, rows in channel_rows.items():
        ch_cfg_entry = channels_cfg.get(channel, {})
        if not ch_cfg_entry.get('enabled', True):
            logger.info("Canal '%s' desabilitado — ignorado.", channel)
            continue
        # Separa variáveis normais de bit-addressed
        coil_rows = []     # coils genuínos (At=%MB sem bit addressing)
        reg_rows  = []     # registers normais (At=%MW, sem bit addressing)
        bit_vars: dict[int, list] = {}  # register_addr → [{tag, key, bit}]

        # Primeiro passo: coletar todos os registradores que têm variáveis bit-addressed
        bit_addressed_registers: set[int] = set()
        for row in rows:
            if row['bit_index'] is not None:
                bit_addressed_registers.add(row['modbus'])

        for row in rows:
            at        = row['at']
            bit_index = row['bit_index']
            addr      = row['modbus']

            if bit_index is not None:
                # Variável bit-addressed explícita (Modbus tem sufixo .NN)
                bit_vars.setdefault(addr, []).append({
                    'tag': row['tag'],
                    'key': row['key'],
                    'bit': bit_index,
                })
            elif at == '%MB' and addr in bit_addressed_registers:
                # Bit 0 implícito: At=%MB sem sufixo, mas o registro tem siblings bit-addressed
                bit_vars.setdefault(addr, []).append({
                    'tag': row['tag'],
                    'key': row['key'],
                    'bit': 0,
                })
            elif at == '%MB':
                # Coil genuíno (não compartilha registro com bit-addressed)
                coil_rows.append(row)
            else:
                # Register normal
                reg_rows.append(row)

        # Adiciona registradores de bit_vars ao pool de registers para leitura
        # (cada endereço aparece uma vez, com um tag/key placeholder)
        for addr, bvars in sorted(bit_vars.items()):
            # Usa o primeiro tag do grupo como placeholder
            first = bvars[0]
            reg_rows.append({
                'key':    first['key'],
                'tag':    first['tag'],
                'modbus': addr,
            })

        # Ordena e constrói grupos contíguos
        coil_rows.sort(key=lambda r: r['modbus'])
        reg_rows.sort(key=lambda r: r['modbus'])

        # Deduplica reg_rows por endereço (pode haver registros duplicados via bit_vars + normal)
        seen_reg_addrs: set[int] = set()
        deduped_reg_rows: list = []
        for r in reg_rows:
            if r['modbus'] not in seen_reg_addrs:
                seen_reg_addrs.add(r['modbus'])
                deduped_reg_rows.append(r)
        reg_rows = deduped_reg_rows

        coil_addrs = [r['modbus'] for r in coil_rows]
        reg_addrs  = [r['modbus'] for r in reg_rows]

        coil_groups = _find_contiguous(coil_addrs)
        reg_groups  = _find_contiguous(reg_addrs)

        # Mapeia endereços de volta para tags/keys
        coil_addr_tag = {r['modbus']: r['tag'] for r in coil_rows}
        coil_addr_key = {r['modbus']: r['key'] for r in coil_rows}
        reg_addr_tag  = {r['modbus']: r['tag'] for r in reg_rows}
        reg_addr_key  = {r['modbus']: r['key'] for r in reg_rows}

        coil_tags = [[coil_addr_tag[a] for a in g] for g in coil_groups]
        coil_keys = [[coil_addr_key[a] for a in g] for g in coil_groups]
        reg_tags  = [[reg_addr_tag[a] for a in g]  for g in reg_groups]
        reg_keys  = [[reg_addr_key[a] for a in g]  for g in reg_groups]

        ch_cfg       = channels_cfg.get(channel, {})
        history_size = ch_cfg.get('history_size', default_hist)

        result[channel] = {
            'coil_groups':  coil_groups,
            'reg_groups':   reg_groups,
            'coil_tags':    coil_tags,
            'reg_tags':     reg_tags,
            'coil_keys':    coil_keys,
            'reg_keys':     reg_keys,
            'bit_vars':     bit_vars if bit_vars else None,
            'history_size': history_size,
            'sources':      channel_sources[channel],
        }

    logger.info("Canais carregados dos CSVs: %d — %s", len(result), list(result.keys()))
    return result
