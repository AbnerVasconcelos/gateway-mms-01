#!/usr/bin/env python3
"""
Teste de integração do Atena.

Fluxo:
  1. Publica channel1 → user_state=True  (habilita escrita)
  2. Publica channel3 → dados de escrita (coil + register)
  3. Lê de volta do simulador Modbus para confirmar que o Atena escreveu

Pré-requisitos:
    python tests/modbus_simulator.py     # terminal 1
    cd Atena && python atena.py          # terminal 2  (ou iniciado por este script)
"""

import json
import logging
import os
import time
import unittest

import redis
from pyModbusTCP.client import ModbusClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_atena")

REDIS_HOST   = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT   = int(os.environ.get("REDIS_PORT", 6379))
MODBUS_HOST  = os.environ.get("MODBUS_HOST", "127.0.0.1")
MODBUS_PORT  = int(os.environ.get("MODBUS_PORT", 5020))

# Delay após publicar para o Atena processar a mensagem
ATENA_DELAY = float(os.environ.get("ATENA_DELAY", "0.8"))


class TestAtenaEscrita(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Conexão Redis
        cls.r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
        try:
            cls.r.ping()
        except Exception as e:
            raise unittest.SkipTest(f"Redis não acessível em {REDIS_HOST}:{REDIS_PORT}: {e}")

        # Conexão Modbus (simulador)
        cls.mb = ModbusClient(MODBUS_HOST, MODBUS_PORT, auto_open=True)
        if not cls.mb.open():
            raise unittest.SkipTest(
                f"Simulador não acessível em {MODBUS_HOST}:{MODBUS_PORT}.\n"
                "Execute: python tests/modbus_simulator.py"
            )

        # Habilita user_state — sem isso o Atena ignora channel3
        logger.info("Publicando channel1: user_state=True")
        cls.r.publish("channel1", json.dumps({"user_state": True}))
        time.sleep(ATENA_DELAY)
        logger.info("Conectado ao Redis e ao simulador.")

    @classmethod
    def tearDownClass(cls):
        cls.mb.close()

    # ------------------------------------------------------------------

    def _publish_ch3(self, payload: dict):
        data = json.dumps(payload)
        self.r.publish("channel3", data)
        logger.info("channel3 → %s", data)
        time.sleep(ATENA_DELAY)

    # ------------------------------------------------------------------

    def test_01_escreve_coil(self):
        """Atena deve escrever coil no simulador ao receber channel3."""
        addr = 2173   # extrusoraLigaDesligaBotao

        # Garante estado inicial False
        self.mb.write_single_coil(addr, False)
        time.sleep(0.1)

        self._publish_ch3({"Extrusora": {"extrusoraLigaDesligaBotao": 1}})

        result = self.mb.read_coils(addr, 1)
        self.assertIsNotNone(result, "Leitura do simulador retornou None")
        self.assertTrue(result[0], f"Coil {addr} deveria ser True após escrita pelo Atena")
        logger.info("extrusoraLigaDesligaBotao (addr=%d) = %s  OK", addr, result[0])

    def test_02_escreve_register(self):
        """Atena deve escrever register no simulador ao receber channel3."""
        addr      = 40123   # extrusoraRefVelocidade
        setpoint  = 1450

        self._publish_ch3({"Extrusora": {"extrusoraRefVelocidade": setpoint}})

        result = self.mb.read_holding_registers(addr, 1)
        self.assertIsNotNone(result, "Leitura do simulador retornou None")
        self.assertEqual(
            result[0], setpoint,
            f"Register {addr}: esperado={setpoint}, recebido={result[0]}",
        )
        logger.info("extrusoraRefVelocidade (addr=%d) = %d  OK", addr, result[0])

    def test_03_escreve_coil_e_register_simultaneos(self):
        """Atena deve escrever coil e register no mesmo comando."""
        coil_addr = 2171   # extrusoraLigadoDesligado
        reg_addr  = 40123  # extrusoraRefVelocidade
        setpoint  = 1500

        self._publish_ch3({
            "Extrusora": {
                "extrusoraLigadoDesligado": 1,
                "extrusoraRefVelocidade": setpoint,
            }
        })

        coil = self.mb.read_coils(coil_addr, 1)
        reg  = self.mb.read_holding_registers(reg_addr, 1)

        self.assertIsNotNone(coil)
        self.assertIsNotNone(reg)
        self.assertTrue(coil[0], f"Coil {coil_addr} deveria ser True")
        self.assertEqual(reg[0], setpoint, f"Register {reg_addr}: esperado={setpoint}, recebido={reg[0]}")
        logger.info("Escrita simultânea: coil[%d]=%s, reg[%d]=%d  OK", coil_addr, coil[0], reg_addr, reg[0])

    def test_04_escreve_puxador(self):
        """Atena deve escrever tags do Puxador corretamente."""
        coil_addr = 2150   # puxadorLigaDesliga
        reg_addr  = 40003  # puxadorRefVelocidade
        setpoint  = 1200

        self._publish_ch3({
            "Puxador": {
                "puxadorLigaDesliga": 1,
                "puxadorRefVelocidade": setpoint,
            }
        })

        coil = self.mb.read_coils(coil_addr, 1)
        reg  = self.mb.read_holding_registers(reg_addr, 1)

        self.assertIsNotNone(coil)
        self.assertIsNotNone(reg)
        self.assertTrue(coil[0])
        self.assertEqual(reg[0], setpoint)
        logger.info("Puxador: coil[%d]=%s, reg[%d]=%d  OK", coil_addr, coil[0], reg_addr, reg[0])

    def test_05_user_state_false_bloqueia_escrita(self):
        """Com user_state=False, Atena deve ignorar channel3."""
        addr = 40123  # extrusoraRefVelocidade

        # Define valor conhecido
        self.mb.write_single_register(addr, 9999)
        time.sleep(0.1)

        # Desabilita user_state
        self.r.publish("channel1", json.dumps({"user_state": False}))
        time.sleep(ATENA_DELAY)

        # Tenta escrever via channel3 — não deve surtir efeito
        self._publish_ch3({"Extrusora": {"extrusoraRefVelocidade": 1111}})

        result = self.mb.read_holding_registers(addr, 1)
        self.assertIsNotNone(result)
        self.assertEqual(
            result[0], 9999,
            f"Register {addr} foi alterado mesmo com user_state=False! valor={result[0]}",
        )
        logger.info("Bloqueio user_state=False: register permaneceu em 9999  OK")

        # Reabilita para os próximos testes
        self.r.publish("channel1", json.dumps({"user_state": True}))
        time.sleep(ATENA_DELAY)

    def test_06_tag_inexistente_nao_causa_erro(self):
        """Tag desconhecida no channel3 não deve travar o Atena."""
        self._publish_ch3({"NaoExiste": {"tagFalsa": 42}})
        # Se Atena ainda está vivo, conseguimos publicar novamente
        self.r.publish("channel3", json.dumps({"Extrusora": {"extrusoraRefVelocidade": 1400}}))
        time.sleep(ATENA_DELAY)
        result = self.mb.read_holding_registers(40123, 1)
        self.assertIsNotNone(result, "Atena parou após tag inexistente")
        self.assertEqual(result[0], 1400)
        logger.info("Tag inexistente tratada sem erro; Atena continua operacional  OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
