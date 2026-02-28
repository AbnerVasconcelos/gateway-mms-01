"""
config_store — leitura e escrita das configurações do gateway.

Gerencia dois arquivos em tables/:
  - group_config.json      : mapeamento grupo → canal + delay_ms + history_size
  - variable_overrides.json: exceções por tag (enabled, channel, delay_ms)

O caminho base (_TABLES_DIR) pode ser sobrescrito via variável de ambiente
TABLES_DIR ou diretamente no atributo de módulo (útil em testes).
"""

import io
import json
import logging
import os

import openpyxl
import pandas as pd

logger = logging.getLogger(__name__)

_HUB_DIR     = os.path.dirname(os.path.abspath(__file__))
_GATEWAY_DIR = os.path.dirname(_HUB_DIR)

def _resolve_tables_dir() -> str:
    """
    Resolve o diretório de tabelas.
    Caminhos relativos (ex: '../tables') são resolvidos a partir de _HUB_DIR,
    não do CWD — garante comportamento correto independente de onde o processo
    é iniciado ou de qual .env foi carregado no ambiente herdado.
    """
    tables_env = os.environ.get('TABLES_DIR')
    if tables_env:
        if not os.path.isabs(tables_env):
            return os.path.normpath(os.path.join(_HUB_DIR, tables_env))
        return tables_env
    return os.path.join(_GATEWAY_DIR, 'tables')

_TABLES_DIR = _resolve_tables_dir()


def _group_config_path() -> str:
    return os.path.join(_TABLES_DIR, 'group_config.json')


def _overrides_path() -> str:
    return os.path.join(_TABLES_DIR, 'variable_overrides.json')


# ── Leitura ──────────────────────────────────────────────────────────────────

def load_group_config() -> dict:
    """Carrega group_config.json. Lança exceção se não encontrar."""
    with open(_group_config_path(), 'r', encoding='utf-8') as f:
        return json.load(f)


def load_overrides() -> dict:
    """Carrega variable_overrides.json. Retorna {} se o arquivo não existir."""
    path = _overrides_path()
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ── Escrita ───────────────────────────────────────────────────────────────────

def save_group_config(config: dict) -> None:
    """Persiste group_config.json."""
    with open(_group_config_path(), 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info("group_config.json salvo.")


def save_overrides(overrides: dict) -> None:
    """Persiste variable_overrides.json."""
    with open(_overrides_path(), 'w', encoding='utf-8') as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)
    logger.info("variable_overrides.json salvo.")


# ── Operações de canal ────────────────────────────────────────────────────────

def get_channel_history_sizes() -> dict:
    """
    Retorna {channel: history_size} para todos os canais únicos configurados.
    Cada canal aparece uma única vez — o primeiro grupo mapeado define o valor.
    """
    config = load_group_config()
    result: dict[str, int] = {}
    for cfg in config.get('groups', {}).values():
        ch = cfg.get('channel')
        if ch and ch not in result:
            result[ch] = cfg.get('history_size', 100)
    return result


def update_channel_history_size(channel: str, size: int) -> None:
    """
    Atualiza history_size de todos os grupos mapeados para o canal dado
    e persiste o arquivo. Não altera outros canais.
    """
    config  = load_group_config()
    updated = 0
    for cfg in config.get('groups', {}).values():
        if cfg.get('channel') == channel:
            cfg['history_size'] = size
            updated += 1
    save_group_config(config)
    logger.info("history_size=%d aplicado a %d grupos do canal '%s'.", size, updated, channel)


def patch_variable_override(tag: str, fields: dict) -> None:
    """
    Atualiza (ou cria) o override de uma variável individual.
    Campos suportados: enabled, channel, delay_ms.
    """
    overrides = load_overrides()
    overrides.setdefault(tag, {}).update(fields)
    save_overrides(overrides)
    logger.info("Override da tag '%s' atualizado: %s", tag, fields)


# ── Variáveis: leitura mesclada ───────────────────────────────────────────────

# Grupos do operacao.csv (para detectar colisão com configuracao.csv)
_OPERACAO_GROUPS: set[str] = set()


