#!/usr/bin/env python3
"""
Teste end-to-end: Simulador Modbus (TCP + RTU over TCP) → Delfos → Redis.

Verifica que o Delfos consegue ler valores do simulador e publicá-los no Redis.
Testa ambos os protocolos (tcp e rtu_tcp).

Uso:
    python tests/test_e2e_rtu.py
"""

import json
import logging
import os
import subprocess
import sys
import time

import redis

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES_DIR = os.path.join(GATEWAY_DIR, 'tables')
VENV_PYTHON = os.path.join(GATEWAY_DIR, '.venv', 'bin', 'python')
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('test_e2e')

CHANNEL = 'plc_test_e2e'
CSV_FILE = 'mapeamento_clp.csv'
REDIS_HOST = 'localhost'
REDIS_PORT = 6379


def setup_configs(port, protocol, tags_sample):
    """Configura group_config.json e variable_overrides.json para o teste."""
    group_config = {
        '_meta': {
            'aggregate_channel': 'plc_data',
            'backward_compatible': False,
            'default_delay_ms': 1000,
            'default_history_size': 100,
        },
        'devices': {
            'test_device': {
                'label': f'Test Device ({protocol})',
                'protocol': protocol,
                'host': 'localhost',
                'port': port,
                'unit_id': 1,
                'enabled': True,
                'csv_files': [CSV_FILE],
            },
        },
        'channels': {
            CHANNEL: {'delay_ms': 500, 'history_size': 10},
        },
    }

    overrides = {}
    for tag in tags_sample:
        overrides[tag] = {'channel': CHANNEL}

    with open(os.path.join(TABLES_DIR, 'group_config.json'), 'w') as f:
        json.dump(group_config, f, indent=2)
    with open(os.path.join(TABLES_DIR, 'variable_overrides.json'), 'w') as f:
        json.dump(overrides, f, indent=2)

    logger.info("Configs: protocol=%s port=%d, %d tags no canal '%s'",
                protocol, port, len(overrides), CHANNEL)


def get_sample_tags(limit=15):
    """Lê tags do CSV para usar nos overrides."""
    import pandas as pd
    csv_path = os.path.join(TABLES_DIR, CSV_FILE)
    df = pd.read_csv(csv_path)
    tags = df['ObjecTag'].dropna().astype(str).tolist()
    return tags[:limit]


