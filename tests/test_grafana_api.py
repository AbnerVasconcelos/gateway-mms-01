#!/usr/bin/env python3
"""
Tests for Hub/grafana_api.py — Grafana SimpleJSON-compatible endpoints.

Unit tests only — no external dependencies (no Redis, no Hub subprocess).

Usage:
    python -m pytest tests/test_grafana_api.py -v
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GATEWAY_DIR)
sys.path.insert(0, os.path.join(GATEWAY_DIR, 'Hub'))

import grafana_api  # noqa: E402


class TestGrafanaHelpers(unittest.TestCase):
    """Tests for helper functions (pure logic, no async)."""

    def test_parse_metric_valid(self):
        result = grafana_api._parse_metric('simulador.plc_operacao.controle_extrusora.extrusoraFeedBackSpeed')
        self.assertEqual(result['device_id'], 'simulador')
        self.assertEqual(result['channel'], 'plc_operacao')
        self.assertEqual(result['group'], 'controle_extrusora')
        self.assertEqual(result['tag'], 'extrusoraFeedBackSpeed')

    def test_parse_metric_invalid_too_few_parts(self):
        self.assertIsNone(grafana_api._parse_metric('only.two'))
        self.assertIsNone(grafana_api._parse_metric('three.parts.here'))

    def test_parse_metric_tag_with_dots(self):
        """Tag containing dots should be preserved as a single string."""
        result = grafana_api._parse_metric('dev.ch.grp.tag.with.dots')
        self.assertIsNotNone(result)
        self.assertEqual(result['tag'], 'tag.with.dots')

    def test_extract_metrics_from_message(self):
        data = {
            'coils': {'alarmes': {'emergencia': False, 'parada': True}},
            'registers': {'extrusora': {'speed': 1450}},
            'timestamp': '2026-01-01T00:00:00',
        }
        metrics = grafana_api._extract_metrics_from_message(data, 'sim', 'plc_alarmes')
        self.assertIn('sim.plc_alarmes.alarmes.emergencia', metrics)
        self.assertIn('sim.plc_alarmes.alarmes.parada', metrics)
        self.assertIn('sim.plc_alarmes.extrusora.speed', metrics)
        self.assertEqual(len(metrics), 3)

    def test_extract_metrics_empty_message(self):
        metrics = grafana_api._extract_metrics_from_message({}, 'sim', 'ch')
        self.assertEqual(metrics, [])

    def test_extract_value_from_registers(self):
        data = {
            'coils': {},
            'registers': {'extrusora': {'speed': 1450}},
        }
        val = grafana_api._extract_value(data, 'extrusora', 'speed')
        self.assertEqual(val, 1450)

    def test_extract_value_from_coils(self):
        data = {
            'coils': {'alarmes': {'emergencia': True}},
            'registers': {},
        }
        val = grafana_api._extract_value(data, 'alarmes', 'emergencia')
        self.assertEqual(val, 1)  # bool → int

    def test_extract_value_coil_false(self):
        data = {
            'coils': {'alarmes': {'emergencia': False}},
            'registers': {},
        }
        val = grafana_api._extract_value(data, 'alarmes', 'emergencia')
        self.assertEqual(val, 0)  # False → 0

    def test_extract_value_missing(self):
        data = {'coils': {}, 'registers': {}}
        val = grafana_api._extract_value(data, 'nonexistent', 'tag')
        self.assertIsNone(val)

    def test_parse_timestamp_ms(self):
        ts = grafana_api._parse_timestamp_ms('2026-01-01T00:00:00')
        self.assertIsNotNone(ts)
        self.assertIsInstance(ts, int)
        self.assertGreater(ts, 0)

    def test_parse_timestamp_ms_invalid(self):
        self.assertIsNone(grafana_api._parse_timestamp_ms('not-a-date'))
        self.assertIsNone(grafana_api._parse_timestamp_ms(None))
        self.assertIsNone(grafana_api._parse_timestamp_ms(''))


class TestGrafanaTimeserie(unittest.TestCase):
    """Tests for _build_timeserie_response."""

    def _make_history(self, values, base_ts_ms=1700000000000, interval_ms=1000):
        """Create a list of history items (oldest first)."""
        items = []
        for i, val in enumerate(values):
            ts_ms = base_ts_ms + i * interval_ms
            from datetime import datetime, timezone
            ts_str = datetime.fromtimestamp(ts_ms / 1000).isoformat()
            items.append({
                'coils': {},
                'registers': {'grp': {'tag': val}},
                'timestamp': ts_str,
            })
        return items

    def test_basic_timeserie(self):
        history = self._make_history([10, 20, 30])
        result = grafana_api._build_timeserie_response(
            'dev.ch.grp.tag', history, 'grp', 'tag', None, None, 1000)
        self.assertEqual(result['target'], 'dev.ch.grp.tag')
        self.assertEqual(len(result['datapoints']), 3)
        # Values should be [value, timestamp_ms]
        self.assertEqual(result['datapoints'][0][0], 10.0)
        self.assertEqual(result['datapoints'][1][0], 20.0)
        self.assertEqual(result['datapoints'][2][0], 30.0)

    def test_timeserie_time_range_filter(self):
        base = 1700000000000
        history = self._make_history([10, 20, 30, 40, 50], base_ts_ms=base, interval_ms=1000)
        # Filter: only items 2-4 (ts base+1000 to base+3000)
        result = grafana_api._build_timeserie_response(
            'dev.ch.grp.tag', history, 'grp', 'tag',
            base + 1000, base + 3000, 1000)
        # Should include values at base+1000, base+2000, base+3000
        self.assertEqual(len(result['datapoints']), 3)
        self.assertEqual(result['datapoints'][0][0], 20.0)

    def test_timeserie_downsampling(self):
        history = self._make_history(list(range(100)))
        result = grafana_api._build_timeserie_response(
            'dev.ch.grp.tag', history, 'grp', 'tag', None, None, 10)
        self.assertLessEqual(len(result['datapoints']), 10)

    def test_timeserie_boolean_coil(self):
        """Boolean coils should be converted to 0/1."""
        from datetime import datetime
        history = [
            {'coils': {'grp': {'tag': True}}, 'registers': {},
             'timestamp': datetime.fromtimestamp(1700000000).isoformat()},
            {'coils': {'grp': {'tag': False}}, 'registers': {},
             'timestamp': datetime.fromtimestamp(1700000001).isoformat()},
        ]
        result = grafana_api._build_timeserie_response(
            'dev.ch.grp.tag', history, 'grp', 'tag', None, None, 1000)
        self.assertEqual(len(result['datapoints']), 2)
        self.assertEqual(result['datapoints'][0][0], 1.0)
        self.assertEqual(result['datapoints'][1][0], 0.0)

    def test_timeserie_missing_tag_returns_empty(self):
        history = self._make_history([10, 20])
        result = grafana_api._build_timeserie_response(
            'dev.ch.grp.nonexistent', history, 'grp', 'nonexistent', None, None, 1000)
        self.assertEqual(result['datapoints'], [])


class TestGrafanaTable(unittest.TestCase):
    """Tests for _build_table_response."""

    def test_table_response_format(self):
        from datetime import datetime
        history = [
            {'coils': {}, 'registers': {'grp': {'tag': 42}},
             'timestamp': datetime.fromtimestamp(1700000000).isoformat()},
        ]
        result = grafana_api._build_table_response(
            'dev.ch.grp.tag', history, 'grp', 'tag', None, None)
        self.assertEqual(result['type'], 'table')
        self.assertEqual(len(result['columns']), 3)
        self.assertEqual(result['columns'][0]['text'], 'Time')
        self.assertEqual(result['columns'][1]['text'], 'Metric')
        self.assertEqual(result['columns'][2]['text'], 'Value')
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0][1], 'dev.ch.grp.tag')
        self.assertEqual(result['rows'][0][2], 42)


class TestGrafanaEndpoints(unittest.TestCase):
    """Tests for the async endpoint functions using mocks."""

    def setUp(self):
        self._orig_redis = grafana_api._redis
        self._orig_get_channels = grafana_api._get_channels
        self._orig_get_variables = grafana_api._get_variables
        self._orig_search_cache = grafana_api._search_cache.copy()
        self._orig_history_cache = grafana_api._history_cache.copy()

        # Reset caches
        grafana_api._search_cache = {'metrics': [], 'ts': 0.0}
        grafana_api._history_cache = {}

    def tearDown(self):
        grafana_api._redis = self._orig_redis
        grafana_api._get_channels = self._orig_get_channels
        grafana_api._get_variables = self._orig_get_variables
        grafana_api._search_cache = self._orig_search_cache
        grafana_api._history_cache = self._orig_history_cache

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_health_returns_ok(self):
        result = self._run(grafana_api.grafana_health())
        self.assertEqual(result, 'OK')

    def test_search_with_history_data(self):
        mock_redis = AsyncMock()
        history_item = json.dumps({
            'coils': {'alarmes': {'emergencia': False}},
            'registers': {'extrusora': {'speed': 1450}},
            'timestamp': '2026-01-01T00:00:00',
        }).encode()
        mock_redis.lindex = AsyncMock(return_value=history_item)

        grafana_api._redis = mock_redis
        grafana_api._get_channels = lambda: {
            'plc_operacao': {'device_id': 'sim', 'delay_ms': 1000, 'history_size': 100},
        }
        grafana_api._get_variables = lambda: []

        result = self._run(grafana_api.grafana_search({}))
        self.assertIsInstance(result, list)
        self.assertIn('sim.plc_operacao.alarmes.emergencia', result)
        self.assertIn('sim.plc_operacao.extrusora.speed', result)

    def test_search_with_filter(self):
        mock_redis = AsyncMock()
        history_item = json.dumps({
            'coils': {},
            'registers': {'extrusora': {'speed': 1450, 'temp': 200}},
            'timestamp': '2026-01-01T00:00:00',
        }).encode()
        mock_redis.lindex = AsyncMock(return_value=history_item)

        grafana_api._redis = mock_redis
        grafana_api._get_channels = lambda: {
            'plc_op': {'device_id': 'sim', 'delay_ms': 1000, 'history_size': 100},
        }
        grafana_api._get_variables = lambda: []

        result = self._run(grafana_api.grafana_search({'target': 'speed'}))
        self.assertTrue(all('speed' in m.lower() for m in result))

    def test_search_fallback_to_variables(self):
        """When no history data, should fall back to config variables."""
        mock_redis = AsyncMock()
        mock_redis.lindex = AsyncMock(return_value=None)

        grafana_api._redis = mock_redis
        grafana_api._get_channels = lambda: {
            'plc_alarmes': {'device_id': 'sim', 'delay_ms': 1000, 'history_size': 100},
        }
        grafana_api._get_variables = lambda: [
            {'tag': 'emergencia', 'channel': 'plc_alarmes', 'device': 'sim', 'group': 'alarmes'},
        ]

        result = self._run(grafana_api.grafana_search({}))
        self.assertIn('sim.plc_alarmes.alarmes.emergencia', result)

    def test_query_timeserie(self):
        from datetime import datetime

        ts1 = datetime.fromtimestamp(1700000000).isoformat()
        ts2 = datetime.fromtimestamp(1700000001).isoformat()

        history_data = [
            json.dumps({'coils': {}, 'registers': {'grp': {'tag': 20}}, 'timestamp': ts2}).encode(),
            json.dumps({'coils': {}, 'registers': {'grp': {'tag': 10}}, 'timestamp': ts1}).encode(),
        ]

        mock_redis = AsyncMock()
        mock_redis.lrange = AsyncMock(return_value=history_data)
        grafana_api._redis = mock_redis
        grafana_api._get_channels = lambda: {}

        body = {
            'targets': [{'target': 'dev.ch.grp.tag', 'type': 'timeserie'}],
            'range': {'from': ts1, 'to': ts2},
        }
        result = self._run(grafana_api.grafana_query(body))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['target'], 'dev.ch.grp.tag')
        # History is newest-first in Redis, reversed internally → oldest first
        self.assertEqual(len(result[0]['datapoints']), 2)
        self.assertEqual(result[0]['datapoints'][0][0], 10.0)
        self.assertEqual(result[0]['datapoints'][1][0], 20.0)

    def test_query_table_format(self):
        from datetime import datetime
        ts = datetime.fromtimestamp(1700000000).isoformat()
        history_data = [
            json.dumps({'coils': {}, 'registers': {'grp': {'tag': 42}}, 'timestamp': ts}).encode(),
        ]

        mock_redis = AsyncMock()
        mock_redis.lrange = AsyncMock(return_value=history_data)
        grafana_api._redis = mock_redis
        grafana_api._get_channels = lambda: {}

        body = {
            'targets': [{'target': 'dev.ch.grp.tag', 'type': 'table'}],
            'range': {'from': ts, 'to': ts},
        }
        result = self._run(grafana_api.grafana_query(body))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['type'], 'table')
        self.assertEqual(len(result[0]['rows']), 1)
        self.assertEqual(result[0]['rows'][0][2], 42)

    def test_query_unknown_metric(self):
        """Invalid metric format should be silently skipped."""
        mock_redis = AsyncMock()
        grafana_api._redis = mock_redis

        body = {
            'targets': [{'target': 'invalid_metric'}],
            'range': {},
        }
        result = self._run(grafana_api.grafana_query(body))
        self.assertEqual(result, [])

    def test_query_multi_target_grouping(self):
        """Multiple targets on the same channel should share one Redis read."""
        from datetime import datetime
        ts = datetime.fromtimestamp(1700000000).isoformat()
        history_data = [
            json.dumps({
                'coils': {},
                'registers': {'grp': {'speed': 1450, 'temp': 200}},
                'timestamp': ts,
            }).encode(),
        ]

        mock_redis = AsyncMock()
        mock_redis.lrange = AsyncMock(return_value=history_data)
        grafana_api._redis = mock_redis
        grafana_api._get_channels = lambda: {}

        body = {
            'targets': [
                {'target': 'dev.ch.grp.speed', 'type': 'timeserie'},
                {'target': 'dev.ch.grp.temp', 'type': 'timeserie'},
            ],
            'range': {'from': ts, 'to': ts},
        }
        result = self._run(grafana_api.grafana_query(body))
        self.assertEqual(len(result), 2)
        # Only one lrange call because both targets share channel 'ch'
        mock_redis.lrange.assert_called_once()

    def test_search_cache_expires(self):
        """Search cache should expire after TTL."""
        import time

        mock_redis = AsyncMock()
        mock_redis.lindex = AsyncMock(return_value=None)

        grafana_api._redis = mock_redis
        grafana_api._get_channels = lambda: {}
        grafana_api._get_variables = lambda: [
            {'tag': 'x', 'channel': 'ch', 'device': 'd', 'group': 'g'},
        ]

        # First call populates cache
        result1 = self._run(grafana_api.grafana_search({}))

        # Expire cache
        grafana_api._search_cache['ts'] = time.monotonic() - 60

        # Change variables
        grafana_api._get_variables = lambda: [
            {'tag': 'y', 'channel': 'ch', 'device': 'd', 'group': 'g'},
        ]

        result2 = self._run(grafana_api.grafana_search({}))
        self.assertIn('d.ch.g.y', result2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
