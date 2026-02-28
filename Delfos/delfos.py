#!/usr/bin/env python3
import datetime
import json
import logging
import os
import sys
from collections import defaultdict
from time import sleep, time

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.redis_config_functions import setup_redis, publish_to_channel, subscribe_to_channels, get_latest_message
from shared.modbus_functions import setup_modbus, read_coils, read_registers
from table_filter import extract_parameters_by_group

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

_TABLES_DIR = os.environ.get('TABLES_DIR', '../tables')

# Tick do loop principal em segundos — resolução mínima de delay entre grupos.
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


def _channel_history_size(channel, group_config, default=100):
    """Retorna history_size do primeiro grupo mapeado para este canal."""
    for cfg in group_config.get('groups', {}).values():
        if cfg.get('channel') == channel:
            return cfg.get('history_size', default)
    return default


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


def _build_all_groups(operacao_groups, configuracao_groups):
    """
    Mescla os grupos dos dois CSVs num único dict.
    Se houver colisão de nome (ex: 'alarmes' em ambos), o grupo de
    configuracao.csv recebe sufixo '_cfg' para evitar sobrescrita.
    """
    all_groups = {}
    for name, data in operacao_groups.items():
        data['_source'] = 'operacao'
        all_groups[name] = data
    for name, data in configuracao_groups.items():
        data['_source'] = 'configuracao'
        key = f"{name}_cfg" if name in all_groups else name
        all_groups[key] = data
    return all_groups


