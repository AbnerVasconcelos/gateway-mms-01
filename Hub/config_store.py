"""
config_store — leitura e escrita das configurações do gateway.

Gerencia dois arquivos em tables/:
  - group_config.json      : mapeamento grupo → canal + channels section (delay_ms, history_size)
  - variable_overrides.json: exceções por tag (enabled, channel)

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
    """Carrega variable_overrides.json. Strip de delay_ms em cada entrada. Retorna {} se ausente."""
    path = _overrides_path()
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    # Strip delay_ms de todas as entradas (campo removido da camada de variáveis)
    return {tag: {k: v for k, v in cfg.items() if k != 'delay_ms'} for tag, cfg in raw.items()}


# ── Escrita ───────────────────────────────────────────────────────────────────

def save_group_config(config: dict) -> None:
    """Persiste group_config.json."""
    with open(_group_config_path(), 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info("group_config.json salvo.")


def save_overrides(overrides: dict) -> None:
    """Persiste variable_overrides.json. Strip de delay_ms antes de gravar."""
    cleaned = {tag: {k: v for k, v in cfg.items() if k != 'delay_ms'} for tag, cfg in overrides.items()}
    with open(_overrides_path(), 'w', encoding='utf-8') as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
    logger.info("variable_overrides.json salvo.")


# ── Operações de canal ────────────────────────────────────────────────────────

def get_channels() -> dict:
    """
    Retorna {channel: {delay_ms, history_size}} para todos os canais conhecidos.
    Canais derivados dos grupos recebem defaults de _meta; a seção 'channels'
    tem precedência total.
    """
    config = load_group_config()
    meta   = config.get('_meta', {})
    default_delay = meta.get('default_delay_ms', 1000)
    default_hist  = meta.get('default_history_size', 100)

    result: dict = {}
    # Primeiro: canais referenciados nos grupos (com valores default)
    for cfg in config.get('groups', {}).values():
        ch = cfg.get('channel')
        if ch and ch not in result:
            result[ch] = {'delay_ms': default_delay, 'history_size': default_hist}
    # Depois: seção channels sobrescreve os defaults
    for ch, ch_cfg in config.get('channels', {}).items():
        result[ch] = {
            'delay_ms':    ch_cfg.get('delay_ms',    default_delay),
            'history_size': ch_cfg.get('history_size', default_hist),
        }
    return result


def get_channel_history_sizes() -> dict:
    """Wrapper backward-compat: retorna {channel: history_size}."""
    return {ch: v['history_size'] for ch, v in get_channels().items()}


def create_channel(channel: str, delay_ms: int = 1000, history_size: int = 100) -> None:
    """Cria ou atualiza um canal explícito em group_config['channels']."""
    config = load_group_config()
    config.setdefault('channels', {})[channel] = {
        'delay_ms': delay_ms,
        'history_size': history_size,
    }
    save_group_config(config)
    logger.info("Canal '%s' criado (delay_ms=%d, history_size=%d).", channel, delay_ms, history_size)


def delete_channel(channel: str) -> None:
    """Remove um canal de group_config['channels']. Não altera grupos existentes."""
    config = load_group_config()
    channels = config.get('channels', {})
    if channel not in channels:
        raise KeyError(f"Canal '{channel}' não encontrado em channels.")
    del channels[channel]
    save_group_config(config)
    logger.info("Canal '%s' removido.", channel)


def update_channel_delay(channel: str, delay_ms: int) -> None:
    """Atualiza delay_ms do canal na seção channels de group_config.json."""
    config = load_group_config()
    config.setdefault('channels', {}).setdefault(channel, {})['delay_ms'] = delay_ms
    save_group_config(config)
    logger.info("delay_ms=%d aplicado ao canal '%s'.", delay_ms, channel)


def update_channel_history_size(channel: str, size: int) -> None:
    """
    Atualiza history_size do canal.
    Escreve na seção channels (fonte primária) e também nos grupos que mapeiam
    para o canal (backward-compat com testes que lêem groups diretamente).
    """
    config  = load_group_config()
    # Seção channels — fonte primária
    config.setdefault('channels', {}).setdefault(channel, {})['history_size'] = size
    # Grupos — backward-compat
    updated = 0
    for cfg in config.get('groups', {}).values():
        if cfg.get('channel') == channel:
            cfg['history_size'] = size
            updated += 1
    save_group_config(config)
    logger.info("history_size=%d aplicado ao canal '%s' (%d grupos).", size, channel, updated)


def patch_variable_override(tag: str, fields: dict) -> None:
    """
    Atualiza (ou cria) o override de uma variável individual.
    Campos suportados: enabled, channel.
    Campo delay_ms é ignorado silenciosamente.
    """
    allowed = {k: v for k, v in fields.items() if k != 'delay_ms'}
    if not allowed:
        return
    overrides = load_overrides()
    overrides.setdefault(tag, {}).update(allowed)
    save_overrides(overrides)
    logger.info("Override da tag '%s' atualizado: %s", tag, allowed)


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
    CSV → group_config.json (channels section) → variable_overrides.json.

    Cada item:
        tag, group, group_cfg_key, type, address, channel,
        history_size, enabled, has_override, source
    """
    global _OPERACAO_GROUPS

    group_cfg    = load_group_config()
    groups_data  = group_cfg.get('groups', {})
    meta         = group_cfg.get('_meta', {})
    default_hist = meta.get('default_history_size', 100)
    overrides    = load_overrides()

    # Channels section — fonte de history_size por canal
    channels_data = get_channels()

    operacao_path     = os.path.join(_TABLES_DIR, 'operacao.csv')
    configuracao_path = os.path.join(_TABLES_DIR, 'configuracao.csv')

    variables: list[dict] = []

    for csv_path in (operacao_path, configuracao_path):
        if not os.path.exists(csv_path):
            continue

        source = os.path.splitext(os.path.basename(csv_path))[0]

        df = pd.read_csv(csv_path, sep=',')
        df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])
        df['Modbus'] = pd.to_numeric(df['Modbus'], errors='coerce').astype('Int64')

        if source == 'operacao':
            _OPERACAO_GROUPS = set(df['key'].unique())

        for _, row in df.iterrows():
            tag     = str(row['ObjecTag']).strip()
            group   = str(row['key']).strip()
            var_at  = str(row.get('At', '')).strip()
            address = int(row['Modbus']) if pd.notna(row['Modbus']) else None

            cfg_key = _build_group_cfg_key(group, source)
            grp_cfg = groups_data.get(cfg_key, {})
            channel = grp_cfg.get('channel', 'plc_data')

            ov           = overrides.get(tag, {})
            has_override = bool(ov)
            enabled      = ov.get('enabled', True)
            if 'channel' in ov:
                channel = ov['channel']

            # history_size vem do canal efetivo
            hist = channels_data.get(channel, {}).get('history_size', default_hist)

            variables.append({
                'tag':           tag,
                'group':         group,
                'group_cfg_key': cfg_key,
                'type':          var_at,
                'address':       address,
                'channel':       channel,
                'history_size':  hist,
                'enabled':       enabled,
                'has_override':  has_override,
                'source':        source,
            })

    return variables


