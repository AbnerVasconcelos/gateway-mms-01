#!/usr/bin/env python3
"""
Testes da Fase 2 — Hub: Socket.IO bridge + Config Panel.

Duas suites:

  TestConfigStore      — unitária, sem dependências externas.
                         Usa diretório temporário para isolar os arquivos de config.

  TestHubIntegration   — inicia o Hub como subprocess + conecta cliente Socket.IO real.
                         Requer Redis rodando. Ignorada automaticamente se indisponível.

Uso:
    python -m pytest tests/test_hub.py -v
    python -m pytest tests/test_hub.py -v -k TestConfigStore   # só unitários
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import redis

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('test_hub')

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON      = os.path.join(GATEWAY_DIR, '.venv', 'Scripts', 'python')
REDIS_HOST  = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT  = int(os.environ.get('REDIS_PORT', 6379))
HUB_PORT    = 8765   # porta exclusiva para testes — evita conflito com Hub em produção
TABLES_DIR  = os.path.join(GATEWAY_DIR, 'tables')
LOG_DIR     = os.path.join(GATEWAY_DIR, 'tests', 'logs')

sys.path.insert(0, GATEWAY_DIR)
sys.path.insert(0, os.path.join(GATEWAY_DIR, 'Hub'))
import config_store  # noqa: E402


# ---------------------------------------------------------------------------
# Suite 1 — TestConfigStore (unitário, sem deps externas)
# ---------------------------------------------------------------------------

class TestConfigStore(unittest.TestCase):
    """
    Testa config_store.py isolado, usando um diretório temporário para não
    modificar os arquivos reais de configuração.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        shutil.copy(
            os.path.join(TABLES_DIR, 'group_config.json'),
            os.path.join(self.temp_dir, 'group_config.json'),
        )
        shutil.copy(
            os.path.join(TABLES_DIR, 'variable_overrides.json'),
            os.path.join(self.temp_dir, 'variable_overrides.json'),
        )
        self._orig_tables_dir = config_store._TABLES_DIR
        config_store._TABLES_DIR = self.temp_dir

    def tearDown(self):
        config_store._TABLES_DIR = self._orig_tables_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ── load_group_config ────────────────────────────────────────────────────

    def test_01_load_group_config_returns_dict(self):
        cfg = config_store.load_group_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn('groups', cfg)
        self.assertIn('_meta', cfg)

    def test_02_load_group_config_has_expected_groups(self):
        cfg = config_store.load_group_config()
        expected = {'Extrusora', 'Puxador', 'alarmes', 'threeJs', 'plc_config'}
        present  = set(cfg['groups'].keys())
        for group in ('Extrusora', 'Puxador', 'alarmes', 'threeJs'):
            self.assertIn(group, present, f"Grupo '{group}' ausente em group_config.json")

    # ── save_group_config ────────────────────────────────────────────────────

    def test_03_save_group_config_roundtrip(self):
        cfg = config_store.load_group_config()
        cfg['_meta']['test_marker'] = 'roundtrip'
        config_store.save_group_config(cfg)

        reloaded = config_store.load_group_config()
        self.assertEqual(reloaded['_meta']['test_marker'], 'roundtrip')

    def test_04_save_group_config_preserves_all_groups(self):
        cfg = config_store.load_group_config()
        original_groups = set(cfg['groups'].keys())
        config_store.save_group_config(cfg)
        reloaded = config_store.load_group_config()
        self.assertEqual(original_groups, set(reloaded['groups'].keys()))

    # ── load_overrides ───────────────────────────────────────────────────────

    def test_05_load_overrides_returns_dict(self):
        self.assertIsInstance(config_store.load_overrides(), dict)

    def test_06_load_overrides_missing_file_returns_empty(self):
        os.remove(os.path.join(self.temp_dir, 'variable_overrides.json'))
        self.assertEqual(config_store.load_overrides(), {})

    # ── save_overrides ───────────────────────────────────────────────────────

    def test_07_save_overrides_roundtrip(self):
        data = {'extrusoraErro': {'enabled': False}}
        config_store.save_overrides(data)
        loaded = config_store.load_overrides()
        self.assertEqual(loaded, data)

    # ── update_channel_history_size ──────────────────────────────────────────

    def test_08_update_history_changes_all_groups_for_channel(self):
        channel = 'plc_alarmes'
        new_size = 42
        config_store.update_channel_history_size(channel, new_size)
        cfg = config_store.load_group_config()
        for name, grp in cfg['groups'].items():
            if grp.get('channel') == channel:
                self.assertEqual(
                    grp['history_size'], new_size,
                    f"Grupo '{name}': history_size não atualizado"
                )

    def test_09_update_history_does_not_affect_other_channels(self):
        cfg_before = config_store.load_group_config()
        other_channel = 'plc_config'
        sizes_before = {
            name: grp['history_size']
            for name, grp in cfg_before['groups'].items()
            if grp.get('channel') == other_channel
        }

        config_store.update_channel_history_size('plc_alarmes', 99)

        cfg_after = config_store.load_group_config()
        for name, original_size in sizes_before.items():
            self.assertEqual(
                cfg_after['groups'][name]['history_size'],
                original_size,
                f"Grupo '{name}' (canal '{other_channel}') foi alterado indevidamente"
            )

    def test_10_update_history_persists_to_file(self):
        config_store.update_channel_history_size('plc_process', 77)
        reloaded = config_store.load_group_config()
        changed = [
            name for name, grp in reloaded['groups'].items()
            if grp.get('channel') == 'plc_process'
        ]
        self.assertTrue(changed, "Nenhum grupo mapeado para 'plc_process'")
        for name in changed:
            self.assertEqual(reloaded['groups'][name]['history_size'], 77)

    # ── get_channel_history_sizes ────────────────────────────────────────────

    def test_11_get_channel_history_sizes_unique_channels(self):
        sizes = config_store.get_channel_history_sizes()
        self.assertIsInstance(sizes, dict)
        # Canais esperados no projeto
        for ch in ('plc_alarmes', 'plc_process', 'plc_visual', 'plc_config'):
            self.assertIn(ch, sizes, f"Canal '{ch}' ausente no retorno")

    def test_12_get_channel_history_sizes_values_are_positive_int(self):
        for ch, size in config_store.get_channel_history_sizes().items():
            with self.subTest(channel=ch):
                self.assertIsInstance(size, int)
                self.assertGreater(size, 0)

    def test_13_each_channel_appears_once(self):
        sizes = config_store.get_channel_history_sizes()
        # Cada canal deve aparecer exatamente uma vez (sem duplicatas de key no dict)
        self.assertEqual(len(sizes), len(set(sizes.keys())))

    # ── patch_variable_override ──────────────────────────────────────────────

    def test_14_patch_variable_creates_override(self):
        config_store.patch_variable_override('extrusoraErro', {'enabled': False})
        overrides = config_store.load_overrides()
        self.assertIn('extrusoraErro', overrides)
        self.assertFalse(overrides['extrusoraErro']['enabled'])

    def test_15_patch_variable_merges_fields(self):
        config_store.patch_variable_override('testTag', {'enabled': True})
        config_store.patch_variable_override('testTag', {'delay_ms': 500})
        overrides = config_store.load_overrides()
        self.assertTrue(overrides['testTag']['enabled'])
        self.assertEqual(overrides['testTag']['delay_ms'], 500)