def main():
    csv_operacao      = os.path.join(_TABLES_DIR, 'operacao.csv')
    csv_configuracao  = os.path.join(_TABLES_DIR, 'configuracao.csv')
    group_config_path = os.path.join(_TABLES_DIR, 'group_config.json')
    overrides_path    = os.path.join(_TABLES_DIR, 'variable_overrides.json')

    # Carrega mapeamento CSV por grupo
    try:
        operacao_groups     = extract_parameters_by_group(csv_operacao)
        configuracao_groups = extract_parameters_by_group(csv_configuracao)
    except FileNotFoundError as e:
        logger.critical("Arquivo CSV não encontrado: %s", e)
        return
    except Exception as e:
        logger.critical("Erro inesperado ao processar arquivos CSV: %s", e)
        return

    if not operacao_groups and not configuracao_groups:
        logger.critical("Nenhum grupo carregado dos CSVs. Encerrando.")
        return

    all_groups = _build_all_groups(operacao_groups, configuracao_groups)
    logger.info("Grupos carregados: %s", list(all_groups.keys()))

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

    # Redis
    redis_result = retry_on_failure(setup_redis)
    if redis_result is None:
        return
    r, pubsub = redis_result
    if r is None or pubsub is None:
        return

    subscribe_to_channels(pubsub, ['user_status', 'config_reload'])

    # Modbus
    client = retry_on_failure(setup_modbus)
    if client is None:
        return

    # Estado inicial
    user_state = True
    last_read  = {group: 0.0 for group in all_groups}
    successful_reads   = 0
    unsuccessful_reads = 0

    logger.info("Delfos iniciado. Tick=%ss, grupos=%d", _LOOP_TICK, len(all_groups))

    while True:
        loop_start = time()

        # ── Drena fila de mensagens Redis ──────────────────────────────────
        message = get_latest_message(pubsub)
        if message and message['type'] == 'message':
            channel = message['channel'].decode()

            if channel == 'user_status':
                data       = json.loads(message['data'].decode())
                user_state = data['user_state']
                logger.info("Estado do usuário atualizado: conectado=%s", user_state)

            elif channel == 'config_reload':
                new_cfg = _load_group_config(group_config_path)
                if new_cfg:
                    group_config        = new_cfg
                    meta                = group_config.get('_meta', {})
                    backward_compatible = meta.get('backward_compatible', True)
                    default_delay_ms    = meta.get('default_delay_ms', 1000)
                    default_history     = meta.get('default_history_size', 100)
                    logger.info("group_config.json recarregado.")
                overrides = _load_variable_overrides(overrides_path)
                logger.info("variable_overrides.json recarregado.")

        # ── Sem usuário conectado: aguarda sem ler CLP ──────────────────────
        if not user_state:
            sleep(0.5)
            continue

        # ── Leitura segmentada por grupo ────────────────────────────────────
        groups_cfg = group_config.get('groups', {})

        # Acumula dados por canal Redis neste tick
        pending = {}   # channel → {"coils": {}, "registers": {}, "_sources": set()}

        for group_name, group_data in all_groups.items():
            cfg   = groups_cfg.get(group_name, {})
            delay = cfg.get('delay_ms', default_delay_ms) / 1000.0

            if loop_start - last_read[group_name] < delay:
                continue

            # Lê coils do grupo
            try:
                coil_data, _ = read_coils(
                    client,
                    group_data['coil_groups'],
                    group_data['coil_tags'],
                    group_data['coil_keys'],
                )
                successful_reads += 1
            except Exception as e:
                logger.error("Erro ao ler coils do grupo '%s': %s", group_name, e)
                coil_data = {}
                unsuccessful_reads += 1

            # Lê registers do grupo
            try:
                reg_data, _ = read_registers(
                    client,
                    group_data['reg_groups'],
                    group_data['reg_tags'],
                    group_data['reg_keys'],
                )
                successful_reads += 1
            except Exception as e:
                logger.error("Erro ao ler registers do grupo '%s': %s", group_name, e)
                reg_data = {}
                unsuccessful_reads += 1

            # Aplica overrides de variáveis (enabled=False)
            coil_data = _apply_overrides(dict(coil_data), overrides)
            reg_data  = _apply_overrides(dict(reg_data),  overrides)

            ch = cfg.get('channel', 'plc_data')
            if ch not in pending:
                pending[ch] = {'coils': {}, 'registers': {}, '_sources': set()}

            pending[ch]['coils'].update(coil_data)
            pending[ch]['registers'].update(reg_data)
            pending[ch]['_sources'].add(group_data['_source'])

            last_read[group_name] = loop_start

        # ── Publica canais segmentados ──────────────────────────────────────
        if pending:
            ts = datetime.datetime.now().isoformat()

            # Agregados para backward-compat
            agg_operacao      = {'coils': {}, 'registers': {}}
            agg_configuracao  = {'coils': {}, 'registers': {}}

            for ch, data in pending.items():
                history_size = _channel_history_size(ch, group_config, default_history)
                payload = {
                    'coils':     data['coils'],
                    'registers': data['registers'],
                    'timestamp': ts,
                }
                publish_to_channel(r, json.dumps(payload, indent=4), ch, history_size)
                logger.debug("Publicado em '%s' (%d coil-keys, %d reg-keys)",
                             ch, len(data['coils']), len(data['registers']))

                if backward_compatible:
                    if 'operacao' in data['_sources']:
                        agg_operacao['coils'].update(data['coils'])
                        agg_operacao['registers'].update(data['registers'])
                    if 'configuracao' in data['_sources']:
                        agg_configuracao['coils'].update(data['coils'])
                        agg_configuracao['registers'].update(data['registers'])

            if backward_compatible:
                if agg_operacao['coils'] or agg_operacao['registers']:
                    agg_operacao['timestamp'] = ts
                    publish_to_channel(r, json.dumps(agg_operacao, indent=4), 'plc_data')

                if agg_configuracao['coils'] or agg_configuracao['registers']:
                    agg_configuracao['timestamp'] = ts
                    publish_to_channel(r, json.dumps(agg_configuracao, indent=4), 'alarms')

            logger.info("Tick: %d canais publicados | ok=%d err=%d",
                        len(pending), successful_reads, unsuccessful_reads)

        # ── Dorme até o próximo tick ────────────────────────────────────────
        elapsed = time() - loop_start
        sleep(max(0.0, _LOOP_TICK - elapsed))


if __name__ == "__main__":
    main()