def _build_group_cfg_key(group: str, source: str) -> str:
    """
    Devolve a chave usada em group_config.json para um dado grupo/source.
    Grupos do configuracao.csv que colidam com operacao.csv recebem sufixo '_cfg'.
    """
    if source == 'configuracao' and group in _OPERACAO_GROUPS:
        return group + '_cfg'
    return group


def load_all_variables() -> list:
    """
    Retorna lista de todas as variáveis com configuração mesclada:
    CSV → group_config.json → variable_overrides.json.

    Cada item:
        tag, group, group_cfg_key, type, address, channel,
        delay_ms, history_size, enabled, has_override, source
    """
    global _OPERACAO_GROUPS

    group_cfg      = load_group_config()
    groups_data    = group_cfg.get('groups', {})
    meta           = group_cfg.get('_meta', {})
    default_delay  = meta.get('default_delay_ms', 1000)
    default_hist   = meta.get('default_history_size', 100)
    overrides      = load_overrides()

    operacao_path     = os.path.join(_TABLES_DIR, 'operacao.csv')
    configuracao_path = os.path.join(_TABLES_DIR, 'configuracao.csv')

    variables: list[dict] = []

    for csv_path in (operacao_path, configuracao_path):
        if not os.path.exists(csv_path):
            continue

        source = os.path.splitext(os.path.basename(csv_path))[0]  # 'operacao' | 'configuracao'

        df = pd.read_csv(csv_path, sep=',')
        df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])
        df['Modbus'] = pd.to_numeric(df['Modbus'], errors='coerce').astype('Int64')

        if source == 'operacao':
            _OPERACAO_GROUPS = set(df['key'].unique())

        for _, row in df.iterrows():
            tag     = str(row['ObjecTag']).strip()
            group   = str(row['key']).strip()
            var_at  = str(row.get('At', '')).strip()   # %MB or %MW
            address = int(row['Modbus']) if pd.notna(row['Modbus']) else None

            cfg_key  = _build_group_cfg_key(group, source)
            grp_cfg  = groups_data.get(cfg_key, {})
            channel  = grp_cfg.get('channel', 'plc_data')
            delay_ms = grp_cfg.get('delay_ms', default_delay)
            hist     = grp_cfg.get('history_size', default_hist)

            ov           = overrides.get(tag, {})
            has_override = bool(ov)
            enabled      = ov.get('enabled', True)
            if 'channel'  in ov: channel  = ov['channel']
            if 'delay_ms' in ov: delay_ms = ov['delay_ms']

            variables.append({
                'tag':           tag,
                'group':         group,
                'group_cfg_key': cfg_key,
                'type':          var_at,
                'address':       address,
                'channel':       channel,
                'delay_ms':      delay_ms,
                'history_size':  hist,
                'enabled':       enabled,
                'has_override':  has_override,
                'source':        source,
            })

    return variables


# ── Export / Import ───────────────────────────────────────────────────────────

_EXPORT_COLUMNS = ['tag', 'group', 'type', 'address', 'channel', 'delay_ms', 'history_size', 'enabled', 'source']
_EXPORT_HEADERS = ['Tag', 'Grupo', 'Tipo', 'Endereço', 'Canal', 'Delay (ms)', 'History size', 'Habilitado', 'Fonte']