# ── Export / Import ───────────────────────────────────────────────────────────

_EXPORT_COLUMNS = ['tag', 'group', 'type', 'address', 'channel', 'history_size', 'enabled', 'source']
_EXPORT_HEADERS = ['Tag', 'Grupo', 'Tipo', 'Endereço', 'Canal', 'History size', 'Habilitado', 'Fonte']


def generate_export_xlsx() -> bytes:
    """Gera e retorna os bytes de um .xlsx com a configuração mesclada atual."""
    variables = load_all_variables()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Variáveis'

    ws.append(_EXPORT_HEADERS)

    from openpyxl.styles import Font
    for cell in ws[1]:
        cell.font = Font(bold=True)

    for var in variables:
        ws.append([var.get(col) for col in _EXPORT_COLUMNS])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_upload_xlsx(file_bytes: bytes) -> list:
    """
    Parseia um .xlsx enviado pelo usuário.
    Aceita colunas: Tag, Canal, History size, Habilitado (mínimo: Tag + Canal).
    Retorna lista de dicts com os campos reconhecidos.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    raw_headers = [str(h).strip() if h else '' for h in rows[0]]

    header_map = {
        'Tag':          'tag',
        'Canal':        'channel',
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
            elif field == 'history_size':
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
      2. Se todos os rows de um grupo têm o mesmo channel →
         atualiza group_config.json para esse grupo.
      3. Para cada row cujo channel difere do grupo → override individual.
      4. enabled=False sempre cria override individual.
      5. Rows que coincidem exatamente com o grupo e enabled=True → remove override.
    """
    if not rows:
        return

    group_cfg  = load_group_config()
    groups_cfg = group_cfg.get('groups', {})
    overrides  = load_overrides()

    if not _OPERACAO_GROUPS:
        load_all_variables()

    from collections import defaultdict
    by_group: dict[str, list] = defaultdict(list)
    for row in rows:
        source  = row.get('source', 'operacao')
        group   = row.get('group', '')
        cfg_key = _build_group_cfg_key(group, source) if group else None
        row['_cfg_key'] = cfg_key
        if cfg_key:
            by_group[cfg_key].append(row)

    for cfg_key, group_rows in by_group.items():
        channels  = {r['channel']  for r in group_rows if r.get('channel')}
        hist_vals = {r['history_size'] for r in group_rows if r.get('history_size') is not None}

        entry = groups_cfg.setdefault(cfg_key, {})
        if len(channels) == 1:
            entry['channel'] = next(iter(channels))
        if len(hist_vals) == 1:
            entry['history_size'] = next(iter(hist_vals))

    save_group_config(group_cfg)

    updated_groups = load_group_config().get('groups', {})

    for row in rows:
        tag     = row.get('tag', '').strip()
        cfg_key = row.get('_cfg_key')
        if not tag:
            continue

        enabled = row.get('enabled', True)
        channel = row.get('channel')

        grp    = updated_groups.get(cfg_key, {})
        grp_ch = grp.get('channel')

        override: dict = {}
        if not enabled:
            override['enabled'] = False
        if channel and channel != grp_ch:
            override['channel'] = channel

        if override:
            overrides.setdefault(tag, {}).update(override)
        else:
            overrides.pop(tag, None)

    save_overrides(overrides)
    logger.info("apply_upload_config: %d linhas aplicadas.", len(rows))
