#!/usr/bin/env python3
"""
Testes de integração Modbus para o gateway IoT.

Testa leitura e escrita diretamente contra o simulador (sem Redis),
usando a mesma biblioteca (pyModbusTCP) que Delfos e Atena usam em produção.

O simulador é iniciado automaticamente neste módulo (porta 5020).

Uso:
    python tests/test_integration.py
"""

import logging
import os
import subprocess
import sys
import time
import unittest

from pyModbusTCP.client import ModbusClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_integration")

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if sys.platform == 'win32':
    PYTHON = os.path.join(GATEWAY_DIR, '.venv', 'Scripts', 'python')
else:
    PYTHON = os.path.join(GATEWAY_DIR, '.venv', 'bin', 'python')
HOST        = os.environ.get("MODBUS_HOST", "127.0.0.1")
PORT        = 5020   # porta exclusiva deste módulo

LOG_DIR = os.path.join(GATEWAY_DIR, "tests", "logs")

_simulator_proc = None


def setUpModule():
    global _simulator_proc
    os.makedirs(LOG_DIR, exist_ok=True)
    _simulator_proc = subprocess.Popen(
        [PYTHON, os.path.join(GATEWAY_DIR, "tests", "modbus_simulator.py"),
         "--port", str(PORT)],
        cwd=GATEWAY_DIR,
        stdout=open(os.path.join(LOG_DIR, "integration_sim.log"), "w"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(2)
    logger.info("Simulador iniciado na porta %d.", PORT)


def tearDownModule():
    if _simulator_proc is not None:
        _simulator_proc.terminate()
        try:
            _simulator_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _simulator_proc.kill()
        logger.info("Simulador encerrado.")

# ---------------------------------------------------------------------------
# Endereços de referência (de mapeamento_clp.csv — atualizado conforme CSVs atuais)
# ---------------------------------------------------------------------------
COIL_PUXADOR_BASE    = 16016  # di101, di102, di103 (contiguous group of 32)
COIL_EXTRUSORA_BASE  = 48045  # indDesvTemp, ligaDeslCalandra, extrusoraManualAutomatico
COIL_EMERGENCIA      = 48000  # resetAlarme (contiguous group of 15)
COIL_LIGA_DESLIGA    = 48002  # ligaDeslBomba

REG_EXTR_SPEED       = 8478   # prearrasteMpm
REG_EXTR_REF         = 28015  # tempoCorte
REG_PUX_SPEED        = 8479   # correntePrearraste
REG_LARGURA_PROG     = 28000  # fatorAjusteCal1
REG_NIVEL_A          = 5003   # ai1


class TestLeitura(unittest.TestCase):
    """Testes de leitura — verifica que o simulador responde com dados válidos."""

    @classmethod
    def setUpClass(cls):
        cls.client = ModbusClient(HOST, PORT, auto_open=True)
        if not cls.client.open():
            raise unittest.SkipTest(
                f"Simulador não acessível em {HOST}:{PORT}.\n"
                "Execute: python tests/modbus_simulator.py"
            )
        logger.info("Conectado ao simulador em %s:%d", HOST, PORT)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_01_read_coils_returns_booleans(self):
        """read_coils deve retornar lista de booleanos."""
        result = self.client.read_coils(COIL_PUXADOR_BASE, 5)
        self.assertIsNotNone(result, "read_coils() retornou None")
        self.assertEqual(len(result), 5)
        for v in result:
            self.assertIn(v, (True, False, 0, 1))
        logger.info("coils[%d:%d] = %s", COIL_PUXADOR_BASE, COIL_PUXADOR_BASE + 5, result)

    def test_02_read_holding_register_is_int(self):
        """read_holding_registers deve retornar inteiros no intervalo Modbus."""
        result = self.client.read_holding_registers(REG_EXTR_SPEED, 1)
        self.assertIsNotNone(result, "read_holding_registers() retornou None")
        self.assertIsInstance(result[0], int)
        self.assertGreaterEqual(result[0], 0)
        self.assertLessEqual(result[0], 65535)
        logger.info("extrusoraFeedBackSpeed (addr=%d) = %d", REG_EXTR_SPEED, result[0])

    def test_03_read_config_register(self):
        """Registers de configuração de baixo endereço devem ser acessíveis."""
        result = self.client.read_holding_registers(REG_LARGURA_PROG, 1)
        self.assertIsNotNone(result, "Leitura de larguraProgramada retornou None")
        self.assertIsInstance(result[0], int)
        logger.info("larguraProgramada (addr=%d) = %d", REG_LARGURA_PROG, result[0])

    def test_04_read_high_address_register(self):
        """Registers de endereços altos (threeJs) devem ser acessíveis."""
        result = self.client.read_holding_registers(REG_NIVEL_A, 1)
        self.assertIsNotNone(result, f"Leitura do addr={REG_NIVEL_A} retornou None")
        logger.info("nivelA (addr=%d) = %d", REG_NIVEL_A, result[0])

    def test_05_read_contiguous_group_coils(self):
        """Leitura contígua de coils (comportamento do find_contiguous_groups)."""
        # Delfos lê grupos de endereços contíguos em uma única chamada
        for start, count, label in [
            (16000, 5,  "entrada digital base"),
            (16016, 4,  "entrada digital slot1"),
            (48000, 3,  "resetAlarme, habBloqTemp, ligaDeslBomba"),
            (40416, 4,  "upExtr, downExtr, enrateExtr, emergExtr"),
        ]:
            result = self.client.read_coils(start, count)
            self.assertIsNotNone(result, f"Falha ao ler {label} em addr={start}")
            self.assertEqual(len(result), count)
            logger.info("  coils[%d:%d] (%s) = %s", start, start + count, label, result)

    def test_06_read_multiple_registers(self):
        """Leitura de registers em diferentes faixas de endereço."""
        addrs = [
            (REG_EXTR_SPEED, "prearrasteMpm"),
            (REG_PUX_SPEED,  "correntePrearraste"),
            (REG_EXTR_REF,   "tempoCorte"),
            (REG_LARGURA_PROG, "fatorAjusteCal1"),
        ]
        for addr, label in addrs:
            result = self.client.read_holding_registers(addr, 1)
            self.assertIsNotNone(result, f"Falha ao ler {label} (addr={addr})")
            self.assertIsInstance(result[0], int)
            logger.info("  register[%d] (%s) = %d", addr, label, result[0])


class TestEscrita(unittest.TestCase):
    """Testes de escrita — verifica persistência e round-trip read-after-write."""

    @classmethod
    def setUpClass(cls):
        cls.client = ModbusClient(HOST, PORT, auto_open=True)
        if not cls.client.open():
            raise unittest.SkipTest(
                f"Simulador não acessível em {HOST}:{PORT}.\n"
                "Execute: python tests/modbus_simulator.py"
            )

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_10_write_coil_true_persists(self):
        """Escrita True em coil deve ser lida de volta como True."""
        addr = COIL_LIGA_DESLIGA
        ok = self.client.write_single_coil(addr, True)
        self.assertTrue(ok, f"write_single_coil({addr}, True) retornou False")
        result = self.client.read_coils(addr, 1)
        self.assertIsNotNone(result)
        self.assertTrue(result[0], f"Coil {addr} deveria ser True após escrita")
        logger.info("write_coil(%d, True) → read=%s  OK", addr, result[0])

    def test_11_write_coil_false_persists(self):
        """Escrita False em coil deve ser lida de volta como False."""
        addr = COIL_LIGA_DESLIGA
        self.assertTrue(self.client.write_single_coil(addr, False))
        result = self.client.read_coils(addr, 1)
        self.assertIsNotNone(result)
        self.assertFalse(result[0])
        logger.info("write_coil(%d, False) → read=%s  OK", addr, result[0])

    def test_12_write_register_value_persists(self):
        """Escrita de valor em register deve ser lida de volta igual."""
        addr       = REG_EXTR_REF
        test_value = 1450
        self.assertTrue(self.client.write_single_register(addr, test_value))
        result = self.client.read_holding_registers(addr, 1)
        self.assertIsNotNone(result)
        self.assertEqual(
            result[0], test_value,
            f"Register {addr}: esperado={test_value}, recebido={result[0]}",
        )
        logger.info("write_register(%d, %d) → read=%d  OK", addr, test_value, result[0])

    def test_13_write_register_boundary_zero(self):
        """Escrever 0 (mínimo) deve persistir."""
        self.assertTrue(self.client.write_single_register(REG_EXTR_REF, 0))
        result = self.client.read_holding_registers(REG_EXTR_REF, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 0)

    def test_14_write_register_boundary_max(self):
        """Escrever 65535 (máximo Modbus) deve persistir."""
        self.assertTrue(self.client.write_single_register(REG_EXTR_REF, 65535))
        result = self.client.read_holding_registers(REG_EXTR_REF, 1)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 65535)
        logger.info("write_register(%d, 65535) → read=%d  OK", REG_EXTR_REF, result[0])

    def test_15_write_sequence_coil_and_register(self):
        """
        Sequência realista: liga equipamento (coil) + define setpoint (register).
        Simula o que handle_plc_commands_message do Atena faz ao receber ch3.
        """
        coil_addr = COIL_EMERGENCIA   # resetAlarme
        reg_addr  = REG_EXTR_REF     # tempoCorte
        setpoint  = 1450

        # Escreve
        self.assertTrue(self.client.write_single_coil(coil_addr, True))
        self.assertTrue(self.client.write_single_register(reg_addr, setpoint))

        # Lê de volta
        coils = self.client.read_coils(coil_addr, 1)
        regs  = self.client.read_holding_registers(reg_addr, 1)

        self.assertIsNotNone(coils)
        self.assertIsNotNone(regs)
        self.assertTrue(coils[0])
        self.assertEqual(regs[0], setpoint)
        logger.info(
            "Sequência Atena: coil[%d]=%s, register[%d]=%d  OK",
            coil_addr, coils[0], reg_addr, regs[0],
        )

    def test_16_write_multiple_registers_roundtrip(self):
        """Escreve em registers independentes e lê todos de volta."""
        writes = {
            REG_EXTR_REF:   1500,
            REG_LARGURA_PROG: 900,
        }
        for addr, val in writes.items():
            self.assertTrue(self.client.write_single_register(addr, val))

        for addr, expected in writes.items():
            result = self.client.read_holding_registers(addr, 1)
            self.assertIsNotNone(result)
            self.assertEqual(result[0], expected, f"addr={addr}")
            logger.info("  register[%d] = %d  OK", addr, result[0])


class TestLeituraDelfos(unittest.TestCase):
    """
    Simula o loop completo de leitura do Delfos:
    grupos contíguos de coils e registers conforme find_contiguous_groups().
    """

    @classmethod
    def setUpClass(cls):
        cls.client = ModbusClient(HOST, PORT, auto_open=True)
        if not cls.client.open():
            raise unittest.SkipTest(
                f"Simulador não acessível em {HOST}:{PORT}.\n"
                "Execute: python tests/modbus_simulator.py"
            )

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_20_delfos_coil_groups(self):
        """Lê todos os grupos de coils contíguos como o Delfos faria."""
        # Grupos contíguos reais de mapeamento_clp.csv
        coil_groups = [
            (16000, 10),  # entrada digital base
            (16016, 32),  # entrada digital slot1
            (40416, 4),   # upExtr, downExtr, enrateExtr, emergExtr
            (48000, 15),  # resetAlarme .. habBloqPuxador
            (48045, 9),   # indDesvTemp .. ligaCanBob2
            (48080, 2),   # ligaDeslPrearraste, ligaDeslAlimentador
            (48083, 8),   # alCanBomba .. alCanBob2
            (48095, 2),   # bob1Completo, bob2Completo
        ]
        total_read = 0
        for start, count in coil_groups:
            result = self.client.read_coils(start, count)
            self.assertIsNotNone(result, f"Falha: read_coils({start}, {count})")
            self.assertEqual(len(result), count)
            total_read += count
        logger.info("Leitura Delfos: %d grupos, %d coils total  OK", len(coil_groups), total_read)

    def test_21_delfos_register_groups(self):
        """Lê todos os grupos de registers contíguos como o Delfos faria."""
        # Grupos contíguos reais de mapeamento_clp.csv
        register_groups = [
            (5003,  11),  # ai1 .. ai105
            (6775,  1),   # register isolado
            (8005,  12),  # alarmesWord1 .. alarmesWord12
            (8025,  1),   # register isolado
            (8027,  4),   # grupo de 4
            (8478,  11),  # prearrasteMpm .. grupo de 11
            (28000, 10),  # fatorAjusteCal1 .. fatorAjustePuxador
            (28015, 18),  # tempoCorte .. comprimentoCorte + contíguos
        ]
        total_read = 0
        for start, count in register_groups:
            result = self.client.read_holding_registers(start, count)
            self.assertIsNotNone(result, f"Falha: read_holding_registers({start}, {count})")
            self.assertEqual(len(result), count)
            total_read += count
        logger.info(
            "Leitura Delfos: %d grupos, %d registers total  OK",
            len(register_groups), total_read,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
