"""
grafana_api — Grafana SimpleJSON-compatible endpoints.

Mounts at /api/grafana/ and provides:
  GET  /           — health check (datasource test)
  POST /search     — available metrics list
  POST /query      — time-series or table data from Redis history

Metric naming: {device_id}.{channel}.{group}.{tag}
Example: simulador.plc_operacao.controle_extrusora.extrusoraFeedBackSpeed

Usage:
    from Hub import grafana_api
    app.include_router(grafana_api.router)
    grafana_api.init(redis_conn, get_channels_fn, get_variables_fn)
"""

import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Optional

import redis.asyncio as aioredis
from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/grafana', tags=['grafana'])

# Injected dependencies
_redis: Optional[aioredis.Redis] = None
_get_channels: Optional[Callable] = None
_get_variables: Optional[Callable] = None

# Caches
_search_cache: dict = {'metrics': [], 'ts': 0.0}
_SEARCH_CACHE_TTL = 30.0

_history_cache: dict = {}  # {channel: {'data': [...], 'ts': float}}
_HISTORY_CACHE_TTL = 2.0


def init(redis_conn: aioredis.Redis, get_channels_fn: Callable, get_variables_fn: Callable) -> None:
    """Initialize module dependencies. Called from main.py on_startup."""
    global _redis, _get_channels, _get_variables
    _redis = redis_conn
    _get_channels = get_channels_fn
    _get_variables = get_variables_fn
    logger.info("Grafana API inicializada.")


def _parse_metric(metric: str) -> Optional[dict]:
    """Parse metric name into components.

    Format: device_id.channel.group.tag
    Returns dict with keys: device_id, channel, group, tag
    Returns None if format is invalid.
    """
    parts = metric.split('.', 3)
    if len(parts) != 4:
        return None
    return {
        'device_id': parts[0],
        'channel': parts[1],
        'group': parts[2],
        'tag': parts[3],
    }


def _extract_metrics_from_message(data: dict, device_id: str, channel: str) -> list[str]:
    """Extract metric names from a Delfos message payload.

    Message format: {coils: {group: {tag: val}}, registers: {group: {tag: val}}, timestamp: ...}
    """
    metrics = []
    for section in ('coils', 'registers'):
        groups = data.get(section, {})
        if not isinstance(groups, dict):
            continue
        for group, tags in groups.items():
            if not isinstance(tags, dict):
                continue
            for tag in tags:
                metrics.append(f'{device_id}.{channel}.{group}.{tag}')
    return metrics


def _extract_value(data: dict, group: str, tag: str) -> Optional[Any]:
    """Extract a value from a Delfos message by group and tag.

    Searches both coils and registers sections.
    Boolean coils are converted to 0/1 for numeric time-series.
    """
    for section in ('coils', 'registers'):
        groups = data.get(section, {})
        if not isinstance(groups, dict):
            continue
        tags = groups.get(group)
        if not isinstance(tags, dict):
            continue
        if tag in tags:
            val = tags[tag]
            if isinstance(val, bool):
                return 1 if val else 0
            return val
    return None


def _parse_timestamp_ms(ts_str: str) -> Optional[int]:
    """Parse ISO 8601 timestamp to milliseconds since epoch."""
    try:
        dt = datetime.fromisoformat(ts_str)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


async def _get_history(channel: str, limit: int = 1000) -> list[dict]:
    """Get channel history from Redis with caching."""
    now = time.monotonic()
    cached = _history_cache.get(channel)
    if cached and (now - cached['ts']) < _HISTORY_CACHE_TTL:
        return cached['data']

    if not _redis:
        return []

    raw = await _redis.lrange(f'history:{channel}', 0, limit - 1)
    items = []
    for entry in raw:
        try:
            items.append(json.loads(entry))
        except (json.JSONDecodeError, TypeError):
            continue

    _history_cache[channel] = {'data': items, 'ts': now}
    return items


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get('/')
async def grafana_health():
    """Health check — Grafana uses this to test the datasource connection."""
    return 'OK'


@router.post('/search')
async def grafana_search(body: dict = {}):
    """Return available metrics for Grafana dropdown.

    Request body (optional): {"target": "filter string"}
    Response: list of metric name strings.
    """
    now = time.monotonic()
    target_filter = body.get('target', '') if isinstance(body, dict) else ''

    # Check cache
    if _search_cache['metrics'] and (now - _search_cache['ts']) < _SEARCH_CACHE_TTL:
        metrics = _search_cache['metrics']
    else:
        metrics = await _build_metrics_list()
        _search_cache['metrics'] = metrics
        _search_cache['ts'] = now

    if target_filter:
        lower_filter = target_filter.lower()
        metrics = [m for m in metrics if lower_filter in m.lower()]

    return sorted(metrics)