def start_simulator(port, protocol):
    """Inicia um simulador Modbus como subprocess."""
    script = f"""
import asyncio, os, sys, random, logging
sys.path.insert(0, '{GATEWAY_DIR}')
sys.path.insert(0, os.path.join('{GATEWAY_DIR}', 'tests'))
from modbus_simulator import load_csv, LoggingDataBlock
from pymodbus.datastore import ModbusServerContext, ModbusSlaveContext
from pymodbus.server import StartAsyncTcpServer
from pymodbus.framer import ModbusRtuFramer, ModbusSocketFramer

logging.basicConfig(level=logging.INFO, format='%(asctime)s [sim] %(levelname)s: %(message)s')
logger = logging.getLogger('sim')

csv_path = os.path.join('{TABLES_DIR}', '{CSV_FILE}')
coils, regs = load_csv(csv_path)
logger.info("Carregado: %d coils, %d registers", len(coils), len(regs))

if not coils: coils = {{0: 0}}
if not regs: regs = {{0: 0}}

max_c = max(coils.keys())
max_r = max(regs.keys())

cv = [0] * (max_c + 2)
for a, v in coils.items(): cv[a] = v

rv = [0] * (max_r + 2)
for a, v in regs.items(): rv[a] = v

slave = ModbusSlaveContext(
    co=LoggingDataBlock(0, cv, "COIL"),
    hr=LoggingDataBlock(0, rv, "HREG"),
    zero_mode=True,
)
ctx = ModbusServerContext(slaves=slave, single=True)

framer = ModbusRtuFramer if '{protocol}' == 'rtu_tcp' else ModbusSocketFramer
logger.info("Simulador {protocol} porta {port} pronto")

asyncio.run(StartAsyncTcpServer(context=ctx, address=('0.0.0.0', {port}), framer=framer))
"""
    proc = subprocess.Popen(
        [VENV_PYTHON, '-c', script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    logger.info("Simulador %s iniciado (PID %d, porta %d)", protocol, proc.pid, port)
    return proc


def start_delfos(port, protocol):
    """Inicia o Delfos como subprocess."""
    env = dict(os.environ)
    env['MODBUS_HOST'] = 'localhost'
    env['MODBUS_PORT'] = str(port)
    env['MODBUS_UNIT_ID'] = '1'
    env['MODBUS_PROTOCOL'] = protocol
    env['REDIS_HOST'] = REDIS_HOST
    env['REDIS_PORT'] = str(REDIS_PORT)
    env['TABLES_DIR'] = TABLES_DIR

    script = os.path.join(GATEWAY_DIR, 'Delfos', 'delfos.py')
    proc = subprocess.Popen(
        [VENV_PYTHON, script],
        env=env,
        cwd=os.path.join(GATEWAY_DIR, 'Delfos'),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    logger.info("Delfos iniciado (PID %d, protocol=%s, port=%d)", proc.pid, protocol, port)
    return proc


def wait_for_redis_data(channel, timeout=20):
    """Espera dados no canal Redis via pub/sub."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
    pubsub = r.pubsub()
    pubsub.subscribe(channel)

    logger.info("Esperando dados no canal '%s' (timeout=%ds)...", channel, timeout)

    start = time.time()
    received = None

    for message in pubsub.listen():
        if time.time() - start > timeout:
            logger.warning("Timeout esperando dados no canal '%s'", channel)
            break
        if message['type'] != 'message':
            continue
        try:
            received = json.loads(message['data'].decode())
            logger.info("Dados recebidos no canal '%s'!", channel)
        except Exception:
            pass
        break

    pubsub.unsubscribe()
    pubsub.close()
    r.close()
    return received


def try_last_message(channel):
    """Tenta ler last_message:{channel} do Redis (fallback)."""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
    raw = r.get(f'last_message:{channel}')
    r.close()
    if raw:
        return json.loads(raw.decode())
    return None


def kill_proc(proc, label):
    """Para um subprocess."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            stdout, _ = proc.communicate(timeout=5)
            if stdout:
                lines = stdout.decode('utf-8', errors='replace').strip().split('\n')
                logger.info("Log do %s (últimas 15 linhas):", label)
                for line in lines[-15:]:
                    logger.info("  > %s", line)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        logger.info("%s parado (exit_code=%s)", label, proc.returncode)


def test_protocol(protocol, port):
    """Teste completo para um protocolo."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("  TESTE: protocol=%s  port=%d", protocol, port)
    logger.info("=" * 60)

    tags = get_sample_tags(15)
    if not tags:
        logger.error("Nenhuma tag encontrada no CSV!")
        return False

    setup_configs(port, protocol, tags)

    # Limpa Redis
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
    r.delete(f'last_message:{CHANNEL}', f'history:{CHANNEL}')
    r.close()

    sim_proc = None
    delfos_proc = None

    try:
        # Inicia simulador
        sim_proc = start_simulator(port, protocol)
        time.sleep(2)

        if sim_proc.poll() is not None:
            logger.error("Simulador morreu imediatamente! exit_code=%s", sim_proc.returncode)
            kill_proc(sim_proc, "Simulador")
            return False

        # Inicia Delfos
        delfos_proc = start_delfos(port, protocol)
        time.sleep(3)

        if delfos_proc.poll() is not None:
            logger.error("Delfos morreu imediatamente! exit_code=%s", delfos_proc.returncode)
            kill_proc(delfos_proc, "Delfos")
            kill_proc(sim_proc, "Simulador")
            return False

        # Espera dados no Redis
        data = wait_for_redis_data(CHANNEL, timeout=20)

        # Fallback: tenta last_message
        if data is None:
            logger.info("Tentando fallback via last_message:%s...", CHANNEL)
            time.sleep(3)
            data = try_last_message(CHANNEL)

        # Valida
        if data is None:
            logger.error("[%s] FALHOU — nenhum dado recebido no Redis!", protocol.upper())
            return False

        if 'coils' not in data or 'registers' not in data:
            logger.error("[%s] FALHOU — payload sem coils/registers: %s", protocol.upper(), list(data.keys()))
            return False

        n_coil_tags = sum(len(v) for v in data['coils'].values())
        n_reg_tags = sum(len(v) for v in data['registers'].values())
        total_tags = n_coil_tags + n_reg_tags

        logger.info("[%s] Payload recebido: %d coil tags, %d register tags, timestamp=%s",
                    protocol.upper(), n_coil_tags, n_reg_tags, data.get('timestamp', '?')[:19])

        # Mostra valores
        for section in ('coils', 'registers'):
            for key, tags_dict in data[section].items():
                for tag, val in list(tags_dict.items())[:5]:
                    logger.info("  [%s] %s.%s.%s = %s", protocol.upper(), section, key, tag, val)

        if total_tags == 0:
            logger.error("[%s] FALHOU — nenhuma tag nos dados!", protocol.upper())
            return False

        logger.info("[%s] SUCESSO — %d tags lidas do simulador via Redis!", protocol.upper(), total_tags)
        return True

    finally:
        kill_proc(delfos_proc, "Delfos")
        kill_proc(sim_proc, "Simulador")


def main():
    logger.info("Verificando Redis...")
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
        r.ping()
        r.close()
        logger.info("Redis OK")
    except Exception as e:
        logger.error("Redis não disponível: %s", e)
        return False

    results = {}

    # Teste 1: TCP puro
    results['tcp'] = test_protocol('tcp', 5020)
    time.sleep(2)

    # Teste 2: RTU over TCP
    results['rtu_tcp'] = test_protocol('rtu_tcp', 5021)

    # Restaura configs originais
    try:
        bak_gc = os.path.join(TABLES_DIR, 'group_config.json.bak')
        bak_vo = os.path.join(TABLES_DIR, 'variable_overrides.json.bak')
        if os.path.exists(bak_gc):
            os.replace(bak_gc, os.path.join(TABLES_DIR, 'group_config.json'))
        if os.path.exists(bak_vo):
            os.replace(bak_vo, os.path.join(TABLES_DIR, 'variable_overrides.json'))
        logger.info("Configs originais restauradas.")
    except Exception as e:
        logger.warning("Erro ao restaurar configs: %s", e)

    # Resultado final
    logger.info("")
    logger.info("=" * 60)
    logger.info("  RESULTADOS")
    logger.info("=" * 60)
    for proto, ok in results.items():
        status = "PASS" if ok else "FAIL"
        logger.info("  %-10s: %s", proto, status)
    logger.info("=" * 60)

    all_ok = all(results.values())
    if all_ok:
        logger.info("Todos os testes passaram!")
    else:
        logger.error("Alguns testes falharam.")
    return all_ok


if __name__ == '__main__':
    ok = main()
    sys.exit(0 if ok else 1)