def generate_export_xlsx() -> bytes:
    """Gera e retorna os bytes de um .xlsx com a configuração mesclada atual."""
    variables = load_all_variables()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Variáveis'

    ws.append(_EXPORT_HEADERS)

    # Cabeçalho em negrito
    from openpyxl.styles import Font
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for var in variables:
        ws.append([var.get(col) for col in _EXPORT_COLUMNS])

    # Ajusta largura das colunas
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_upload_xlsx(file_bytes: bytes) -> list:
    """
    Parseia um .xlsx enviado pelo usuário.
    Aceita colunas: Tag, Canal, Delay (ms), History size, Habilitado (mínimo: Tag + Canal).
    Retorna lista de dicts com os campos reconhecidos.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # Primeira linha = cabeçalhos
    raw_headers = [str(h).strip() if h else '' for h in rows[0]]

    # Mapeamento cabeçalho → campo interno
    header_map = {
        'Tag':          'tag',
        'Canal':        'channel',
        'Delay (ms)':   'delay_ms',
        'History size': 'history_size',
        'Habilitado':   'enabled',
        'Grupo':        'group',
        'Fonte':        'source',
    }

    col_index: dict[str, int] = {}
    for i, h in enumerate(raw_headers):
        if h in header_map:
            col_index[header_map[h]] = i

    if 'tag' not in col_index:
        raise ValueError("Coluna 'Tag' não encontrada no arquivo.")

    result = []
    for row in rows[1:]:
        if all(v is None for v in row):
            continue
        item: dict = {}
        for field, idx in col_index.items():
            val = row[idx] if idx < len(row) else None
            if field == 'enabled':
                item[field] = bool(val) if val is not None else True
            elif field in ('delay_ms', 'history_size'):
                item[field] = int(val) if val is not None else None
            else:
                item[field] = str(val).strip() if val is not None else None
        if item.get('tag'):
            result.append(item)

    return result


def apply_upload_config(rows: list) -> None:
    """
    Aplica configuração vinda de um upload (parse_upload_xlsx).

    Lógica:
      1. Agrupa por group_cfg_key (derivado de group + source).
      2. Se todos os rows de um grupo têm o mesmo channel/delay_ms →
         atualiza group_config.json para esse grupo.
      3. Para cada row cujo channel/delay_ms difere do grupo → override individual.
      4. enabled=False sempre cria override individual.
      5. Rows que coincidem exatamente com o grupo e enabled=True → remove override.
    """
    if not rows:
        return

    group_cfg  = load_group_config()
    groups_cfg = group_cfg.get('groups', {})
    overrides  = load_overrides()

    # Garante que _OPERACAO_GROUPS foi populado
    if not _OPERACAO_GROUPS:
        load_all_variables()

    # Monta mapa group_cfg_key → lista de rows
    from collections import defaultdict
    by_group: dict[str, list] = defaultdict(list)
    for row in rows:
        source   = row.get('source', 'operacao')
        group    = row.get('group', '')
        cfg_key  = _build_group_cfg_key(group, source) if group else None
        row['_cfg_key'] = cfg_key
        if cfg_key:
            by_group[cfg_key].append(row)

    for cfg_key, group_rows in by_group.items():
        channels  = {r['channel']  for r in group_rows if r.get('channel')}
        delays    = {r['delay_ms'] for r in group_rows if r.get('delay_ms') is not None}
        hist_vals = {r['history_size'] for r in group_rows if r.get('history_size') is not None}

        # Atualiza group_config se houver consenso no grupo
        entry = groups_cfg.setdefault(cfg_key, {})
        if len(channels) == 1:
            entry['channel'] = next(iter(channels))
        if len(delays) == 1:
            entry['delay_ms'] = next(iter(delays))
        if len(hist_vals) == 1:
            entry['history_size'] = next(iter(hist_vals))

    save_group_config(group_cfg)

    # Recarrega para comparar
    updated_groups = load_group_config().get('groups', {})

    for row in rows:
        tag     = row.get('tag', '').strip()
        cfg_key = row.get('_cfg_key')
        if not tag:
            continue

        enabled  = row.get('enabled', True)
        channel  = row.get('channel')
        delay_ms = row.get('delay_ms')

        grp = updated_groups.get(cfg_key, {})
        grp_ch  = grp.get('channel')
        grp_dly = grp.get('delay_ms')

        override: dict = {}
        if not enabled:
            override['enabled'] = False
        if channel and channel != grp_ch:
            override['channel'] = channel
        if delay_ms is not None and delay_ms != grp_dly:
            override['delay_ms'] = delay_ms

        if override:
            overrides.setdefault(tag, {}).update(override)
        else:
            # Remove override se coincide com grupo e está habilitado
            overrides.pop(tag, None)

    save_overrides(overrides)
    logger.info("apply_upload_config: %d linhas aplicadas.", len(rows))
