#!/usr/bin/env python3
"""
Teste de integração completa: Delfos + Atena rodando ao mesmo tempo.

Verifica o loop bidirecional end-to-end:

    Redis ch3 → Atena → Modbus simulator → Delfos → Redis ch2

Este script inicia e gerencia os três processos (simulador, Delfos, Atena)
automaticamente. Requer apenas Redis rodando.

Uso:
    python tests/test_full_loop.py
"""

import json
import logging
import os
import subprocess
import time
import unittest

import redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_full_loop")

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON      = os.path.join(GATEWAY_DIR, ".venv", "Scripts", "python")
REDIS_HOST  = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT  = int(os.environ.get("REDIS_PORT", 6379))

# Delfos cicla a ~1 Hz (0.5s de sleep + tempo de leitura Modbus).
# Usamos 2s como margem para Atena escrever + Delfos ler + publicar.
DELFOS_CYCLE = 2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain(pubsub, timeout: float = 0.05):
    """Descarta mensagens pendentes do pubsub."""
    while pubsub.get_message(timeout=timeout):
        pass


def wait_for_ch2(pubsub, key_path: list, expected, timeout: float = 8.0):
    """
    Aguarda até `timeout` s por uma mensagem em plc_data onde
    data[key_path[0]][key_path[1]]... == expected.

    Retorna o payload JSON completo se encontrado, None se timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = pubsub.get_message(timeout=0.1)
        if msg and msg["type"] == "message":
            try:
                data = json.loads(msg["data"])
                node = data
                for k in key_path:
                    node = node[k]
                if node == expected:
                    return data
            except (KeyError, TypeError, json.JSONDecodeError):
                pass
    return None


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

class TestFullLoop(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Verifica Redis
        cls.r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
        try:
            cls.r.ping()
        except Exception as exc:
            raise unittest.SkipTest(f"Redis não disponível: {exc}")

        logger.info("Iniciando ambiente de teste completo…")

        # Diretório de logs dos subprocessos
        log_dir = os.path.join(GATEWAY_DIR, "tests", "logs")
        os.makedirs(log_dir, exist_ok=True)

        # --- Simulador Modbus ------------------------------------------
        cls.sim = subprocess.Popen(
            [PYTHON, os.path.join(GATEWAY_DIR, "tests", "modbus_simulator.py")],
            cwd=GATEWAY_DIR,
            stdout=open(os.path.join(log_dir, "sim.log"),    "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)   # aguarda servidor subir

        # --- Delfos (leitor) -------------------------------------------
        cls.delfos = subprocess.Popen(
            [PYTHON, "delfos.py"],
            cwd=os.path.join(GATEWAY_DIR, "Delfos"),
            stdout=open(os.path.join(log_dir, "delfos.log"), "w"),
            stderr=subprocess.STDOUT,
        )

        # --- Atena (escritor) ------------------------------------------
        cls.atena = subprocess.Popen(
            [PYTHON, "atena.py"],
            cwd=os.path.join(GATEWAY_DIR, "Atena"),
            stdout=open(os.path.join(log_dir, "atena.log"),  "w"),
            stderr=subprocess.STDOUT,
        )

        time.sleep(2)   # aguarda conexões Modbus + Redis

        # Habilita user_state para que Atena processe plc_commands
        cls.r.publish("user_status", json.dumps({"user_state": True}))
        time.sleep(DELFOS_CYCLE)   # aguarda Delfos publicar ao menos 1 ciclo

        logger.info("Simulador + Delfos + Atena prontos.")

    @classmethod
    def tearDownClass(cls):
        cls.r.publish("user_status", json.dumps({"user_state": False}))
        time.sleep(0.3)
        for proc, name in [(cls.delfos, "Delfos"), (cls.atena, "Atena"), (cls.sim, "Simulador")]:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            logger.info("%s encerrado.", name)

    def _sub_ch2(self):
        """Retorna pubsub inscrito em plc_data com buffer drenado."""
        ps = self.r.pubsub()
        ps.subscribe("plc_data")
        ps.get_message(timeout=0.1)   # confirma subscription
        _drain(ps)
        return ps

    def _write(self, payload: dict):
        self.r.publish("plc_commands", json.dumps(payload))
        logger.info("→ plc_commands: %s", payload)

    # ------------------------------------------------------------------
    # Testes
    # ------------------------------------------------------------------

    def test_01_delfos_publica_plc_data(self):
        """Delfos deve estar publicando dados estruturados em plc_data."""
        raw = self.r.get("last_message:plc_data")
        self.assertIsNotNone(raw, "Nenhuma mensagem em plc_data — Delfos não publicou nada.")
        data = json.loads(raw)
        for campo in ("coils", "registers", "timestamp"):
            self.assertIn(campo, data, f"Campo '{campo}' ausente no plc_data")
        self.assertIn("Extrusora", data["registers"])
        self.assertIn("Puxador",   data["registers"])
        logger.info(
            "plc_data OK: %d namespaces de coils, %d de registers",
            len(data["coils"]), len(data["registers"]),
        )

    def test_02_delfos_publica_alarms(self):
        """Delfos deve publicar alarmes/configuração em alarms."""
        raw = self.r.get("last_message:alarms")
        self.assertIsNotNone(raw, "Nenhuma mensagem em alarms.")
        data = json.loads(raw)
        self.assertIn("coils",     data)
        self.assertIn("registers", data)
        self.assertIn("timestamp", data)
        logger.info("alarms OK.")

    def test_03_loop_register(self):
        """
        Loop register completo:
          plc_commands → Atena escreve no simulador → Delfos lê → plc_data reflete.
        """
        ps        = self._sub_ch2()
        setpoint  = 1350

        self._write({"Extrusora": {"extrusoraRefVelocidade": setpoint}})

        data = wait_for_ch2(
            ps,
            ["registers", "Extrusora", "extrusoraRefVelocidade"],
            setpoint,
            timeout=DELFOS_CYCLE * 5,
        )
        self.assertIsNotNone(
            data,
            f"plc_data não refletiu extrusoraRefVelocidade={setpoint} "
            f"após {DELFOS_CYCLE * 5:.0f}s",
        )
        logger.info("Loop register: setpoint %d refletido no plc_data  OK", setpoint)

    def test_04_loop_coil(self):
        """
        Loop coil completo:
          plc_commands liga extrusora → Atena escreve coil → Delfos lê → plc_data reflete.
        """
        ps = self._sub_ch2()

        self._write({"Extrusora": {"extrusoraLigaDesligaBotao": 1}})

        data = wait_for_ch2(
            ps,
            ["coils", "Extrusora", "extrusoraLigaDesligaBotao"],
            True,
            timeout=DELFOS_CYCLE * 5,
        )
        self.assertIsNotNone(
            data,
            "plc_data não refletiu extrusoraLigaDesligaBotao=True",
        )
        logger.info("Loop coil: extrusoraLigaDesligaBotao=True refletido  OK")

    def test_05_loop_multiplos_tags(self):
        """
        Escrita de múltiplos tags (Extrusora + Puxador) — ambos devem
        aparecer no plc_data.
        """
        ps    = self._sub_ch2()
        s_ext = 1600
        s_pux = 1100

        self._write({
            "Extrusora": {"extrusoraRefVelocidade": s_ext},
            "Puxador":   {"puxadorRefVelocidade":   s_pux},
        })

        deadline   = time.time() + DELFOS_CYCLE * 5
        extr_ok = pux_ok = False

        while time.time() < deadline:
            msg = ps.get_message(timeout=0.1)
            if msg and msg["type"] == "message":
                regs = json.loads(msg["data"]).get("registers", {})
                if regs.get("Extrusora", {}).get("extrusoraRefVelocidade") == s_ext:
                    extr_ok = True
                if regs.get("Puxador", {}).get("puxadorRefVelocidade") == s_pux:
                    pux_ok = True
                if extr_ok and pux_ok:
                    break

        self.assertTrue(extr_ok, f"extrusoraRefVelocidade={s_ext} não apareceu no plc_data")
        self.assertTrue(pux_ok,  f"puxadorRefVelocidade={s_pux} não apareceu no plc_data")
        logger.info("Múltiplos tags: extrusora=%d, puxador=%d  OK", s_ext, s_pux)

    def test_06_user_state_false_interrompe_loop(self):
        """
        Com user_state=False, Atena não deve escrever — plc_data não deve
        refletir o valor enviado no plc_commands.
        """
        ps = self._sub_ch2()

        # Define valor conhecido via escrita direta
        self.r.publish("plc_commands", json.dumps({"Extrusora": {"extrusoraRefVelocidade": 1500}}))
        time.sleep(DELFOS_CYCLE * 2)

        # Desabilita user_state
        self.r.publish("user_status", json.dumps({"user_state": False}))
        time.sleep(0.5)

        # Tenta escrever com user_state=False
        valor_bloqueado = 7777
        self._write({"Extrusora": {"extrusoraRefVelocidade": valor_bloqueado}})

        # plc_data NÃO deve conter o valor bloqueado
        data = wait_for_ch2(
            ps,
            ["registers", "Extrusora", "extrusoraRefVelocidade"],
            valor_bloqueado,
            timeout=DELFOS_CYCLE * 3,
        )
        self.assertIsNone(
            data,
            f"plc_data refletiu {valor_bloqueado} mesmo com user_state=False!",
        )
        logger.info("Bloqueio user_state=False confirmado  OK")

        # Reabilita para próximos testes
        self.r.publish("user_status", json.dumps({"user_state": True}))
        time.sleep(DELFOS_CYCLE)

    def test_07_processos_ainda_vivos(self):
        """Delfos, Atena e simulador devem estar rodando ao final dos testes."""
        for proc, name in [
            (self.sim,    "Simulador"),
            (self.delfos, "Delfos"),
            (self.atena,  "Atena"),
        ]:
            self.assertIsNone(
                proc.poll(),
                f"{name} encerrou inesperadamente (returncode={proc.poll()})",
            )
        logger.info("Todos os processos ainda ativos  OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