async def _build_metrics_list() -> list[str]:
    """Build the full list of available metrics.

    Strategy:
    1. Read latest history item per channel, extract group.tag paths
    2. Fall back to config_store variables for channels with no history
    """
    metrics: set[str] = set()

    if not _get_channels:
        return []

    channels = _get_channels()

    # Strategy 1: Extract from live history data
    for channel, info in channels.items():
        device_id = info.get('device_id', 'unknown')
        if not _redis:
            continue
        try:
            raw = await _redis.lindex(f'history:{channel}', 0)
            if raw:
                data = json.loads(raw)
                found = _extract_metrics_from_message(data, device_id, channel)
                metrics.update(found)
        except Exception:
            continue

    # Strategy 2: Fall back to config variables for channels with no history metrics
    if _get_variables:
        channels_with_metrics = set()
        for m in metrics:
            parsed = _parse_metric(m)
            if parsed:
                channels_with_metrics.add(parsed['channel'])

        try:
            variables = _get_variables()
        except Exception:
            variables = []

        for var in variables:
            ch = var.get('channel')
            if not ch or ch in channels_with_metrics:
                continue
            device_id = var.get('device', 'unknown')
            group = var.get('group', 'unknown')
            tag = var.get('tag')
            if tag:
                metrics.add(f'{device_id}.{ch}.{group}.{tag}')

    return list(metrics)


@router.post('/query')
async def grafana_query(body: dict = {}):
    """Return time-series or table data for Grafana panels.

    SimpleJSON query request format:
    {
      "range": {"from": "ISO", "to": "ISO"},
      "targets": [{"target": "metric.name", "type": "timeserie|table"}],
      "maxDataPoints": 1000
    }
    """
    targets = body.get('targets', [])
    range_info = body.get('range', {})
    max_points = body.get('maxDataPoints', 1000)

    range_from = _parse_timestamp_ms(range_info.get('from', ''))
    range_to = _parse_timestamp_ms(range_info.get('to', ''))

    # Group targets by channel to minimize Redis reads
    channel_targets: dict[str, list[dict]] = {}
    for t in targets:
        metric = t.get('target', '')
        resp_type = t.get('type', 'timeserie')
        parsed = _parse_metric(metric)
        if not parsed:
            continue
        ch = parsed['channel']
        channel_targets.setdefault(ch, []).append({
            'metric': metric,
            'type': resp_type,
            **parsed,
        })

    results = []

    for channel, tgt_list in channel_targets.items():
        # Read history once per channel
        history = await _get_history(channel)

        # History is newest-first; reverse for oldest-first
        history_asc = list(reversed(history))

        for tgt in tgt_list:
            group = tgt['group']
            tag = tgt['tag']
            resp_type = tgt['type']

            if resp_type == 'table':
                results.append(_build_table_response(tgt['metric'], history_asc, group, tag,
                                                     range_from, range_to))
            else:
                results.append(_build_timeserie_response(tgt['metric'], history_asc, group, tag,
                                                         range_from, range_to, max_points))

    return results


def _build_timeserie_response(metric: str, history: list[dict], group: str, tag: str,
                               range_from: Optional[int], range_to: Optional[int],
                               max_points: int) -> dict:
    """Build a timeserie response for a single metric."""
    datapoints: list[list] = []

    for item in history:
        ts_ms = _parse_timestamp_ms(item.get('timestamp', ''))
        if ts_ms is None:
            continue

        # Time range filter
        if range_from and ts_ms < range_from:
            continue
        if range_to and ts_ms > range_to:
            continue

        value = _extract_value(item, group, tag)
        if value is not None:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            datapoints.append([numeric, ts_ms])

    # Downsample if exceeds maxDataPoints
    if max_points and len(datapoints) > max_points:
        step = len(datapoints) / max_points
        sampled = []
        i = 0.0
        while i < len(datapoints) and len(sampled) < max_points:
            sampled.append(datapoints[int(i)])
            i += step
        datapoints = sampled

    return {
        'target': metric,
        'datapoints': datapoints,
    }


def _build_table_response(metric: str, history: list[dict], group: str, tag: str,
                           range_from: Optional[int], range_to: Optional[int]) -> dict:
    """Build a table response for a single metric."""
    rows: list[list] = []

    for item in history:
        ts_ms = _parse_timestamp_ms(item.get('timestamp', ''))
        if ts_ms is None:
            continue

        if range_from and ts_ms < range_from:
            continue
        if range_to and ts_ms > range_to:
            continue

        value = _extract_value(item, group, tag)
        if value is not None:
            rows.append([ts_ms, metric, value])

    return {
        'type': 'table',
        'columns': [
            {'text': 'Time', 'type': 'time'},
            {'text': 'Metric', 'type': 'string'},
            {'text': 'Value', 'type': 'number'},
        ],
        'rows': rows,
    }
