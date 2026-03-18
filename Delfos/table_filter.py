import csv
import logging
import itertools
import os
import sys
import time
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.bit_addressing import parse_modbus_address, is_bit_addressed

logger = logging.getLogger(__name__)


def _find_contiguous(lst):
    """Agrupa uma lista de inteiros em sublistas de valores contíguos."""
    groups = []
    for _, g in itertools.groupby(enumerate(lst), lambda x: x[1] - x[0]):
        groups.append([int(x[1]) for x in g])
    return groups


def _read_csv(file_path, attempts=5, pause=5):
    """Lê um CSV e retorna lista de dicts (via csv.DictReader) com retry."""
    for _ in range(attempts):
        try:
            with open(file_path, newline='', encoding='utf-8-sig') as f:
                return list(csv.DictReader(f))
        except Exception as e:
            logger.error("Erro ao tentar ler o arquivo .csv: %s", e)
            logger.info("Tentando novamente em %s segundos.", pause)
            time.sleep(pause)
    logger.critical("Não foi possível ler o arquivo após %s tentativas.", attempts)
    return None


def find_contiguous_groups(file_path, attempts=5, pause=5):
    rows = _read_csv(file_path, attempts, pause)
    if rows is None:
        return None, None, None, None, None, None

    coil_addrs, coil_tags_flat, coil_keys_flat = [], [], []
    reg_addrs, reg_tags_flat, reg_keys_flat = [], [], []

    for row in rows:
        modbus = (row.get('Modbus') or '').strip()
        key = (row.get('key') or '').strip()
        tag = (row.get('ObjecTag') or '').strip()
        at = (row.get('At') or '').strip()
        if not modbus or not key or not tag:
            continue
        try:
            addr = int(modbus)
        except (ValueError, TypeError):
            continue
        if at == '%MB':
            coil_addrs.append(addr)
            coil_tags_flat.append(tag)
            coil_keys_flat.append(key)
        else:
            reg_addrs.append(addr)
            reg_tags_flat.append(tag)
            reg_keys_flat.append(key)

    coils_groups = _find_contiguous(coil_addrs)
    registers_groups = _find_contiguous(reg_addrs)

    # Map addresses back to tags/keys per contiguous group
    idx = 0
    coils_tags, coils_keys = [], []
    for g in coils_groups:
        coils_tags.append(coil_tags_flat[idx:idx + len(g)])
        coils_keys.append(coil_keys_flat[idx:idx + len(g)])
        idx += len(g)

    idx = 0
    registers_tags, registers_keys = [], []
    for g in registers_groups:
        registers_tags.append(reg_tags_flat[idx:idx + len(g)])
        registers_keys.append(reg_keys_flat[idx:idx + len(g)])
        idx += len(g)

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
    rows = _read_csv(csv_file, attempts, pause)
    if rows is None:
        return {}

    # Group rows by key, preserving insertion order
    groups_by_key: dict[str, list] = {}
    for row in rows:
        modbus = (row.get('Modbus') or '').strip()
        key = (row.get('key') or '').strip()
        tag = (row.get('ObjecTag') or '').strip()
        if not modbus or not key or not tag:
            continue
        try:
            addr = int(modbus)
        except (ValueError, TypeError):
            continue
        groups_by_key.setdefault(key, []).append({
            'addr': addr, 'tag': tag, 'key': key,
            'at': (row.get('At') or '').strip(),
        })

    result = {}
    for group_name, group_rows in groups_by_key.items():
        coils = [(r['addr'], r['tag'], r['key']) for r in group_rows if r['at'] == '%MB']
        regs = [(r['addr'], r['tag'], r['key']) for r in group_rows if r['at'] != '%MB']

        coil_groups = _find_contiguous([a for a, _, _ in coils])
        reg_groups = _find_contiguous([a for a, _, _ in regs])

        idx = 0
        coil_tags, coil_keys = [], []
        for g in coil_groups:
            coil_tags.append([coils[idx + i][1] for i in range(len(g))])
            coil_keys.append([coils[idx + i][2] for i in range(len(g))])
            idx += len(g)

        idx = 0
        reg_tags, reg_keys = [], []
        for g in reg_groups:
            reg_tags.append([regs[idx + i][1] for i in range(len(g))])
            reg_keys.append([regs[idx + i][2] for i in range(len(g))])
            idx += len(g)

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