# ---------------------------------------------------------------------------
# Suite 2 — TestHubIntegration (requer Redis + Hub rodando)
# ---------------------------------------------------------------------------

_hub_proc   = None
_redis_conn = None


def _redis_available():
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
        r.ping()
        r.close()
        return True
    except Exception:
        return False


def _hub_ready(timeout=10):
    """Aguarda o Hub responder em /health."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{HUB_PORT}/health', timeout=1
            ) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def setUpModule():
    global _hub_proc, _redis_conn

    if not _redis_available():
        return  # testes de integração serão ignorados via skipTest nas classes

    os.makedirs(LOG_DIR, exist_ok=True)

    _hub_proc = subprocess.Popen(
        [
            PYTHON, '-m', 'uvicorn', 'Hub.main:asgi_app',
            '--host', '127.0.0.1',
            '--port', str(HUB_PORT),
        ],
        cwd=GATEWAY_DIR,
        stdout=open(os.path.join(LOG_DIR, 'hub_proc.log'), 'w'),
        stderr=subprocess.STDOUT,
    )

    if not _hub_ready(timeout=12):
        _hub_proc.terminate()
        _hub_proc = None
        return

    _redis_conn = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)
    logger.info("Hub iniciado na porta %d.", HUB_PORT)


def tearDownModule():
    global _hub_proc, _redis_conn
    if _redis_conn:
        _redis_conn.close()
    if _hub_proc:
        _hub_proc.terminate()
        try:
            _hub_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _hub_proc.kill()
        logger.info("Hub encerrado.")


class TestHubHTTP(unittest.TestCase):
    """Testa os endpoints REST do Hub via HTTP."""

    @classmethod
    def setUpClass(cls):
        if not _redis_available() or _hub_proc is None:
            raise unittest.SkipTest('Hub não disponível (Redis ou processo não iniciado).')
        import urllib.request
        cls.base = f'http://127.0.0.1:{HUB_PORT}'
        cls.urlopen = urllib.request.urlopen

    def _get(self, path):
        import urllib.request
        with urllib.request.urlopen(f'{self.base}{path}', timeout=3) as r:
            return json.loads(r.read().decode())

    def _patch(self, path, payload):
        import urllib.request
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            f'{self.base}{path}',
            data=data,
            method='PATCH',
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())

    def test_30_health_returns_ok(self):
        resp = self._get('/health')
        self.assertEqual(resp.get('status'), 'ok')

    def test_31_api_channels_returns_dict(self):
        resp = self._get('/api/channels')
        self.assertIsInstance(resp, dict)
        for ch in ('plc_alarmes', 'plc_process', 'plc_visual', 'plc_config'):
            self.assertIn(ch, resp, f"Canal '{ch}' ausente em /api/channels")

    def test_32_api_channels_values_are_positive_int(self):
        for ch, size in self._get('/api/channels').items():
            with self.subTest(channel=ch):
                self.assertIsInstance(size, int)
                self.assertGreater(size, 0)

    def test_33_api_groups_returns_dict(self):
        resp = self._get('/api/groups')
        self.assertIsInstance(resp, dict)
        self.assertGreater(len(resp), 0)

    def test_34_api_variables_has_overrides_key(self):
        resp = self._get('/api/variables')
        self.assertIn('overrides', resp)

    def test_35_patch_history_updates_channel(self):
        resp = self._patch('/api/channels/plc_alarmes/history', {'history_size': 55})
        self.assertEqual(resp['channel'],      'plc_alarmes')
        self.assertEqual(resp['history_size'], 55)
        # Verifica que foi persistido
        channels = self._get('/api/channels')
        self.assertEqual(channels.get('plc_alarmes'), 55)

    def test_36_patch_history_invalid_size_returns_422(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._patch('/api/channels/plc_alarmes/history', {'history_size': 0})
        self.assertEqual(ctx.exception.code, 422)


class TestHubSocketIO(unittest.TestCase):
    """Testa os eventos Socket.IO do Hub com cliente real."""

    @classmethod
    def setUpClass(cls):
        if not _redis_available() or _hub_proc is None:
            raise unittest.SkipTest('Hub não disponível (Redis ou processo não iniciado).')
        try:
            import socketio as sio_lib
            cls.sio_lib = sio_lib
        except ImportError:
            raise unittest.SkipTest('python-socketio não instalado.')

    def _make_client(self):
        sio = self.sio_lib.Client(logger=False, engineio_logger=False)
        sio.connect(f'http://127.0.0.1:{HUB_PORT}')
        return sio

    def test_40_connect_receives_connection_ack(self):
        """Ao conectar, cliente deve receber connection_ack com available_rooms."""
        received = []

        sio = self.sio_lib.Client(logger=False, engineio_logger=False)

        @sio.on('connection_ack')
        def on_ack(data):
            received.append(data)

        sio.connect(f'http://127.0.0.1:{HUB_PORT}')
        time.sleep(0.5)
        sio.disconnect()

        self.assertEqual(len(received), 1, "connection_ack não recebido")
        self.assertEqual(received[0]['status'], 'connected')
        self.assertIn('available_rooms', received[0])

    def test_41_join_event_accepted(self):
        """Evento join deve ser aceito sem erro."""
        sio = self._make_client()
        try:
            sio.emit('join', {'rooms': ['alarmes', 'process']})
            time.sleep(0.3)
        finally:
            sio.disconnect()

    def test_42_plc_write_publishes_to_redis(self):
        """plc_write deve resultar em publicação no canal Redis plc_commands."""
        pubsub = _redis_conn.pubsub()
        pubsub.subscribe('plc_commands')
        time.sleep(0.1)

        sio = self._make_client()
        payload = {'Extrusora': {'extrusoraRefVelocidade': 1234}}
        sio.emit('plc_write', payload)
        time.sleep(0.5)
        sio.disconnect()

        msg = None
        for _ in range(10):
            msg = pubsub.get_message(ignore_subscribe_messages=True)
            if msg:
                break
            time.sleep(0.1)
        pubsub.unsubscribe('plc_commands')
        pubsub.close()

        self.assertIsNotNone(msg, "Nenhuma mensagem recebida em plc_commands")
        data = json.loads(msg['data'])
        self.assertEqual(data, payload)

    def test_43_config_get_returns_group_config(self):
        """Evento config_get deve retornar group_config via config:updated."""
        received = []

        sio = self.sio_lib.Client(logger=False, engineio_logger=False)

        @sio.on('config:updated')
        def on_config(data):
            received.append(data)

        sio.connect(f'http://127.0.0.1:{HUB_PORT}')
        time.sleep(0.3)
        sio.emit('config_get', {})
        time.sleep(0.5)
        sio.disconnect()

        self.assertTrue(received, "config:updated não recebido após config_get")
        cfg = received[-1]
        self.assertIn('groups', cfg)
        self.assertIn('_meta',  cfg)

    def test_44_history_get_returns_sizes(self):
        """Evento history_get deve retornar history:sizes com os canais."""
        received = []

        sio = self.sio_lib.Client(logger=False, engineio_logger=False)

        @sio.on('history:sizes')
        def on_sizes(data):
            received.append(data)

        sio.connect(f'http://127.0.0.1:{HUB_PORT}')
        time.sleep(0.3)
        sio.emit('history_get', {})
        time.sleep(0.5)
        sio.disconnect()

        self.assertTrue(received, "history:sizes não recebido após history_get")
        sizes = received[-1]
        self.assertIsInstance(sizes, dict)
        for ch in ('plc_alarmes', 'plc_process', 'plc_visual', 'plc_config'):
            self.assertIn(ch, sizes)

    def test_45_rooms_isolation(self):
        """
        Cliente no room 'alarmes' não deve receber mensagens do room 'process'.
        Publica em plc_process e verifica que cliente em 'alarmes' não recebe.
        """
        received_process = []

        sio = self.sio_lib.Client(logger=False, engineio_logger=False)

        @sio.on('plc:data')
        def on_data(data):
            received_process.append(data)

        sio.connect(f'http://127.0.0.1:{HUB_PORT}')
        sio.emit('join', {'rooms': ['alarmes']})   # entra só em 'alarmes'
        time.sleep(0.3)

        # Publica diretamente em plc_process — NÃO deve chegar ao cliente
        _redis_conn.publish('plc_process', json.dumps({
            'coils': {}, 'registers': {'Extrusora': {'extrusoraRefVelocidade': 1}},
            'timestamp': '2026-01-01T00:00:00',
        }))
        time.sleep(0.5)
        sio.disconnect()

        self.assertEqual(
            len(received_process), 0,
            f"Cliente no room 'alarmes' recebeu mensagem de 'process': {received_process}"
        )

    def test_46_bridge_delivers_plc_data_to_correct_room(self):
        """
        Mensagem publicada em plc_alarmes deve chegar ao cliente no room 'alarmes'.
        """
        received = []

        sio = self.sio_lib.Client(logger=False, engineio_logger=False)

        @sio.on('plc:data')
        def on_data(data):
            received.append(data)

        sio.connect(f'http://127.0.0.1:{HUB_PORT}')
        sio.emit('join', {'rooms': ['alarmes']})
        time.sleep(0.8)   # aguarda o join ser processado no servidor

        test_payload = {
            'coils': {'alarmes': {'emergencia': False}},
            'registers': {},
            'timestamp': '2026-01-01T00:00:00',
        }
        # Publica múltiplas vezes para compensar possível latência de polling
        for _ in range(3):
            _redis_conn.publish('plc_alarmes', json.dumps(test_payload))
            time.sleep(0.5)

        # Aguarda entrega assíncrona bridge→Socket.IO antes de desconectar
        deadline = time.time() + 3.0
        while not received and time.time() < deadline:
            time.sleep(0.1)

        sio.disconnect()

        self.assertTrue(received, "Nenhuma mensagem plc:data recebida em room 'alarmes'")
        self.assertEqual(received[0]['channel'], 'plc_alarmes')

    def test_47_config_save_broadcasts_to_all_clients(self):
        """
        config_save deve emitir config:updated para TODOS os clientes conectados.
        """
        client_a_received = []
        client_b_received = []

        sio_a = self.sio_lib.Client(logger=False, engineio_logger=False)
        sio_b = self.sio_lib.Client(logger=False, engineio_logger=False)

        @sio_a.on('config:updated')
        def on_a(data):
            client_a_received.append(data)

        @sio_b.on('config:updated')
        def on_b(data):
            client_b_received.append(data)

        sio_a.connect(f'http://127.0.0.1:{HUB_PORT}')
        sio_b.connect(f'http://127.0.0.1:{HUB_PORT}')
        time.sleep(0.3)

        cfg = config_store.load_group_config()
        sio_a.emit('config_save', cfg)
        time.sleep(0.5)

        sio_a.disconnect()
        sio_b.disconnect()

        self.assertTrue(client_a_received, "Cliente A não recebeu config:updated")
        self.assertTrue(client_b_received, "Cliente B não recebeu config:updated broadcast")


# ---------------------------------------------------------------------------
# Suite 4 — TestConfigStorePhase3 (unit, sem deps externas)
# ---------------------------------------------------------------------------

class TestConfigStorePhase3(unittest.TestCase):
    """
    Testa as funções de Fase 3 do config_store:
    load_all_variables, generate_export_xlsx, parse_upload_xlsx, apply_upload_config.
    Usa diretório temporário copiando JSON + CSV para isolar os arquivos reais.
    """

    def setUp(self):
        import openpyxl
        import io as _io
        self._openpyxl = openpyxl
        self._io = _io

        self.temp_dir = tempfile.mkdtemp()
        for fname in ('group_config.json', 'variable_overrides.json'):
            shutil.copy(
                os.path.join(TABLES_DIR, fname),
                os.path.join(self.temp_dir, fname),
            )
        for fname in ('operacao.csv', 'configuracao.csv'):
            src = os.path.join(TABLES_DIR, fname)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(self.temp_dir, fname))

        self._orig_tables_dir = config_store._TABLES_DIR
        config_store._TABLES_DIR = self.temp_dir
        # Reseta cache de grupos do operacao para forçar re-leitura
        config_store._OPERACAO_GROUPS = set()

    def tearDown(self):
        config_store._TABLES_DIR = self._orig_tables_dir
        config_store._OPERACAO_GROUPS = set()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ── load_all_variables ───────────────────────────────────────────────────

    def test_50_load_all_variables_returns_list(self):
        result = config_store.load_all_variables()
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0, "load_all_variables deve retornar pelo menos uma variável")

    def test_51_each_variable_has_required_fields(self):
        result = config_store.load_all_variables()
        required = {'tag', 'group', 'type', 'address', 'channel', 'delay_ms',
                    'history_size', 'enabled', 'has_override', 'source'}
        for var in result:
            with self.subTest(tag=var.get('tag')):
                self.assertTrue(required.issubset(var.keys()), f"Campos ausentes em {var}")

    def test_52_channel_from_group_config(self):
        """Variáveis do grupo 'Extrusora' devem herdar channel de group_config.json."""
        result  = config_store.load_all_variables()
        cfg     = config_store.load_group_config()
        ext_ch  = cfg['groups']['Extrusora']['channel']
        ext_vars = [v for v in result if v['group'] == 'Extrusora']
        self.assertTrue(ext_vars, "Nenhuma variável do grupo Extrusora encontrada")
        for v in ext_vars:
            with self.subTest(tag=v['tag']):
                self.assertEqual(v['channel'], ext_ch)

    def test_53_override_applied_to_tag(self):
        """Um override deve sobrescrever o canal da variável."""
        result  = config_store.load_all_variables()
        any_tag = result[0]['tag']

        config_store.patch_variable_override(any_tag, {'channel': 'plc_visual', 'enabled': False})
        config_store._OPERACAO_GROUPS = set()   # force re-read
        updated = config_store.load_all_variables()
        target  = next((v for v in updated if v['tag'] == any_tag), None)

        self.assertIsNotNone(target)
        self.assertEqual(target['channel'],  'plc_visual')
        self.assertFalse(target['enabled'])
        self.assertTrue(target['has_override'])

    def test_54_configuracao_groups_use_cfg_suffix_when_collision(self):
        """Grupos de configuracao.csv que colidem com operacao.csv usam suffix '_cfg'."""
        result = config_store.load_all_variables()
        cfg_keys = {v['group_cfg_key'] for v in result if v['source'] == 'configuracao'}
        # 'alarmes' existe em ambos CSVs → deve aparecer como 'alarmes_cfg' para configuracao
        operacao_groups = {v['group'] for v in result if v['source'] == 'operacao'}
        for cfg_key in cfg_keys:
            base = cfg_key.removesuffix('_cfg')
            if base in operacao_groups:
                self.assertTrue(cfg_key.endswith('_cfg'),
                                f"Grupo '{cfg_key}' do configuracao.csv deveria ter sufixo '_cfg'")

    # ── generate_export_xlsx ─────────────────────────────────────────────────

    def test_55_generate_export_xlsx_returns_bytes(self):
        data = config_store.generate_export_xlsx()
        self.assertIsInstance(data, bytes)
        self.assertGreater(len(data), 100)

    def test_56_export_xlsx_is_valid_workbook(self):
        data = config_store.generate_export_xlsx()
        wb   = self._openpyxl.load_workbook(self._io.BytesIO(data))
        ws   = wb.active
        headers = [cell.value for cell in ws[1]]
        self.assertIn('Tag',   headers)
        self.assertIn('Canal', headers)
        self.assertGreater(ws.max_row, 1, "xlsx deve ter pelo menos 1 linha de dados")

    # ── parse_upload_xlsx ────────────────────────────────────────────────────

    def test_57_parse_upload_xlsx_roundtrip(self):
        """Export → parse deve recuperar os campos editáveis."""
        data    = config_store.generate_export_xlsx()
        preview = config_store.parse_upload_xlsx(data)
        self.assertIsInstance(preview, list)
        self.assertGreater(len(preview), 0)
        first = preview[0]
        self.assertIn('tag', first)
        self.assertIn('channel', first)

    def test_58_parse_upload_xlsx_missing_tag_column_raises(self):
        wb = self._openpyxl.Workbook()
        ws = wb.active
        ws.append(['Sem coluna tag', 'Canal'])  # wrong header
        ws.append(['val1', 'plc_alarmes'])
        buf = self._io.BytesIO()
        wb.save(buf)
        with self.assertRaises(ValueError):
            config_store.parse_upload_xlsx(buf.getvalue())

    # ── apply_upload_config ──────────────────────────────────────────────────

    def test_59_apply_upload_sets_group_config(self):
        """Se todos os rows de um grupo têm o mesmo canal novo, group_config deve ser atualizado."""
        config_store._OPERACAO_GROUPS = set()
        variables = config_store.load_all_variables()
        ext_vars  = [v for v in variables if v['group'] == 'Extrusora']
        self.assertTrue(ext_vars)

        rows = [
            {'tag': v['tag'], 'group': v['group'], 'source': v['source'],
             'channel': 'plc_visual', 'delay_ms': 999, 'history_size': 50, 'enabled': True}
            for v in ext_vars
        ]
        config_store.apply_upload_config(rows)

        updated = config_store.load_group_config()
        self.assertEqual(updated['groups']['Extrusora']['channel'],  'plc_visual')
        self.assertEqual(updated['groups']['Extrusora']['delay_ms'], 999)

    def test_60_apply_upload_sets_per_tag_override(self):
        """Row com canal diferente do grupo cria override individual."""
        config_store._OPERACAO_GROUPS = set()
        variables = config_store.load_all_variables()
        ext_vars  = [v for v in variables if v['group'] == 'Extrusora']
        self.assertTrue(len(ext_vars) >= 2)

        # Mantém defaults para todos exceto o primeiro
        group_ch = ext_vars[0]['channel']
        rows = []
        for i, v in enumerate(ext_vars):
            ch = 'plc_alarmes' if i == 0 else group_ch   # só o primeiro é diferente
            rows.append({'tag': v['tag'], 'group': v['group'], 'source': v['source'],
                         'channel': ch, 'delay_ms': v['delay_ms'],
                         'history_size': v['history_size'], 'enabled': True})
        config_store.apply_upload_config(rows)

        overrides = config_store.load_overrides()
        self.assertIn(ext_vars[0]['tag'], overrides)
        self.assertEqual(overrides[ext_vars[0]['tag']]['channel'], 'plc_alarmes')


# ---------------------------------------------------------------------------
# Suite 5 — TestHubHTTPPhase3 (requer Hub subprocess + Redis)
# ---------------------------------------------------------------------------

class TestHubHTTPPhase3(unittest.TestCase):
    """Testa os endpoints REST de Fase 3 do Hub."""

    @classmethod
    def setUpClass(cls):
        try:
            r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_connect_timeout=2)
            r.ping()
            r.close()
        except Exception:
            raise unittest.SkipTest("Redis não disponível")

        if _hub_proc is None:
            raise unittest.SkipTest("Hub não iniciado")

    def _get(self, path, **kwargs):
        import urllib.request
        req = urllib.request.urlopen(f'http://127.0.0.1:{HUB_PORT}{path}', **kwargs)
        return req

    def _post_json(self, path, data):
        import urllib.request
        body = json.dumps(data).encode()
        req  = urllib.request.Request(
            f'http://127.0.0.1:{HUB_PORT}{path}',
            data=body,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        return urllib.request.urlopen(req)

    def _patch_json(self, path, data):
        import urllib.request
        body = json.dumps(data).encode()
        req  = urllib.request.Request(
            f'http://127.0.0.1:{HUB_PORT}{path}',
            data=body,
            headers={'Content-Type': 'application/json'},
            method='PATCH',
        )
        return urllib.request.urlopen(req)

    def test_70_get_index_returns_html(self):
        res = self._get('/')
        self.assertEqual(res.status, 200)
        ct = res.headers.get('Content-Type', '')
        self.assertIn('text/html', ct)

    def test_71_get_variables_has_variables_key(self):
        res  = self._get('/api/variables')
        data = json.loads(res.read())
        self.assertIn('variables', data)
        self.assertIn('overrides', data)

    def test_72_get_variables_list_is_non_empty(self):
        res  = self._get('/api/variables')
        data = json.loads(res.read())
        self.assertIsInstance(data['variables'], list)
        self.assertGreater(len(data['variables']), 0)

    def test_73_each_variable_has_tag_and_channel(self):
        res  = self._get('/api/variables')
        data = json.loads(res.read())
        for var in data['variables']:
            with self.subTest(tag=var.get('tag')):
                self.assertIn('tag',     var)
                self.assertIn('channel', var)

    def test_74_patch_variable_creates_override(self):
        import urllib.error
        # Pega um tag qualquer
        res  = self._get('/api/variables')
        data = json.loads(res.read())
        tag  = data['variables'][0]['tag']

        res2 = self._patch_json(f'/api/variables/{tag}', {'enabled': False})
        self.assertEqual(res2.status, 200)

        res3  = self._get('/api/variables')
        data3 = json.loads(res3.read())
        ov    = data3['overrides']
        self.assertIn(tag, ov)
        self.assertFalse(ov[tag].get('enabled', True))

    def test_75_get_export_returns_xlsx(self):
        res = self._get('/api/export')
        self.assertEqual(res.status, 200)
        ct  = res.headers.get('Content-Type', '')
        self.assertIn('spreadsheetml', ct)
        body = res.read()
        self.assertGreater(len(body), 100)

    def test_76_export_is_valid_xlsx(self):
        import io as _io
        import openpyxl
        res  = self._get('/api/export')
        wb   = openpyxl.load_workbook(_io.BytesIO(res.read()))
        ws   = wb.active
        hdrs = [c.value for c in ws[1]]
        self.assertIn('Tag',   hdrs)
        self.assertIn('Canal', hdrs)

    def test_77_post_upload_with_export_data_returns_preview(self):
        """Exporta, faz upload do mesmo arquivo e verifica preview."""
        import email.generator
        import io as _io
        import openpyxl
        import urllib.request

        # Obtém o xlsx exportado
        res      = self._get('/api/export')
        xlsx_raw = res.read()

        # Monta multipart/form-data manualmente
        boundary = b'----TestBoundary12345'
        body = (
            b'--' + boundary + b'\r\n'
            b'Content-Disposition: form-data; name="file"; filename="test.xlsx"\r\n'
            b'Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n'
            b'\r\n' + xlsx_raw + b'\r\n'
            b'--' + boundary + b'--\r\n'
        )
        req = urllib.request.Request(
            f'http://127.0.0.1:{HUB_PORT}/api/upload',
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary.decode()}'},
            method='POST',
        )
        resp = urllib.request.urlopen(req)
        data = json.loads(resp.read())
        self.assertIn('preview', data)
        self.assertIn('count',   data)
        self.assertGreater(data['count'], 0)

    def test_78_post_upload_confirm_applies_config(self):
        """upload/confirm aceita rows e retorna contagem."""
        # Pega variables atuais
        res      = self._get('/api/variables')
        data     = json.loads(res.read())
        vars_now = data['variables'][:3]   # usa só 3 para agilizar

        rows = [
            {'tag': v['tag'], 'group': v.get('group', ''),
             'source': v.get('source', 'operacao'),
             'channel': v['channel'], 'delay_ms': v['delay_ms'],
             'history_size': v.get('history_size', 100), 'enabled': v.get('enabled', True)}
            for v in vars_now
        ]
        res2 = self._post_json('/api/upload/confirm', {'rows': rows})
        self.assertEqual(res2.status, 200)
        data2 = json.loads(res2.read())
        self.assertEqual(data2['applied'], len(rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
