#!/usr/bin/env python3
"""
Teste de integração completa: Delfos + Atena rodando ao mesmo tempo.

Verifica o loop bidirecional end-to-end:

    Redis simulador_commands → Atena → Modbus simulator → Delfos → Redis plc_preArraste/plc_operacao

Este script inicia e gerencia os três processos (simulador, Delfos, Atena)
automaticamente. Requer apenas Redis rodando.

Uso:
    python tests/test_full_loop.py
"""

import json
import logging
import os
import subprocess
import sys
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
if sys.platform == 'win32':
    PYTHON = os.path.join(GATEWAY_DIR, '.venv', 'Scripts', 'python')
else:
    PYTHON = os.path.join(GATEWAY_DIR, '.venv', 'bin', 'python')
REDIS_HOST  = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT  = int(os.environ.get("REDIS_PORT", 6379))
MODBUS_HOST = "127.0.0.1"
MODBUS_PORT = 5022   # porta exclusiva deste módulo (evita conflito com outros suites)

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
    Aguarda até `timeout` s por uma mensagem no canal subscrito onde
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

        # Env com porta exclusiva deste suite (override do .env)
        _env = os.environ.copy()
        _env["MODBUS_HOST"] = MODBUS_HOST
        _env["MODBUS_PORT"] = str(MODBUS_PORT)
        _env["DEVICE_ID"] = "simulador"
        _env["COMMAND_CHANNEL"] = "simulador_commands"

        # --- Simulador Modbus ------------------------------------------
        cls.sim = subprocess.Popen(
            [PYTHON, os.path.join(GATEWAY_DIR, "tests", "modbus_simulator.py"),
             "--port", str(MODBUS_PORT)],
            cwd=GATEWAY_DIR,
            stdout=open(os.path.join(log_dir, "sim.log"),    "w"),
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)   # aguarda servidor subir

        # --- Delfos (leitor) -------------------------------------------
        cls.delfos = subprocess.Popen(
            [PYTHON, "delfos.py"],
            cwd=os.path.join(GATEWAY_DIR, "Delfos"),
            env=_env,
            stdout=open(os.path.join(log_dir, "delfos.log"), "w"),
            stderr=subprocess.STDOUT,
        )

        # --- Atena (escritor) ------------------------------------------
        cls.atena = subprocess.Popen(
            [PYTHON, "atena.py"],
            cwd=os.path.join(GATEWAY_DIR, "Atena"),
            env=_env,
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

    def _sub_channel(self, channel):
        """Retorna pubsub inscrito no canal indicado com buffer drenado."""
        ps = self.r.pubsub()
        ps.subscribe(channel)
        ps.get_message(timeout=0.1)   # confirma subscription
        _drain(ps)
        return ps

    def _sub_ch2(self):
        """Retorna pubsub inscrito em plc_preArraste com buffer drenado."""
        return self._sub_channel("plc_preArraste")

    def _write(self, payload: dict):
        self.r.publish("simulador_commands", json.dumps(payload))
        logger.info("→ simulador_commands: %s", payload)

    # ------------------------------------------------------------------
    # Testes
    # ------------------------------------------------------------------

    def test_01_delfos_publica_plc_data(self):
        """Delfos deve estar publicando dados estruturados em plc_preArraste."""
        raw = self.r.get("last_message:plc_preArraste")
        self.assertIsNotNone(raw, "Nenhuma mensagem em plc_preArraste — Delfos não publicou nada.")
        data = json.loads(raw)
        for campo in ("coils", "registers", "timestamp"):
            self.assertIn(campo, data, f"Campo '{campo}' ausente no plc_preArraste")
        self.assertIn("corte", data["registers"])
        logger.info(
            "plc_preArraste OK: %d namespaces de coils, %d de registers",
            len(data["coils"]), len(data["registers"]),
        )

    def test_02_delfos_publica_alarms(self):
        """Delfos deve publicar alarmes/configuração em plc_alarmes."""
        raw = self.r.get("last_message:plc_alarmes")
        self.assertIsNotNone(raw, "Nenhuma mensagem em plc_alarmes.")
        data = json.loads(raw)
        self.assertIn("coils",     data)
        self.assertIn("registers", data)
        self.assertIn("timestamp", data)
        logger.info("plc_alarmes OK.")

    def test_03_loop_register(self):
        """
        Loop register completo:
          simulador_commands → Atena escreve no simulador → Delfos lê → plc_preArraste reflete.
        """
        ps        = self._sub_ch2()
        setpoint  = 1350

        self._write({"corte": {"tempoCorte": setpoint}})

        data = wait_for_ch2(
            ps,
            ["registers", "corte", "tempoCorte"],
            setpoint,
            timeout=DELFOS_CYCLE * 5,
        )
        self.assertIsNotNone(
            data,
            f"plc_preArraste não refletiu tempoCorte={setpoint} "
            f"após {DELFOS_CYCLE * 5:.0f}s",
        )
        logger.info("Loop register: setpoint %d refletido no plc_preArraste  OK", setpoint)

    def test_04_loop_coil(self):
        """
        Loop coil completo:
          simulador_commands liga resetAlarme → Atena escreve coil → Delfos lê → plc_operacao reflete.
        """
        ps = self._sub_channel("plc_operacao")

        self._write({"sistema": {"resetAlarme": 1}})

        data = wait_for_ch2(
            ps,
            ["coils", "sistema", "resetAlarme"],
            True,
            timeout=DELFOS_CYCLE * 5,
        )
        self.assertIsNotNone(
            data,
            "plc_operacao não refletiu resetAlarme=True",
        )
        logger.info("Loop coil: resetAlarme=True refletido  OK")

    def test_05_loop_multiplos_tags(self):
        """
        Escrita de múltiplos tags (tempoCorte + comprimentoCorte) — ambos devem
        aparecer no plc_preArraste.
        """
        ps      = self._sub_ch2()
        s_tempo = 1600
        s_compr = 1100

        self._write({
            "corte": {"tempoCorte": s_tempo, "comprimentoCorte": s_compr},
        })

        deadline   = time.time() + DELFOS_CYCLE * 5
        tempo_ok = compr_ok = False

        while time.time() < deadline:
            msg = ps.get_message(timeout=0.1)
            if msg and msg["type"] == "message":
                regs = json.loads(msg["data"]).get("registers", {})
                if regs.get("corte", {}).get("tempoCorte") == s_tempo:
                    tempo_ok = True
                if regs.get("corte", {}).get("comprimentoCorte") == s_compr:
                    compr_ok = True
                if tempo_ok and compr_ok:
                    break

        self.assertTrue(tempo_ok,  f"tempoCorte={s_tempo} não apareceu no plc_preArraste")
        self.assertTrue(compr_ok,  f"comprimentoCorte={s_compr} não apareceu no plc_preArraste")
        logger.info("Múltiplos tags: tempoCorte=%d, comprimentoCorte=%d  OK", s_tempo, s_compr)

    def test_06_user_state_false_interrompe_loop(self):
        """
        Com user_state=False, Atena não deve escrever — plc_preArraste não deve
        refletir o valor enviado no simulador_commands.
        """
        ps = self._sub_ch2()

        # Define valor conhecido via escrita direta
        self.r.publish("simulador_commands", json.dumps({"corte": {"tempoCorte": 1500}}))
        time.sleep(DELFOS_CYCLE * 2)

        # Desabilita user_state
        self.r.publish("user_status", json.dumps({"user_state": False}))
        time.sleep(0.5)

        # Tenta escrever com user_state=False
        valor_bloqueado = 7777
        self._write({"corte": {"tempoCorte": valor_bloqueado}})

        # plc_preArraste NÃO deve conter o valor bloqueado
        data = wait_for_ch2(
            ps,
            ["registers", "corte", "tempoCorte"],
            valor_bloqueado,
            timeout=DELFOS_CYCLE * 3,
        )
        self.assertIsNone(
            data,
            f"plc_preArraste refletiu {valor_bloqueado} mesmo com user_state=False!",
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
