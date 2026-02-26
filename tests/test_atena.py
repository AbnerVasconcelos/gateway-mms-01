#!/usr/bin/env python3
"""
Teste de integração do Atena.

Fluxo:
  1. Publica user_status → user_state=True  (habilita escrita)
  2. Publica plc_commands → dados de escrita (coil + register)
  3. Lê de volta do simulador Modbus para confirmar que o Atena escreveu

Pré-requisitos:
    python tests/modbus_simulator.py     # terminal 1
    cd Atena && python atena.py          # terminal 2  (ou iniciado por este script)
"""

import json
import logging
import os
import subprocess
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

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON      = os.path.join(GATEWAY_DIR, ".venv", "Scripts", "python")
REDIS_HOST  = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT  = int(os.environ.get("REDIS_PORT", 6379))
MODBUS_HOST = os.environ.get("MODBUS_HOST", "127.0.0.1")
MODBUS_PORT = 5021   # porta exclusiva deste módulo (evita conflito com outros suites)

# Delay após publicar para o Atena processar a mensagem
ATENA_DELAY = float(os.environ.get("ATENA_DELAY", "0.8"))

LOG_DIR = os.path.join(GATEWAY_DIR, "tests", "logs")


class TestAtenaEscrita(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.makedirs(LOG_DIR, exist_ok=True)

        # Conexão Redis
        cls.r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
        try:
            cls.r.ping()
        except Exception as e:
            raise unittest.SkipTest(f"Redis não acessível em {REDIS_HOST}:{REDIS_PORT}: {e}")

        # Inicia simulador Modbus na porta exclusiva deste suite
        cls.sim = subprocess.Popen(
            [PYTHON, os.path.join(GATEWAY_DIR, "tests", "modbus_simulator.py"),
             "--port", str(MODBUS_PORT)],
            cwd=GATEWAY_DIR,
            stdout=open(os.path.join(LOG_DIR, "atena_sim.log"), "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)

        # Inicia Atena apontando para o simulador deste suite
        _env = os.environ.copy()
        _env["MODBUS_HOST"] = MODBUS_HOST
        _env["MODBUS_PORT"] = str(MODBUS_PORT)
        cls.atena = subprocess.Popen(
            [PYTHON, "atena.py"],
            cwd=os.path.join(GATEWAY_DIR, "Atena"),
            env=_env,
            stdout=open(os.path.join(LOG_DIR, "atena_proc.log"), "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)

        # Conexão Modbus para verificação
        cls.mb = ModbusClient(MODBUS_HOST, MODBUS_PORT, auto_open=True)
        if not cls.mb.open():
            raise unittest.SkipTest(f"Simulador não acessível em {MODBUS_HOST}:{MODBUS_PORT}.")

        # Habilita user_state — sem isso o Atena ignora plc_commands
        logger.info("Publicando user_status: user_state=True")
        cls.r.publish("user_status", json.dumps({"user_state": True}))
        time.sleep(ATENA_DELAY)
        logger.info("Simulador + Atena prontos.")

    @classmethod
    def tearDownClass(cls):
        cls.r.publish("user_status", json.dumps({"user_state": False}))
        time.sleep(0.3)
        cls.mb.close()
        for proc, name in [(cls.atena, "Atena"), (cls.sim, "Simulador")]:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            logger.info("%s encerrado.", name)

    # ------------------------------------------------------------------

    def _publish_ch3(self, payload: dict):
        data = json.dumps(payload)
        self.r.publish("plc_commands", data)
        logger.info("plc_commands → %s", data)
        time.sleep(ATENA_DELAY)

    # ------------------------------------------------------------------

    def test_01_escreve_coil(self):
        """Atena deve escrever coil no simulador ao receber plc_commands."""
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
        """Atena deve escrever register no simulador ao receber plc_commands."""
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
        """Com user_state=False, Atena deve ignorar plc_commands."""
        addr = 40123  # extrusoraRefVelocidade

        # Define valor conhecido
        self.mb.write_single_register(addr, 9999)
        time.sleep(0.1)

        # Desabilita user_state
        self.r.publish("user_status", json.dumps({"user_state": False}))
        time.sleep(ATENA_DELAY)

        # Tenta escrever via plc_commands — não deve surtir efeito
        self._publish_ch3({"Extrusora": {"extrusoraRefVelocidade": 1111}})

        result = self.mb.read_holding_registers(addr, 1)
        self.assertIsNotNone(result)
        self.assertEqual(
            result[0], 9999,
            f"Register {addr} foi alterado mesmo com user_state=False! valor={result[0]}",
        )
        logger.info("Bloqueio user_state=False: register permaneceu em 9999  OK")

        # Reabilita para os próximos testes
        self.r.publish("user_status", json.dumps({"user_state": True}))
        time.sleep(ATENA_DELAY)

    def test_06_tag_inexistente_nao_causa_erro(self):
        """Tag desconhecida no plc_commands não deve travar o Atena."""
        self._publish_ch3({"NaoExiste": {"tagFalsa": 42}})
        # Se Atena ainda está vivo, conseguimos publicar novamente
        self.r.publish("plc_commands", json.dumps({"Extrusora": {"extrusoraRefVelocidade": 1400}}))
        time.sleep(ATENA_DELAY)
        result = self.mb.read_holding_registers(40123, 1)
        self.assertIsNotNone(result, "Atena parou após tag inexistente")
        self.assertEqual(result[0], 1400)
        logger.info("Tag inexistente tratada sem erro; Atena continua operacional  OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
