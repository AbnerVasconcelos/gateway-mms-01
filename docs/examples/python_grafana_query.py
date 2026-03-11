#!/usr/bin/env python3
"""
Example: Querying the Grafana API endpoints programmatically.

Demonstrates how to use the SimpleJSON-compatible API directly
(without Grafana) for data extraction, scripting, or custom dashboards.

Requirements:
    pip install requests

Usage:
    python python_grafana_query.py
"""

import json
from datetime import datetime, timedelta
import requests

HUB_URL = 'http://localhost:4567'
GRAFANA_API = f'{HUB_URL}/api/grafana'


def test_connection():
    """Test that the Grafana API is reachable."""
    resp = requests.get(f'{GRAFANA_API}/')
    print(f"Health check: {resp.status_code} — {resp.text}")
    return resp.status_code == 200


def search_metrics(filter_text=''):
    """Search for available metrics.

    Args:
        filter_text: Optional filter string (case-insensitive substring match)

    Returns:
        List of metric name strings
    """
    resp = requests.post(
        f'{GRAFANA_API}/search',
        json={'target': filter_text},
    )
    resp.raise_for_status()
    return resp.json()


def query_timeseries(metrics, minutes_back=15, max_points=500):
    """Query time-series data for one or more metrics.

    Args:
        metrics: List of metric name strings
        minutes_back: How far back to look
        max_points: Maximum data points per metric

    Returns:
        List of {target, datapoints: [[value, timestamp_ms], ...]}
    """
    now = datetime.now()
    from_time = now - timedelta(minutes=minutes_back)

    resp = requests.post(
        f'{GRAFANA_API}/query',
        json={
            'targets': [{'target': m, 'type': 'timeserie'} for m in metrics],
            'range': {
                'from': from_time.isoformat(),
                'to': now.isoformat(),
            },
            'maxDataPoints': max_points,
        },
    )
    resp.raise_for_status()
    return resp.json()


def query_table(metric, minutes_back=15):
    """Query table-format data for a single metric.

    Returns:
        {type: "table", columns: [...], rows: [[ts, metric, value], ...]}
    """
    now = datetime.now()
    from_time = now - timedelta(minutes=minutes_back)

    resp = requests.post(
        f'{GRAFANA_API}/query',
        json={
            'targets': [{'target': metric, 'type': 'table'}],
            'range': {
                'from': from_time.isoformat(),
                'to': now.isoformat(),
            },
        },
    )
    resp.raise_for_status()
    results = resp.json()
    return results[0] if results else None


def main():
    print("=== Grafana API Example ===\n")

    # 1. Test connection
    if not test_connection():
        print("Hub not reachable. Is it running?")
        return

    # 2. Search all metrics
    print("\n--- All available metrics ---")
    all_metrics = search_metrics()
    for m in all_metrics[:20]:  # Show first 20
        print(f"  {m}")
    if len(all_metrics) > 20:
        print(f"  ... and {len(all_metrics) - 20} more")
    print(f"Total: {len(all_metrics)} metrics\n")

    # 3. Search with filter
    print("--- Metrics matching 'temperatura' ---")
    temp_metrics = search_metrics('temperatura')
    for m in temp_metrics[:10]:
        print(f"  {m}")
    print(f"Found: {len(temp_metrics)}\n")

    # 4. Query time-series
    if all_metrics:
        metric = all_metrics[0]
        print(f"--- Time-series for '{metric}' (last 15 min) ---")
        results = query_timeseries([metric], minutes_back=15)
        if results and results[0]['datapoints']:
            dp = results[0]['datapoints']
            print(f"  {len(dp)} data points")
            print(f"  Latest: value={dp[-1][0]}, ts={dp[-1][1]}")
            print(f"  Oldest: value={dp[0][0]}, ts={dp[0][1]}")
        else:
            print("  No data points in the last 15 minutes")

        # 5. Query table format
        print(f"\n--- Table format for '{metric}' ---")
        table = query_table(metric, minutes_back=15)
        if table and table.get('rows'):
            print(f"  Columns: {[c['text'] for c in table['columns']]}")
            print(f"  Rows: {len(table['rows'])}")
            print(f"  First row: {table['rows'][0]}")
        else:
            print("  No table data")


if __name__ == '__main__':
    main()
