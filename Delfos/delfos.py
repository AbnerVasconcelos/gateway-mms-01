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

# Tick do loop principal em segundos — resolucao minima de delay entre canais.
_LOOP_TICK = 0.05


def retry_on_failure(fn, attempts=3, delay=1):
    for _ in range(attempts):
        try:
            return fn()
        except Exception as e:
            logger.warning("Erro: %s, nova tentativa em %ss.", e, delay)
            sleep(delay)
    logger.error("Falha apos %s tentativas.", attempts)
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
        logger.warning("Erro ao carregar overrides '%s': %s. Usando vazio.", path, e)
        return {}


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
    # ── DEVICE_ID obrigatorio ────────────────────────────────────────────────
    device_id = os.environ.get('DEVICE_ID')
    if not device_id:
        logger.critical("DEVICE_ID env var obrigatoria. Encerrando.")
        return

    group_config_path = os.path.join(_TABLES_DIR, 'group_config.json')
    overrides_path    = os.path.join(_TABLES_DIR, f'variable_overrides_{device_id}.json')

    # Carrega configuracao e overrides do device
    group_config = retry_on_failure(lambda: _load_group_config(group_config_path))
    if group_config is None:
        logger.critical("Nao foi possivel carregar group_config.json. Encerrando.")
        return

    device_cfg = group_config.get('devices', {}).get(device_id)
    if not device_cfg:
        logger.critical("Device '%s' nao encontrado em group_config.json.", device_id)
        return

    overrides = _load_variable_overrides(overrides_path)

    meta             = group_config.get('_meta', {})
    default_delay_ms = meta.get('default_delay_ms', 1000)
    default_history  = meta.get('default_history_size', 100)

    # Caminhos CSV apenas deste device
    csv_paths = [os.path.join(_TABLES_DIR, f) for f in device_cfg.get('csv_files', [])]

    # Channel config do device (nao global)
    channel_cfg = {}
    for ch, ch_c in device_cfg.get('channels', {}).items():
        channel_cfg[ch] = {
            'delay_ms':     ch_c.get('delay_ms', default_delay_ms),
            'history_size': ch_c.get('history_size', default_history),
        }

    # Build device-scoped group_config para extract_parameters_by_channel
    device_group_config = {
        '_meta':    meta,
        'channels': device_cfg.get('channels', {}),
    }

    # Redis (antes de channel_data para poder aguardar config_reload)
    redis_result = retry_on_failure(setup_redis)
    if redis_result is None:
        return
    r, pubsub = redis_result
    if r is None or pubsub is None:
        return

    config_reload_channel = os.environ.get('CONFIG_RELOAD_CHANNEL', f'config_reload_{device_id}')
    subscribe_to_channels(pubsub, ['user_status', config_reload_channel])

    # Carrega mapeamento CSV por canal
    try:
        channel_data = extract_parameters_by_channel(
            csv_paths, device_group_config, overrides)
    except FileNotFoundError as e:
        logger.critical("Arquivo CSV nao encontrado: %s", e)
        return
    except Exception as e:
        logger.critical("Erro inesperado ao processar arquivos CSV: %s", e)
        return

    if not channel_data:
        logger.warning("Nenhum canal carregado dos CSVs. Aguardando config_reload...")

    # Modbus (lazy — só conecta quando ha canais para ler)
    _modbus_protocol = os.environ.get('MODBUS_PROTOCOL', 'tcp')
    client = None
    if channel_data:
        client = retry_on_failure(lambda: setup_modbus(protocol=_modbus_protocol))
        if client is None:
            return

    # Estado inicial
    user_state = True
    last_read  = {ch: 0.0 for ch in channel_data}
    successful_reads   = 0
    unsuccessful_reads = 0

    logger.info("Delfos iniciado para device '%s'. Tick=%ss, canais=%d",
                device_id, _LOOP_TICK, len(channel_data))

    while True:
        loop_start = time()

        # ── Drena fila de mensagens Redis ──────────────────────────────────
        message = get_latest_message(pubsub)
        if message and message['type'] == 'message':
            ch_msg = message['channel'].decode()

            if ch_msg == 'user_status':
                data       = json.loads(message['data'].decode())
                user_state = data['user_state']
                logger.info("Estado do usuario atualizado: conectado=%s", user_state)

            elif ch_msg == config_reload_channel:
                new_cfg = _load_group_config(group_config_path)
                new_overrides = _load_variable_overrides(overrides_path)
                if new_cfg:
                    group_config     = new_cfg
                    meta             = group_config.get('_meta', {})
                    default_delay_ms = meta.get('default_delay_ms', 1000)
                    default_history  = meta.get('default_history_size', 100)
                    device_cfg       = group_config.get('devices', {}).get(device_id, device_cfg)
                    logger.info("group_config.json recarregado.")
                overrides = new_overrides

                # Re-computa channel_data com nova config (hot-reload graceful)
                try:
                    new_csv_paths = [os.path.join(_TABLES_DIR, f)
                                     for f in device_cfg.get('csv_files', [])]
                    new_device_group_config = {
                        '_meta':    meta,
                        'channels': device_cfg.get('channels', {}),
                    }
                    new_channel_data = extract_parameters_by_channel(
                        new_csv_paths, new_device_group_config, overrides)

                    new_channel_cfg = {}
                    for ch, ch_c in device_cfg.get('channels', {}).items():
                        new_channel_cfg[ch] = {
                            'delay_ms':     ch_c.get('delay_ms', default_delay_ms),
                            'history_size': ch_c.get('history_size', default_history),
                        }

                    # Preserva timers de canais existentes; novos canais publicam imediatamente
                    new_last_read = {ch: last_read.get(ch, 0.0) for ch in new_channel_data}
                    channel_data = new_channel_data
                    channel_cfg  = new_channel_cfg
                    last_read    = new_last_read
                    logger.info("Config recarregada. Canais ativos: %s", list(channel_data.keys()))

                    # Conecta Modbus se ainda nao conectado (primeira atribuicao de canais)
                    if channel_data and client is None:
                        client = retry_on_failure(lambda: setup_modbus(protocol=_modbus_protocol))
                        if client:
                            logger.info("Modbus conectado apos config_reload.")
                except Exception as e:
                    logger.error("Erro ao recarregar channel_data: %s", e)

        # ── Sem canais ou sem conexao Modbus: aguarda config_reload ────────
        if not channel_data or client is None:
            sleep(0.5)
            continue

        # ── Sem usuario conectado: aguarda sem ler CLP ──────────────────────
        if not user_state:
            sleep(0.5)
            continue

        # ── Leitura e publicacao por canal ──────────────────────────────────
        ts = datetime.datetime.now().isoformat()
        published_count = 0

        for ch, ch_data in channel_data.items():
            cfg   = channel_cfg.get(ch, {})
            delay = cfg.get('delay_ms', default_delay_ms) / 1000.0

            if loop_start - last_read[ch] < delay:
                continue

            # Le coils do canal (cross-group aggregado)
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

            # Le registers do canal
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

            # Aplica overrides (enabled=False em runtime, seguranca extra)
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

        if published_count:
            logger.info("Tick: %d canais publicados | ok=%d err=%d",
                        published_count, successful_reads, unsuccessful_reads)

        # ── Dorme ate o proximo tick ────────────────────────────────────────
        elapsed = time() - loop_start
        sleep(max(0.0, _LOOP_TICK - elapsed))


if __name__ == "__main__":
    main()
