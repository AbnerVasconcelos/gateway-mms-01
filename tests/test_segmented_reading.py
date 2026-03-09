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

from table_filter import extract_parameters_by_group          # noqa: E402
from shared.redis_config_functions import publish_to_channel  # noqa: E402

TABLES_DIR         = os.path.join(GATEWAY_DIR, 'tables')
MAPEAMENTO_CLP_CSV = os.path.join(TABLES_DIR, 'mapeamento_clp.csv')
GLOBAIS_CSV        = os.path.join(TABLES_DIR, 'globais.csv')
RETENTIVAS_CSV     = os.path.join(TABLES_DIR, 'retentivas.csv')
IO_FISICAS_CSV     = os.path.join(TABLES_DIR, 'io_fisicas.csv')
GROUP_CONFIG_PATH  = os.path.join(TABLES_DIR, 'group_config.json')
OVERRIDES_PATH     = os.path.join(TABLES_DIR, 'variable_overrides.json')


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
        for expected in ('plc_alarmes', 'plc_process', 'plc_visual', 'plc_config'):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
