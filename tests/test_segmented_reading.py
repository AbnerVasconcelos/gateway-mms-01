#!/usr/bin/env python3
"""
Testes da Fase 1 — Leitura segmentada por canal com frequência adaptativa.

Cobre:
  - extract_parameters_by_group: estrutura, grupos presentes, contiguidade, tamanhos
  - publish_to_channel: parâmetro history_size
  - group_config.json: carregamento, campos obrigatórios, consistência com os CSVs
  - variable_overrides.json: carregamento e aplicação de overrides

Não requer CLP físico nem Redis em execução — usa mocks onde necessário.

Uso:
    python -m pytest tests/test_segmented_reading.py -v
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GATEWAY_DIR)
sys.path.insert(0, os.path.join(GATEWAY_DIR, 'Delfos'))

from table_filter import extract_parameters_by_group, extract_parameters_by_channel  # noqa: E402
from shared.redis_config_functions import publish_to_channel  # noqa: E402
from shared.modbus_functions import read_registers_with_bits  # noqa: E402

TABLES_DIR          = os.path.join(GATEWAY_DIR, 'tables')
MAPEAMENTO_CLP_CSV  = os.path.join(TABLES_DIR, 'mapeamento_clp.csv')
GLOBAIS_CSV         = os.path.join(TABLES_DIR, 'globais.csv')
RETENTIVAS_CSV      = os.path.join(TABLES_DIR, 'retentivas.csv')
IO_FISICAS_CSV      = os.path.join(TABLES_DIR, 'io_fisicas.csv')
TEMP_24Z_CSV        = os.path.join(TABLES_DIR, 'temperatura_24z.csv')
GROUP_CONFIG_PATH   = os.path.join(TABLES_DIR, 'group_config.json')
OVERRIDES_PATH      = os.path.join(TABLES_DIR, 'variable_overrides.json')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redis_mock():
    r = MagicMock()
    r.publish = MagicMock()
    r.set     = MagicMock()
    r.lpush   = MagicMock()
    r.ltrim   = MagicMock()
    return r


# ---------------------------------------------------------------------------
# 1. extract_parameters_by_group
# ---------------------------------------------------------------------------

class TestExtractByGroup(unittest.TestCase):
    """Valida a função extract_parameters_by_group para os CSVs ativos."""

    @classmethod
    def setUpClass(cls):
        cls.mapeamento = extract_parameters_by_group(MAPEAMENTO_CLP_CSV)
        cls.globais    = extract_parameters_by_group(GLOBAIS_CSV)
        cls.retentivas = extract_parameters_by_group(RETENTIVAS_CSV)

    # ── Tipo de retorno ──────────────────────────────────────────────────────

    def test_01_returns_dict(self):
        self.assertIsInstance(self.mapeamento, dict)
        self.assertIsInstance(self.globais,    dict)
        self.assertIsInstance(self.retentivas, dict)

    def test_02_not_empty(self):
        self.assertGreater(len(self.mapeamento), 0, "mapeamento_clp.csv não gerou grupos")
        self.assertGreater(len(self.globais),    0, "globais.csv não gerou grupos")

    # ── Grupos esperados ─────────────────────────────────────────────────────

    def test_03_mapeamento_expected_groups(self):
        expected = {
            'alarmes', 'controle_extrusora', 'producao',
            'corte', 'ajuste', 'comandos',
        }
        missing = expected - set(self.mapeamento.keys())
        self.assertFalse(missing, f"Grupos ausentes em mapeamento_clp.csv: {missing}")

    def test_04_globais_expected_groups(self):
        expected = {
            'alarmes', 'controle_extrusora', 'producao',
            'setpoint', 'velocidade', 'corrente',
        }
        missing = expected - set(self.globais.keys())
        self.assertFalse(missing, f"Grupos ausentes em globais.csv: {missing}")

    # ── Estrutura de cada grupo ──────────────────────────────────────────────

    def test_05_group_structural_keys(self):
        required = {'coil_groups', 'reg_groups', 'coil_tags', 'reg_tags', 'coil_keys', 'reg_keys'}
        for name, data in {**self.mapeamento, **self.globais}.items():
            with self.subTest(group=name):
                self.assertEqual(required, set(data.keys()),
                                 f"Grupo '{name}' com chaves inesperadas")

    # ── Contiguidade interna ─────────────────────────────────────────────────

    def test_06_coil_groups_are_contiguous(self):
        """Endereços dentro de cada sub-grupo de coils devem ser consecutivos."""
        for name, data in self.mapeamento.items():
            for i, sub in enumerate(data['coil_groups']):
                with self.subTest(group=name, sub=i):
                    for j in range(1, len(sub)):
                        self.assertEqual(sub[j], sub[j - 1] + 1,
                                         f"'{name}' coil_group[{i}] não contíguo: {sub}")

    def test_07_reg_groups_are_contiguous(self):
        """Endereços dentro de cada sub-grupo de registers devem ser consecutivos."""
        for name, data in self.mapeamento.items():
            for i, sub in enumerate(data['reg_groups']):
                with self.subTest(group=name, sub=i):
                    for j in range(1, len(sub)):
                        self.assertEqual(sub[j], sub[j - 1] + 1,
                                         f"'{name}' reg_group[{i}] não contíguo: {sub}")

    # ── Tamanhos consistentes ────────────────────────────────────────────────

    def test_08_coil_tags_length_matches_group(self):
        """len(coil_tags[i]) == len(coil_groups[i]) para todo i."""
        for name, data in {**self.mapeamento, **self.globais}.items():
            for i, (sub, tags) in enumerate(zip(data['coil_groups'], data['coil_tags'])):
                with self.subTest(group=name, sub=i):
                    self.assertEqual(len(sub), len(tags))

    def test_09_reg_tags_length_matches_group(self):
        """len(reg_tags[i]) == len(reg_groups[i]) para todo i."""
        for name, data in {**self.mapeamento, **self.globais}.items():
            for i, (sub, tags) in enumerate(zip(data['reg_groups'], data['reg_tags'])):
                with self.subTest(group=name, sub=i):
                    self.assertEqual(len(sub), len(tags))

    # ── Grupos específicos ───────────────────────────────────────────────────

    def test_10_alarmes_has_coils_and_registers(self):
        alarmes = self.mapeamento['alarmes']
        self.assertGreater(len(alarmes['coil_groups']) + len(alarmes['reg_groups']), 0,
                           "alarmes deve ter coils ou registers")

    def test_11_producao_has_registers(self):
        producao = self.mapeamento['producao']
        self.assertGreater(len(producao['reg_groups']), 0,
                           "producao deve ter registers")

    def test_12_retentivas_not_empty(self):
        self.assertGreater(len(self.retentivas), 0, "retentivas.csv deve gerar grupos")

    def test_13_globais_dont_share_reg_addresses(self):
        """Endereços de register não devem se repetir entre grupos em globais.csv."""
        seen = {}
        for name, data in self.globais.items():
            for sub in data['reg_groups']:
                for addr in sub:
                    if addr in seen:
                        self.fail(
                            f"Endereço register {addr} aparece em '{name}' e '{seen[addr]}'"
                        )
                    seen[addr] = name


# ---------------------------------------------------------------------------
# 2. publish_to_channel — parâmetro history_size
# ---------------------------------------------------------------------------

class TestPublishHistorySize(unittest.TestCase):
    """Verifica que publish_to_channel respeita history_size no ltrim."""

    def test_20_default_history_100(self):
        """Sem history_size explícito → ltrim(0, 99) = 100 entradas."""
        r = _redis_mock()
        publish_to_channel(r, '{}', 'plc_test')
        r.ltrim.assert_called_once_with('history:plc_test', 0, 99)

    def test_21_history_50(self):
        r = _redis_mock()
        publish_to_channel(r, '{}', 'plc_test', history_size=50)
        r.ltrim.assert_called_once_with('history:plc_test', 0, 49)

    def test_22_history_200(self):
        r = _redis_mock()
        publish_to_channel(r, '{}', 'plc_test', history_size=200)
        r.ltrim.assert_called_once_with('history:plc_test', 0, 199)

    def test_23_history_1(self):
        """history_size=1 → ltrim(0, 0) = apenas a última entrada."""
        r = _redis_mock()
        publish_to_channel(r, '{}', 'plc_test', history_size=1)
        r.ltrim.assert_called_once_with('history:plc_test', 0, 0)

    def test_24_correct_channel_used(self):
        r = _redis_mock()
        publish_to_channel(r, '{"x": 1}', 'plc_alarmes', history_size=100)
        r.publish.assert_called_once_with('plc_alarmes', '{"x": 1}')
        r.set.assert_called_once_with('last_message:plc_alarmes', '{"x": 1}')
        r.lpush.assert_called_once_with('history:plc_alarmes', '{"x": 1}')

    def test_25_different_channels_independent(self):
        """history_size de um canal não afeta outro."""
        r = _redis_mock()
        publish_to_channel(r, '{}', 'plc_process', history_size=50)
        publish_to_channel(r, '{}', 'plc_config',  history_size=200)
        calls = r.ltrim.call_args_list
        self.assertEqual(calls[0], (('history:plc_process', 0, 49),))
        self.assertEqual(calls[1], (('history:plc_config',  0, 199),))


# ---------------------------------------------------------------------------
# 3. group_config.json — estrutura e consistência
# ---------------------------------------------------------------------------

class TestGroupConfig(unittest.TestCase):
    """Valida o conteúdo de tables/group_config.json."""

    @classmethod
    def setUpClass(cls):
        with open(GROUP_CONFIG_PATH, 'r', encoding='utf-8') as f:
            cls.config = json.load(f)
        cls.mapeamento_groups = set(extract_parameters_by_group(MAPEAMENTO_CLP_CSV).keys())

    def test_30_has_meta_section(self):
        self.assertIn('_meta', self.config)

    def test_31_no_groups_section(self):
        """Fase 5: seção 'groups' removida de group_config.json."""
        self.assertNotIn('groups', self.config)

    def test_32_meta_fields(self):
        meta = self.config['_meta']
        self.assertIn('default_delay_ms',    meta)
        self.assertIn('default_history_size', meta)

    def test_33_channels_have_required_fields(self):
        """Todos os canais em devices[*].channels devem ter delay_ms e history_size."""
        all_channels = {}
        for dev_id, dev_cfg in self.config.get('devices', {}).items():
            for ch, ch_cfg in dev_cfg.get('channels', {}).items():
                all_channels[ch] = ch_cfg
        self.assertGreater(len(all_channels), 0, "Nenhum canal encontrado em devices[*].channels")
        for ch, ch_cfg in all_channels.items():
            with self.subTest(channel=ch):
                self.assertIn('delay_ms',     ch_cfg)
                self.assertIn('history_size', ch_cfg)

    def test_34_channels_section_is_sole_routing_config(self):
        """Channels are now per-device; 'groups' section absent."""
        self.assertNotIn('groups', self.config)
        # Channels are inside each device, not at top level
        for dev_id, dev_cfg in self.config.get('devices', {}).items():
            with self.subTest(device=dev_id):
                self.assertIn('channels', dev_cfg)
                self.assertIsInstance(dev_cfg['channels'], dict)

    def test_35_delay_ms_positive(self):
        """delay_ms está em devices[*].channels (não mais nos grupos)."""
        all_channels = {}
        for dev_id, dev_cfg in self.config.get('devices', {}).items():
            for ch, ch_cfg in dev_cfg.get('channels', {}).items():
                all_channels[ch] = ch_cfg
        self.assertGreater(len(all_channels), 0, "Nenhum canal encontrado em devices[*].channels")
        for ch, cfg in all_channels.items():
            with self.subTest(channel=ch):
                self.assertGreater(cfg.get('delay_ms', 0), 0)

    def test_36_history_size_positive(self):
        """history_size está em devices[*].channels (não mais nos grupos)."""
        all_channels = {}
        for dev_id, dev_cfg in self.config.get('devices', {}).items():
            for ch, ch_cfg in dev_cfg.get('channels', {}).items():
                all_channels[ch] = ch_cfg
        self.assertGreater(len(all_channels), 0, "Nenhum canal encontrado em devices[*].channels")
        for ch, cfg in all_channels.items():
            with self.subTest(channel=ch):
                self.assertGreater(cfg.get('history_size', 0), 0)

    def test_37_channels_use_plc_prefix(self):
        """Todos os canais em devices[*].channels devem ter prefixo 'plc_'."""
        for dev_id, dev_cfg in self.config.get('devices', {}).items():
            for ch in dev_cfg.get('channels', {}):
                with self.subTest(device=dev_id, channel=ch):
                    self.assertTrue(ch.startswith('plc_'),
                                    f"Canal '{ch}' sem prefixo 'plc_'")

    def test_38_known_channels_present(self):
        """Canais obrigatórios devem estar em pelo menos um device."""
        all_channels = set()
        for dev_id, dev_cfg in self.config.get('devices', {}).items():
            all_channels.update(dev_cfg.get('channels', {}).keys())
        for expected in ('plc_alarmes', 'plc_retentivas', 'plc_operacao'):
            with self.subTest(channel=expected):
                self.assertIn(expected, all_channels)


# ---------------------------------------------------------------------------
# 4. variable_overrides.json
# ---------------------------------------------------------------------------

class TestVariableOverrides(unittest.TestCase):
    """Valida o carregamento de variable_overrides.json."""

    @classmethod
    def setUpClass(cls):
        with open(OVERRIDES_PATH, 'r', encoding='utf-8') as f:
            cls.overrides = json.load(f)

    def test_40_is_dict(self):
        self.assertIsInstance(self.overrides, dict)

    def test_41_enabled_fields_are_bool(self):
        for tag, cfg in self.overrides.items():
            if 'enabled' in cfg:
                with self.subTest(tag=tag):
                    self.assertIsInstance(cfg['enabled'], bool)


# ---------------------------------------------------------------------------
# 5. Bit addressing — extract_parameters_by_channel com CSVs de temperatura
# ---------------------------------------------------------------------------

class TestBitAddressing(unittest.TestCase):
    """Testa suporte a bit addressing em extract_parameters_by_channel."""

    @classmethod
    def setUpClass(cls):
        """Configura canal de teste com variáveis de temperatura_24z."""
        cls.temp_csv_exists = os.path.exists(TEMP_24Z_CSV)
        if not cls.temp_csv_exists:
            return

        # Overrides: atribui algumas variáveis bit-addressed a um canal
        cls.overrides = {
            'tempNaoAtingidaZona1':  {'channel': 'plc_temp_test'},
            'tempNaoAtingidaZona2':  {'channel': 'plc_temp_test'},
            'tempNaoAtingidaZona3':  {'channel': 'plc_temp_test'},
            'tempNaoAtingidaZona11': {'channel': 'plc_temp_test'},
            'tempZona1':            {'channel': 'plc_temp_test'},
            'tempZona2':            {'channel': 'plc_temp_test'},
        }
        cls.group_config = {
            '_meta': {'default_delay_ms': 1000, 'default_history_size': 100},
            'channels': {
                'plc_temp_test': {'delay_ms': 500, 'history_size': 50},
            },
        }
        cls.channel_data = extract_parameters_by_channel(
            [TEMP_24Z_CSV], cls.group_config, cls.overrides,
        )

    def test_50_channel_has_bit_vars(self):
        """Canal com variáveis de temperatura deve ter bit_vars."""
        if not self.temp_csv_exists:
            self.skipTest("temperatura_24z.csv não encontrado")
        ch = self.channel_data.get('plc_temp_test')
        self.assertIsNotNone(ch, "Canal plc_temp_test não encontrado")
        self.assertIsNotNone(ch.get('bit_vars'), "bit_vars deve estar presente")

    def test_51_bit_vars_structure(self):
        """bit_vars mapeia register_addr → lista de {tag, key, bit}."""
        if not self.temp_csv_exists:
            self.skipTest("temperatura_24z.csv não encontrado")
        ch = self.channel_data['plc_temp_test']
        bv = ch['bit_vars']

        # Register 1584 deve ter entradas para zonas 1 (bit 0), 2 (bit 1), 3 (bit 2), 11 (bit 10)
        self.assertIn(1584, bv, "Register 1584 deveria estar em bit_vars")
        entries = bv[1584]
        tags = {e['tag']: e['bit'] for e in entries}
        self.assertEqual(tags.get('tempNaoAtingidaZona1'), 0)
        self.assertEqual(tags.get('tempNaoAtingidaZona2'), 1)
        self.assertEqual(tags.get('tempNaoAtingidaZona3'), 2)
        self.assertEqual(tags.get('tempNaoAtingidaZona11'), 10)

    def test_52_bit_vars_not_in_coil_groups(self):
        """Variáveis bit-addressed não devem aparecer em coil_groups."""
        if not self.temp_csv_exists:
            self.skipTest("temperatura_24z.csv não encontrado")
        ch = self.channel_data['plc_temp_test']
        all_coil_addrs = [a for g in ch['coil_groups'] for a in g]
        # Register 1584 não deve estar em coils
        self.assertNotIn(1584, all_coil_addrs)

    def test_53_bit_var_register_in_reg_groups(self):
        """Registradores com bit_vars devem aparecer em reg_groups para serem lidos."""
        if not self.temp_csv_exists:
            self.skipTest("temperatura_24z.csv não encontrado")
        ch = self.channel_data['plc_temp_test']
        all_reg_addrs = [a for g in ch['reg_groups'] for a in g]
        # Register 1584 deve estar em reg_groups (para ser lido como holding register)
        self.assertIn(1584, all_reg_addrs)

    def test_54_no_duplicate_reg_addresses(self):
        """Cada endereço aparece uma única vez em reg_groups."""
        if not self.temp_csv_exists:
            self.skipTest("temperatura_24z.csv não encontrado")
        ch = self.channel_data['plc_temp_test']
        all_reg_addrs = [a for g in ch['reg_groups'] for a in g]
        self.assertEqual(len(all_reg_addrs), len(set(all_reg_addrs)),
                         f"Endereços duplicados em reg_groups: {all_reg_addrs}")

    def test_55_normal_registers_still_work(self):
        """Registradores normais (tempZona1, tempZona2) estão em reg_groups sem bit_vars."""
        if not self.temp_csv_exists:
            self.skipTest("temperatura_24z.csv não encontrado")
        ch = self.channel_data['plc_temp_test']
        all_reg_addrs = [a for g in ch['reg_groups'] for a in g]
        # tempZona1=1536, tempZona2=1537
        self.assertIn(1536, all_reg_addrs)
        self.assertIn(1537, all_reg_addrs)
        # Esses não devem estar em bit_vars
        bv = ch.get('bit_vars') or {}
        self.assertNotIn(1536, bv)
        self.assertNotIn(1537, bv)

    def test_56_bit_vars_empty_for_normal_csv(self):
        """CSVs sem variáveis bit-addressed retornam bit_vars=None."""
        overrides = {
            'alarmes': {'channel': 'plc_test_normal'},
        }
        group_config = {
            '_meta': {'default_delay_ms': 1000, 'default_history_size': 100},
            'channels': {'plc_test_normal': {'delay_ms': 500, 'history_size': 50}},
        }
        # mapeamento_clp.csv não tem variáveis bit-addressed
        if not os.path.exists(MAPEAMENTO_CLP_CSV):
            self.skipTest("mapeamento_clp.csv não encontrado")

        # Precisa de tags que existam no CSV para ter um canal ativo
        import pandas as pd
        df = pd.read_csv(MAPEAMENTO_CLP_CSV, sep=',', dtype={'Modbus': str})
        tags = df['ObjecTag'].dropna().astype(str).str.strip().tolist()[:5]
        overrides_real = {tag: {'channel': 'plc_test_normal'} for tag in tags}

        channel_data = extract_parameters_by_channel(
            [MAPEAMENTO_CLP_CSV], group_config, overrides_real,
        )
        if 'plc_test_normal' in channel_data:
            self.assertIsNone(channel_data['plc_test_normal'].get('bit_vars'))

    def test_57_read_registers_with_bits_extraction(self):
        """read_registers_with_bits extrai bits corretamente de valores mockados."""
        # Mock client
        client = MagicMock()
        # Register 1584 com valor 0b0000_0100_0000_0011 = 0x0403 = 1027
        # bit 0 = 1, bit 1 = 1, bit 2 = 0, ..., bit 10 = 1
        client.read_holding_registers.return_value = [0x0403]

        groups = [[1584]]
        tags = [['tempNaoAtingidaZona1']]
        keys = [['alarme_temp_nao_atingida']]
        bit_vars = {
            1584: [
                {'tag': 'tempNaoAtingidaZona1',  'key': 'alarme_temp_nao_atingida', 'bit': 0},
                {'tag': 'tempNaoAtingidaZona2',  'key': 'alarme_temp_nao_atingida', 'bit': 1},
                {'tag': 'tempNaoAtingidaZona3',  'key': 'alarme_temp_nao_atingida', 'bit': 2},
                {'tag': 'tempNaoAtingidaZona11', 'key': 'alarme_temp_nao_atingida', 'bit': 10},
            ],
        }

        data, count = read_registers_with_bits(client, groups, tags, keys, bit_vars)

        alarm_data = data['alarme_temp_nao_atingida']
        self.assertTrue(alarm_data['tempNaoAtingidaZona1'])   # bit 0 = 1
        self.assertTrue(alarm_data['tempNaoAtingidaZona2'])   # bit 1 = 1
        self.assertFalse(alarm_data['tempNaoAtingidaZona3'])  # bit 2 = 0
        self.assertTrue(alarm_data['tempNaoAtingidaZona11'])  # bit 10 = 1

    def test_58_read_registers_with_bits_no_bit_vars(self):
        """read_registers_with_bits sem bit_vars comporta-se como read_registers."""
        client = MagicMock()
        client.read_holding_registers.return_value = [42]

        groups = [[1536]]
        tags = [['tempZona1']]
        keys = [['temperatura']]

        data, count = read_registers_with_bits(client, groups, tags, keys, None)
        self.assertEqual(data['temperatura']['tempZona1'], 42)


if __name__ == '__main__':
    unittest.main(verbosity=2)