def _group_by_slave(rows):
    """Agrupa rows por unit_id, calcula contiguos por slave, retorna grupos + slaves.

    Enderecos de slaves diferentes NAO podem ser agrupados em blocos contiguos
    (mesmos enderecos numericos pertencem a slaves distintos).
    """
    by_uid = OrderedDict()
    for r in sorted(rows, key=lambda r: (r.get('unit_id') or 0, r['modbus'])):
        uid = r.get('unit_id')
        by_uid.setdefault(uid, []).append(r)

    all_groups, all_tags, all_keys, all_slaves = [], [], [], []
    for uid, uid_rows in by_uid.items():
        addrs = [r['modbus'] for r in uid_rows]
        addr_tag = {r['modbus']: r['tag'] for r in uid_rows}
        addr_key = {r['modbus']: r['key'] for r in uid_rows}
        groups = _find_contiguous(addrs)
        for g in groups:
            all_groups.append(g)
            all_tags.append([addr_tag[a] for a in g])
            all_keys.append([addr_key[a] for a in g])
            all_slaves.append(uid)
    return all_groups, all_tags, all_keys, all_slaves


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

    Suporta multi-slave: coluna 'unit_id' no CSV identifica qual slave Modbus
    cada tag pertence. Enderecos de slaves diferentes sao agrupados separadamente.

    Retorno por canal:
        {
            'coil_groups': [[addr, ...], ...],
            'reg_groups':  [[addr, ...], ...],
            'coil_tags':   [[tag, ...], ...],
            'reg_tags':    [[tag, ...], ...],
            'coil_keys':   [[group, ...], ...],
            'reg_keys':    [[group, ...], ...],
            'bit_vars':    {1584: [{'tag': ..., 'key': ..., 'bit': 0, 'unit_id': 20}, ...], ...},
            'group_slaves': [1, 1, 20, ...],   # unit_id por grupo reg (mesma ordem que reg_groups)
            'coil_slaves':  [1, 1, ...],        # unit_id por grupo coil
            'history_size': int,
            'sources':     set{'operacao', 'configuracao'},
        }

    Endereços contíguos são calculados através de TODOS os grupos do canal
    (pool cross-group), reduzindo roundtrips Modbus — mas separados por unit_id.
    """
    channels_cfg = group_config.get('channels', {})
    meta         = group_config.get('_meta', {})
    default_hist = meta.get('default_history_size', 100)

    all_rows: list = []

    for csv_path in csv_paths:
        source = os.path.splitext(os.path.basename(csv_path))[0]

        csv_rows = _read_csv(csv_path, attempts, pause)
        if csv_rows is None:
            continue

        for row in csv_rows:
            modbus_raw = (row.get('Modbus') or '').strip()
            key = (row.get('key') or '').strip()
            tag = (row.get('ObjecTag') or '').strip()
            if not modbus_raw or not key or not tag:
                continue

            try:
                register_addr, bit_index = parse_modbus_address(modbus_raw)
            except (ValueError, TypeError):
                logger.warning("Endereço Modbus inválido '%s' no CSV '%s', ignorando.", modbus_raw, csv_path)
                continue

            # Parse unit_id (only relevant for serial/sniff protocols)
            uid_raw = (row.get('unit_id') or '').strip()
            unit_id = int(uid_raw) if uid_raw else None

            all_rows.append({
                'key':       key,
                'tag':       tag,
                'at':        (row.get('At') or '').strip(),
                'modbus':    register_addr,
                'bit_index': bit_index,
                'source':    source,
                'unit_id':   unit_id,
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
        bit_vars: dict[int, list] = {}  # register_addr → [{tag, key, bit, unit_id}]

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
                    'unit_id': row.get('unit_id'),
                })
            elif at == '%MB' and addr in bit_addressed_registers:
                # Bit 0 implícito: At=%MB sem sufixo, mas o registro tem siblings bit-addressed
                bit_vars.setdefault(addr, []).append({
                    'tag': row['tag'],
                    'key': row['key'],
                    'bit': 0,
                    'unit_id': row.get('unit_id'),
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
                'key':     first['key'],
                'tag':     first['tag'],
                'modbus':  addr,
                'unit_id': first.get('unit_id'),
            })

        # Agrupa por slave e calcula contiguos separadamente por unit_id
        # Deduplica reg_rows por (unit_id, endereco)
        seen_reg_keys: set[tuple] = set()
        deduped_reg_rows: list = []
        for r in reg_rows:
            rk = (r.get('unit_id'), r['modbus'])
            if rk not in seen_reg_keys:
                seen_reg_keys.add(rk)
                deduped_reg_rows.append(r)
        reg_rows = deduped_reg_rows

        coil_groups, coil_tags, coil_keys, coil_slaves = _group_by_slave(coil_rows)
        reg_groups, reg_tags, reg_keys, reg_slaves = _group_by_slave(reg_rows)

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
            'group_slaves': reg_slaves if any(s is not None for s in reg_slaves) else None,
            'coil_slaves':  coil_slaves if any(s is not None for s in coil_slaves) else None,
            'history_size': history_size,
            'sources':      channel_sources[channel],
        }

    logger.info("Canais carregados dos CSVs: %d — %s", len(result), list(result.keys()))
    return result
