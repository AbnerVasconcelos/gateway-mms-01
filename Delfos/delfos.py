#!/usr/bin/env python3
import datetime
import json
import logging
import os
import sys
from time import sleep, time

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.redis_config_functions import setup_redis, publish_to_channel, subscribe_to_channels, get_latest_message
from shared.modbus_functions import setup_modbus, read_coils, read_registers
from table_filter import extract_parameters_by_channel

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

_TABLES_DIR = os.environ.get('TABLES_DIR', '../tables')

# Tick do loop principal em segundos — resolução mínima de delay entre canais.
_LOOP_TICK = 0.05


def retry_on_failure(fn, attempts=3, delay=1):
    for _ in range(attempts):
        try:
            return fn()
        except Exception as e:
            logger.warning("Erro: %s, nova tentativa em %ss.", e, delay)
            sleep(delay)
    logger.error("Falha após %s tentativas.", attempts)
    return None


def _load_group_config(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error("Erro ao carregar group_config.json: %s", e)
        return None


def _load_variable_overrides(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Erro ao carregar variable_overrides.json: %s. Usando vazio.", e)
        return {}


def _load_channel_config(group_config, default_delay_ms, default_history) -> dict:
    """
    Retorna {channel: {delay_ms, history_size}} lendo a seção 'channels' do
    group_config. Canais referenciados nos grupos mas sem entrada em 'channels'
    recebem os valores default.
    """
    result: dict = {}
    # Canais inferidos dos grupos (com defaults)
    for cfg in group_config.get('groups', {}).values():
        ch = cfg.get('channel')
        if ch and ch not in result:
            result[ch] = {'delay_ms': default_delay_ms, 'history_size': default_history}
    # Seção channels sobrescreve
    for ch, ch_cfg in group_config.get('channels', {}).items():
        result[ch] = {
            'delay_ms':    ch_cfg.get('delay_ms',    default_delay_ms),
            'history_size': ch_cfg.get('history_size', default_history),
        }
    return result


def _build_csv_paths(tables_dir: str, group_config: dict) -> list:
    """
    Retorna lista de caminhos CSV a ler, respeitando a seção 'devices' e a flag
    'enabled' por device. Devices desativados (enabled=False) são ignorados.
    Sem seção 'devices': backward compat com operacao.csv + configuracao.csv.
    """
    devices = group_config.get('devices', {})
    if not devices:
        return [
            os.path.join(tables_dir, 'operacao.csv'),
            os.path.join(tables_dir, 'configuracao.csv'),
        ]
    paths = []
    for dev_id, dev_cfg in devices.items():
        if not dev_cfg.get('enabled', True):
            logger.info("Device '%s' desativado — pulando leitura.", dev_id)
            continue
        for fname in dev_cfg.get('csv_files', []):
            path = os.path.join(tables_dir, fname)
            if path not in paths:
                paths.append(path)
    return paths


def _apply_overrides(data, overrides):
    """Remove tags com enabled=False dos dados lidos. Preserva estrutura {key: {tag: val}}."""
    if not overrides:
        return data
    result = {}
    for key, tags in data.items():
        filtered = {tag: val for tag, val in tags.items()
                    if overrides.get(tag, {}).get('enabled', True)}
        if filtered:
            result[key] = filtered
    return result


def main():
    group_config_path = os.path.join(_TABLES_DIR, 'group_config.json')
    overrides_path    = os.path.join(_TABLES_DIR, 'variable_overrides.json')

    # Carrega configuração de canais e overrides
    group_config = retry_on_failure(lambda: _load_group_config(group_config_path))
    if group_config is None:
        logger.critical("Não foi possível carregar group_config.json. Encerrando.")
        return
    overrides = _load_variable_overrides(overrides_path)

    meta               = group_config.get('_meta', {})
    backward_compatible = meta.get('backward_compatible', True)
    default_delay_ms   = meta.get('default_delay_ms', 1000)
    default_history    = meta.get('default_history_size', 100)

    # Carrega mapeamento CSV por canal
    try:
        channel_data = extract_parameters_by_channel(
            _build_csv_paths(_TABLES_DIR, group_config), group_config, overrides)
    except FileNotFoundError as e:
        logger.critical("Arquivo CSV não encontrado: %s", e)
        return
    except Exception as e:
        logger.critical("Erro inesperado ao processar arquivos CSV: %s", e)
        return

    if not channel_data:
        logger.critical("Nenhum canal carregado dos CSVs. Encerrando.")
        return

    channel_cfg = _load_channel_config(group_config, default_delay_ms, default_history)
    logger.info("Canais ativos: %s", list(channel_data.keys()))

    # Redis
    redis_result = retry_on_failure(setup_redis)
    if redis_result is None:
        return
    r, pubsub = redis_result
    if r is None or pubsub is None:
        return

    subscribe_to_channels(pubsub, ['user_status', 'config_reload'])

    # Modbus
    _modbus_protocol = os.environ.get('MODBUS_PROTOCOL', 'tcp')
    client = retry_on_failure(lambda: setup_modbus(protocol=_modbus_protocol))
    if client is None:
        return

    # Estado inicial
    user_state = True
    last_read  = {ch: 0.0 for ch in channel_data}
    successful_reads   = 0
    unsuccessful_reads = 0

    logger.info("Delfos iniciado. Tick=%ss, canais=%d", _LOOP_TICK, len(channel_data))

    while True:
        loop_start = time()

        # ── Drena fila de mensagens Redis ──────────────────────────────────
        message = get_latest_message(pubsub)
        if message and message['type'] == 'message':
            ch_msg = message['channel'].decode()

            if ch_msg == 'user_status':
                data       = json.loads(message['data'].decode())
                user_state = data['user_state']
                logger.info("Estado do usuário atualizado: conectado=%s", user_state)

            elif ch_msg == 'config_reload':
                new_cfg = _load_group_config(group_config_path)
                new_overrides = _load_variable_overrides(overrides_path)
                if new_cfg:
                    group_config        = new_cfg
                    meta                = group_config.get('_meta', {})
                    backward_compatible = meta.get('backward_compatible', True)
                    default_delay_ms    = meta.get('default_delay_ms', 1000)
                    default_history     = meta.get('default_history_size', 100)
                    logger.info("group_config.json recarregado.")
                overrides = new_overrides
                # Re-computa channel_data com nova config (hot-reload graceful)
                try:
                    new_channel_data = extract_parameters_by_channel(
                        _build_csv_paths(_TABLES_DIR, group_config), group_config, overrides)
                    new_channel_cfg  = _load_channel_config(group_config, default_delay_ms, default_history)
                    # Preserva timers de canais existentes; novos canais publicam imediatamente
                    new_last_read = {ch: last_read.get(ch, 0.0) for ch in new_channel_data}
                    channel_data = new_channel_data
                    channel_cfg  = new_channel_cfg
                    last_read    = new_last_read
                    logger.info("Config recarregada. Canais ativos: %s", list(channel_data.keys()))
                except Exception as e:
                    logger.error("Erro ao recarregar channel_data: %s", e)

        # ── Sem usuário conectado: aguarda sem ler CLP ──────────────────────
        if not user_state:
            sleep(0.5)
            continue

        # ── Leitura e publicação por canal ──────────────────────────────────
        ts = datetime.datetime.now().isoformat()

        # Acumuladores backward-compat (plc_data / alarms)
        agg_operacao     = {'coils': {}, 'registers': {}} if backward_compatible else None
        agg_configuracao = {'coils': {}, 'registers': {}} if backward_compatible else None
        published_count  = 0

        for ch, ch_data in channel_data.items():
            cfg   = channel_cfg.get(ch, {})
            delay = cfg.get('delay_ms', default_delay_ms) / 1000.0

            if loop_start - last_read[ch] < delay:
                continue

            # Lê coils do canal (cross-group aggregado)
            try:
                coil_data, _ = read_coils(
                    client,
                    ch_data['coil_groups'],
                    ch_data['coil_tags'],
                    ch_data['coil_keys'],
                )
                successful_reads += 1
            except Exception as e:
                logger.error("Erro ao ler coils do canal '%s': %s", ch, e)
                coil_data = {}
                unsuccessful_reads += 1

            # Lê registers do canal
            try:
                reg_data, _ = read_registers(
                    client,
                    ch_data['reg_groups'],
                    ch_data['reg_tags'],
                    ch_data['reg_keys'],
                )
                successful_reads += 1
            except Exception as e:
                logger.error("Erro ao ler registers do canal '%s': %s", ch, e)
                reg_data = {}
                unsuccessful_reads += 1

            # Aplica overrides (enabled=False em runtime, segurança extra)
            coil_data = _apply_overrides(dict(coil_data), overrides)
            reg_data  = _apply_overrides(dict(reg_data),  overrides)

            history_size = ch_data.get('history_size', cfg.get('history_size', default_history))
            payload = {
                'coils':     coil_data,
                'registers': reg_data,
                'timestamp': ts,
            }
            publish_to_channel(r, json.dumps(payload, indent=4), ch, history_size)
            logger.debug("Publicado em '%s' (%d coil-keys, %d reg-keys)",
                         ch, len(coil_data), len(reg_data))

            last_read[ch]   = loop_start
            published_count += 1

            if backward_compatible:
                sources = ch_data.get('sources', set())
                if 'operacao' in sources:
                    agg_operacao['coils'].update(coil_data)
                    agg_operacao['registers'].update(reg_data)
                if 'configuracao' in sources:
                    agg_configuracao['coils'].update(coil_data)
                    agg_configuracao['registers'].update(reg_data)

        # ── Publica canais backward-compat ──────────────────────────────────
        if backward_compatible and published_count:
            if agg_operacao['coils'] or agg_operacao['registers']:
                agg_operacao['timestamp'] = ts
                publish_to_channel(r, json.dumps(agg_operacao, indent=4), 'plc_data')

            if agg_configuracao['coils'] or agg_configuracao['registers']:
                agg_configuracao['timestamp'] = ts
                publish_to_channel(r, json.dumps(agg_configuracao, indent=4), 'alarms')

        if published_count:
            logger.info("Tick: %d canais publicados | ok=%d err=%d",
                        published_count, successful_reads, unsuccessful_reads)

        # ── Dorme até o próximo tick ────────────────────────────────────────
        elapsed = time() - loop_start
        sleep(max(0.0, _LOOP_TICK - elapsed))


if __name__ == "__main__":
    main()
