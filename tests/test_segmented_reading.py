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
OPERACAO_CSV       = os.path.join(TABLES_DIR, 'operacao.csv')
CONFIGURACAO_CSV   = os.path.join(TABLES_DIR, 'configuracao.csv')
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
    """Valida a função extract_parameters_by_group para ambos os CSVs."""

    @classmethod
    def setUpClass(cls):
        cls.operacao      = extract_parameters_by_group(OPERACAO_CSV)
        cls.configuracao  = extract_parameters_by_group(CONFIGURACAO_CSV)

    # ── Tipo de retorno ──────────────────────────────────────────────────────

    def test_01_returns_dict(self):
        self.assertIsInstance(self.operacao,     dict)
        self.assertIsInstance(self.configuracao, dict)

    def test_02_not_empty(self):
        self.assertGreater(len(self.operacao),     0, "operacao.csv não gerou grupos")
        self.assertGreater(len(self.configuracao), 0, "configuracao.csv não gerou grupos")

    # ── Grupos esperados ─────────────────────────────────────────────────────

    def test_03_operacao_expected_groups(self):
        expected = {
            'Extrusora', 'Puxador', 'producao', 'threeJs',
            'dosador', 'alimentador', 'totalizadores', 'saidasDigitais', 'alarmes',
        }
        missing = expected - set(self.operacao.keys())
        self.assertFalse(missing, f"Grupos ausentes em operacao.csv: {missing}")

    def test_04_configuracao_expected_groups(self):
        expected = {
            'balancaInferior', 'balancaSuperior',
            'configExtrusora', 'configFunis', 'configPuxador', 'configReceita',
        }
        missing = expected - set(self.configuracao.keys())
        self.assertFalse(missing, f"Grupos ausentes em configuracao.csv: {missing}")

    # ── Estrutura de cada grupo ──────────────────────────────────────────────

    def test_05_group_structural_keys(self):
        required = {'coil_groups', 'reg_groups', 'coil_tags', 'reg_tags', 'coil_keys', 'reg_keys'}
        for name, data in {**self.operacao, **self.configuracao}.items():
            with self.subTest(group=name):
                self.assertEqual(required, set(data.keys()),
                                 f"Grupo '{name}' com chaves inesperadas")

    # ── Contiguidade interna ─────────────────────────────────────────────────

    def test_06_coil_groups_are_contiguous(self):
        """Endereços dentro de cada sub-grupo de coils devem ser consecutivos."""
        for name, data in self.operacao.items():
            for i, sub in enumerate(data['coil_groups']):
                with self.subTest(group=name, sub=i):
                    for j in range(1, len(sub)):
                        self.assertEqual(sub[j], sub[j - 1] + 1,
                                         f"'{name}' coil_group[{i}] não contíguo: {sub}")

    def test_07_reg_groups_are_contiguous(self):
        """Endereços dentro de cada sub-grupo de registers devem ser consecutivos."""
        for name, data in self.operacao.items():
            for i, sub in enumerate(data['reg_groups']):
                with self.subTest(group=name, sub=i):
                    for j in range(1, len(sub)):
                        self.assertEqual(sub[j], sub[j - 1] + 1,
                                         f"'{name}' reg_group[{i}] não contíguo: {sub}")

    # ── Tamanhos consistentes ────────────────────────────────────────────────

    def test_08_coil_tags_length_matches_group(self):
        """len(coil_tags[i]) == len(coil_groups[i]) para todo i."""
        for name, data in {**self.operacao, **self.configuracao}.items():
            for i, (sub, tags) in enumerate(zip(data['coil_groups'], data['coil_tags'])):
                with self.subTest(group=name, sub=i):
                    self.assertEqual(len(sub), len(tags))

    def test_09_reg_tags_length_matches_group(self):
        """len(reg_tags[i]) == len(reg_groups[i]) para todo i."""
        for name, data in {**self.operacao, **self.configuracao}.items():
            for i, (sub, tags) in enumerate(zip(data['reg_groups'], data['reg_tags'])):
                with self.subTest(group=name, sub=i):
                    self.assertEqual(len(sub), len(tags))

    # ── Grupos específicos ───────────────────────────────────────────────────

    def test_10_extrusora_has_coils_and_registers(self):
        ext = self.operacao['Extrusora']
        self.assertGreater(len(ext['coil_groups']), 0, "Extrusora deve ter coils")
        self.assertGreater(len(ext['reg_groups']),  0, "Extrusora deve ter registers")

    def test_11_dosador_only_coil(self):
        dosador = self.operacao['dosador']
        self.assertGreater(len(dosador['coil_groups']), 0)
        self.assertEqual(dosador['reg_groups'], [], "dosador não deve ter registers")

    def test_12_alimentador_only_coil(self):
        alimentador = self.operacao['alimentador']
        self.assertGreater(len(alimentador['coil_groups']), 0)
        self.assertEqual(alimentador['reg_groups'], [])

    def test_13_groups_dont_share_addresses(self):
        """Endereços de coil não devem se repetir entre grupos diferentes."""
        seen = {}
        for name, data in self.operacao.items():
            for sub in data['coil_groups']:
                for addr in sub:
                    if addr in seen:
                        self.fail(
                            f"Endereço {addr} aparece em '{name}' e '{seen[addr]}'"
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
        cls.operacao_groups = set(extract_parameters_by_group(OPERACAO_CSV).keys())

    def test_30_has_meta_section(self):
        self.assertIn('_meta', self.config)

    def test_31_has_groups_section(self):
        self.assertIn('groups', self.config)
        self.assertIsInstance(self.config['groups'], dict)
        self.assertGreater(len(self.config['groups']), 0)

    def test_32_meta_fields(self):
        meta = self.config['_meta']
        self.assertIn('backward_compatible', meta)
        self.assertIn('default_delay_ms',    meta)
        self.assertIn('default_history_size', meta)

    def test_33_all_operacao_groups_configured(self):
        """Todos os grupos de operacao.csv devem ter entrada em group_config.json."""
        configured = set(self.config['groups'].keys())
        for name in self.operacao_groups:
            with self.subTest(group=name):
                self.assertIn(name, configured,
                              f"Grupo '{name}' ausente em group_config.json")

    def test_34_required_fields_per_group(self):
        for name, cfg in self.config['groups'].items():
            with self.subTest(group=name):
                self.assertIn('channel',      cfg)
                self.assertIn('delay_ms',     cfg)
                self.assertIn('history_size', cfg)

    def test_35_delay_ms_positive(self):
        for name, cfg in self.config['groups'].items():
            with self.subTest(group=name):
                self.assertGreater(cfg['delay_ms'], 0)

    def test_36_history_size_positive(self):
        for name, cfg in self.config['groups'].items():
            with self.subTest(group=name):
                self.assertGreater(cfg['history_size'], 0)

    def test_37_channels_use_plc_prefix(self):
        for name, cfg in self.config['groups'].items():
            with self.subTest(group=name):
                self.assertTrue(
                    cfg['channel'].startswith('plc_'),
                    f"'{name}': canal '{cfg['channel']}' sem prefixo 'plc_'"
                )

    def test_38_known_channels_present(self):
        channels = {cfg['channel'] for cfg in self.config['groups'].values()}
        for expected in ('plc_alarmes', 'plc_process', 'plc_visual', 'plc_config'):
            with self.subTest(channel=expected):
                self.assertIn(expected, channels)


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
