#!/usr/bin/env python3
"""
Testes de integração Modbus para o gateway IoT.

Testa leitura e escrita diretamente contra o simulador (sem Redis),
usando a mesma biblioteca (pyModbusTCP) que Delfos e Atena usam em produção.

Pré-requisito:
    # Em outro terminal:
    python tests/modbus_simulator.py

Uso:
    python tests/test_integration.py
    MODBUS_PORT=502 python tests/test_integration.py   # porta personalizada
"""

import logging
import os
import sys
import unittest

from pyModbusTCP.client import ModbusClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_integration")

HOST = os.environ.get("MODBUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("MODBUS_PORT", "5020"))

# ---------------------------------------------------------------------------
# Endereços de referência (de operacao.csv)
# ---------------------------------------------------------------------------
COIL_PUXADOR_BASE    = 2150   # puxadorLigaDesliga … puxadorErro (grupos)
COIL_EXTRUSORA_BASE  = 2169   # extrusoraAutManEstado … extrusoraLigaDesligaBotao
COIL_EMERGENCIA      = 2048   # alarmes/emergencia
COIL_LIGA_DESLIGA    = 2173   # extrusoraLigaDesligaBotao

REG_EXTR_SPEED       = 39810  # extrusoraFeedBackSpeed
REG_EXTR_REF         = 40123  # extrusoraRefVelocidade
REG_PUX_SPEED        = 39791  # puxadorFeedBackSpeed
REG_LARGURA_PROG     = 4507   # larguraProgramada (configuração)
REG_NIVEL_A          = 40373  # nivelA (threeJs)


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
            (2048, 1,  "emergencia"),
            (2051, 4,  "capacitivos A-D"),
            (2150, 4,  "puxador coils"),
            (2169, 5,  "extrusora coils"),
        ]:
            result = self.client.read_coils(start, count)
            self.assertIsNotNone(result, f"Falha ao ler {label} em addr={start}")
            self.assertEqual(len(result), count)
            logger.info("  coils[%d:%d] (%s) = %s", start, start + count, label, result)

    def test_06_read_multiple_registers(self):
        """Leitura de registers em diferentes faixas de endereço."""
        addrs = [
            (REG_EXTR_SPEED, "extrusoraFeedBackSpeed"),
            (REG_PUX_SPEED,  "puxadorFeedBackSpeed"),
            (REG_EXTR_REF,   "extrusoraRefVelocidade"),
            (REG_LARGURA_PROG, "larguraProgramada"),
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
        coil_addr = 2171   # extrusoraLigadoDesligado
        reg_addr  = 40123  # extrusoraRefVelocidade
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
        # Grupos contíguos reais de operacao.csv
        coil_groups = [
            (2048, 1),   # [alarmes] emergencia
            (2051, 4),   # [threeJs] capacitivo A-D
            (2071, 3),   # [saidasDigitais] misturador, alimentandoMixer, ...
            (2077, 3),   # [saidasDigitais] compressorRadial, vacuo
            (2082, 2),   # [totalizadores]
            (2096, 2),   # [threeJs] vacuoA, vacuoB
            (2102, 2),   # [threeJs] vacuoC, vacuoD
            (2150, 4),   # [Puxador]
            (2156, 2),   # [Extrusora/Puxador] erros
            (2169, 5),   # [Extrusora] coils
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
        # Grupos (não todos são contíguos, mas os individuais sempre funcionam)
        register_groups = [
            (4507,  1),   # larguraProgramada
            (4526,  2),   # nivelMaximo, nivelMinimo
            (4542,  1),   # espessuraProgramada
            (4545,  4),   # percentual A-D
            (4553,  1),   # densidadeMedia
            (39772, 1),   # larguraAtual
            (39781, 1),   # kgHoraAtual
            (39791, 2),   # puxadorFeedBackSpeed, totalizadorKiloGrama
            (39794, 2),   # pesoBalanca, gramaMinutoAtual
            (39800, 1),   # kgHoraProgramado
            (39810, 1),   # extrusoraFeedBackSpeed
            (39812, 4),   # gramatura, espessura
            (39928, 1),   # totalizadorMetragem
            (40003, 1),   # puxadorRefVelocidade
            (40070, 1),   # puxadorProgramado
            (40123, 1),   # extrusoraRefVelocidade
            (40285, 1),   # gramaMinuto
            (40287, 1),   # espessuraAlgoritmo
            (40373, 4),   # nivelA-D
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
