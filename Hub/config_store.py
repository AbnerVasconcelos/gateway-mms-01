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


# ── Operações de device ───────────────────────────────────────────────────────

def get_devices() -> dict:
    """Retorna {device_id: cfg} para todos os devices configurados."""
    return load_group_config().get('devices', {})


def create_device(device_id: str, cfg: dict) -> None:
    """Cria ou substitui um device em group_config['devices']."""
    config = load_group_config()
    config.setdefault('devices', {})[device_id] = cfg
    save_group_config(config)
    logger.info("Device '%s' criado.", device_id)


def update_device(device_id: str, fields: dict) -> None:
    """Atualiza campos de um device existente. Lança KeyError se não encontrado."""
    config = load_group_config()
    devices = config.setdefault('devices', {})
    if device_id not in devices:
        raise KeyError(f"Device '{device_id}' não encontrado.")
    devices[device_id].update({k: v for k, v in fields.items() if v is not None})
    save_group_config(config)
    logger.info("Device '%s' atualizado: %s", device_id, fields)


def delete_device(device_id: str) -> None:
    """Remove um device. Lança KeyError se não encontrado."""
    config = load_group_config()
    if device_id not in config.get('devices', {}):
        raise KeyError(f"Device '{device_id}' não encontrado.")
    del config['devices'][device_id]
    save_group_config(config)
    logger.info("Device '%s' removido.", device_id)


# ── Operações de canal ────────────────────────────────────────────────────────

def get_channels() -> dict:
    """
    Retorna {channel: {delay_ms, history_size}} para todos os canais configurados.
    Fonte única: seção 'channels' de group_config.json.
    """
    config = load_group_config()
    meta   = config.get('_meta', {})
    default_delay = meta.get('default_delay_ms', 1000)
    default_hist  = meta.get('default_history_size', 100)

    result: dict = {}
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
    """Atualiza history_size do canal na seção channels de group_config.json."""
    config = load_group_config()
    config.setdefault('channels', {}).setdefault(channel, {})['history_size'] = size
    save_group_config(config)
    logger.info("history_size=%d aplicado ao canal '%s'.", size, channel)


def patch_variable_override(tag: str, fields: dict) -> None:
    """
    Atualiza (ou cria) o override de uma variável individual.
    Campos suportados: enabled, channel.
    Campo delay_ms é ignorado silenciosamente.
    Valor None remove a chave correspondente do override.
    Override vazio após a operação → entrada removida completamente.
    """
    allowed = {k: v for k, v in fields.items() if k != 'delay_ms'}
    if not allowed:
        return
    overrides = load_overrides()
    entry = overrides.setdefault(tag, {})
    for k, v in allowed.items():
        if v is None or v == '':
            entry.pop(k, None)   # None ou string vazia → remove a chave
        else:
            entry[k] = v
    if not entry:
        del overrides[tag]       # override vazio → remove a entrada
    save_overrides(overrides)
    logger.info("Override da tag '%s' atualizado: %s", tag, allowed)


# ── Variáveis: leitura mesclada ───────────────────────────────────────────────

def load_all_variables() -> list:
    """
    Retorna lista de todas as variáveis com configuração mesclada:
    CSV → variable_overrides.json.

    Canal efetivo = overrides[tag]['channel'].  None quando não atribuída.

    Cada item:
        tag, group, type, address, channel, history_size, enabled, source, device
    """
    cfg          = load_group_config()
    meta         = cfg.get('_meta', {})
    default_hist = meta.get('default_history_size', 100)
    overrides    = load_overrides()
    channels_data = get_channels()
    devices      = cfg.get('devices', {})

    variables: list[dict] = []

    def _read_csv_files(csv_file_list: list, device_id: str | None) -> None:
        for csv_name in csv_file_list:
            csv_path = os.path.join(_TABLES_DIR, csv_name)
            if not os.path.exists(csv_path):
                continue

            source = os.path.splitext(os.path.basename(csv_path))[0]

            df = pd.read_csv(csv_path, sep=',')
            df = df.dropna(subset=['Modbus', 'key', 'ObjecTag'])
            df['Modbus'] = pd.to_numeric(df['Modbus'], errors='coerce').astype('Int64')

            for _, row in df.iterrows():
                tag     = str(row['ObjecTag']).strip()
                group   = str(row['key']).strip()
                var_at  = str(row.get('At', '')).strip()
                address = int(row['Modbus']) if pd.notna(row['Modbus']) else None

                ov      = overrides.get(tag, {})
                enabled = ov.get('enabled', True)
                channel = ov.get('channel')   # None quando não atribuída

                hist = channels_data.get(channel, {}).get('history_size', default_hist) if channel else None

                variables.append({
                    'tag':          tag,
                    'group':        group,
                    'type':         var_at,
                    'address':      address,
                    'channel':      channel,
                    'history_size': hist,
                    'enabled':      enabled,
                    'source':       source,
                    'device':       device_id,
                })

    if devices:
        for dev_id, dev_cfg in devices.items():
            csv_files = dev_cfg.get('csv_files', [])
            _read_csv_files(csv_files, dev_id)
    else:
        # backward compat: sem seção devices, usa os CSVs padrão
        _read_csv_files(['operacao.csv', 'configuracao.csv'], None)

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
    Parseia um arquivo enviado pelo usuário — aceita .xlsx ou .csv.
    Detecta o formato pelos magic bytes (xlsx = ZIP → começa com b'PK').
    Colunas reconhecidas: Tag, Canal, History size, Habilitado (mínimo: Tag + Canal).
    Retorna lista de dicts com os campos reconhecidos.
    """
    _is_xlsx = file_bytes[:2] == b'PK'

    if _is_xlsx:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    else:
        import csv
        text = file_bytes.decode('utf-8-sig', errors='replace')
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel   # fallback: vírgula padrão
        reader = csv.reader(io.StringIO(text), dialect)
        rows   = [tuple(row) for row in reader]

    if not rows:
        return []

    raw_headers = [str(h).strip() if h else '' for h in rows[0]]

    # Detecta formato: CSV nativo Modbus (tem 'ObjecTag') vs exportado pelo Hub (tem 'Tag'+'Canal')
    if 'ObjecTag' in raw_headers:
        header_map = {
            'ObjecTag': 'tag',
            'key':      'group',   # no CSV nativo, 'key' é o namespace/grupo
            'At':       'type',
            'Modbus':   'address',
        }
    else:
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
        raise ValueError(
            "Coluna de tag não encontrada. "
            "Esperado: 'Tag' (formato exportado) ou 'ObjecTag' (CSV Modbus nativo)."
        )

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

    Para cada linha: atualiza variable_overrides.json com channel e enabled.
    - channel vazio/None → remove a atribuição de canal
    - enabled=False → cria override; enabled=True → remove override de enabled
    - Override vazio após a operação → entrada removida
    """
    if not rows:
        return

    overrides = load_overrides()

    for row in rows:
        tag = row.get('tag', '').strip()
        if not tag:
            continue

        entry = overrides.get(tag, {})

        # Só atualiza o campo se a coluna estava presente no arquivo
        if 'channel' in row:
            channel = row['channel'] or None   # string vazia → None
            if channel:
                entry['channel'] = channel
            else:
                entry.pop('channel', None)

        if 'enabled' in row:
            if not row['enabled']:
                entry['enabled'] = False
            else:
                entry.pop('enabled', None)

        if entry:
            overrides[tag] = entry
        else:
            overrides.pop(tag, None)

    save_overrides(overrides)
    logger.info("apply_upload_config: %d linhas aplicadas.", len(rows))
