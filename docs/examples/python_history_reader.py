#!/usr/bin/env python3
"""
Example: REST API history reader for the Gateway IoT Industrial.

Polls channel history via the Hub REST API at regular intervals.

Requirements:
    pip install requests

Usage:
    python python_history_reader.py
"""

import json
import time
import requests

HUB_URL = 'http://localhost:4567'
CHANNEL = 'plc_operacao'
POLL_INTERVAL = 5  # seconds
HISTORY_LIMIT = 10


def get_devices():
    """List all configured devices."""
    resp = requests.get(f'{HUB_URL}/api/devices')
    resp.raise_for_status()
    return resp.json()


def get_channels():
    """List all configured channels with settings."""
    resp = requests.get(f'{HUB_URL}/api/channels')
    resp.raise_for_status()
    return resp.json()


def get_variables():
    """List all variables with their channel assignments."""
    resp = requests.get(f'{HUB_URL}/api/variables')
    resp.raise_for_status()
    return resp.json()


def get_history(channel, limit=100):
    """Fetch recent history for a channel."""
    resp = requests.get(
        f'{HUB_URL}/api/channels/{channel}/history',
        params={'limit': limit},
    )
    resp.raise_for_status()
    return resp.json()


def main():
    # Show system info
    print("=== Devices ===")
    devices = get_devices()
    for dev_id, cfg in devices.items():
        print(f"  {dev_id}: {cfg.get('label', '')} ({cfg.get('protocol', 'tcp')})")

    print("\n=== Channels ===")
    channels = get_channels()
    for ch, cfg in channels.items():
        print(f"  {ch}: delay={cfg['delay_ms']}ms, history={cfg['history_size']}, device={cfg.get('device_id', '?')}")

    # Poll history
    print(f"\n=== Polling {CHANNEL} every {POLL_INTERVAL}s (limit={HISTORY_LIMIT}) ===")
    print("Press Ctrl+C to stop.\n")

    last_ts = None
    try:
        while True:
            data = get_history(CHANNEL, limit=HISTORY_LIMIT)
            items = data.get('items', [])

            if items:
                newest = items[0]
                ts = newest.get('timestamp', '')
                if ts != last_ts:
                    last_ts = ts
                    print(f"[{ts}] {data['count']} items in history")

                    # Print latest values
                    for section in ('coils', 'registers'):
                        groups = newest.get(section, {})
                        for group, tags in groups.items():
                            for tag, val in tags.items():
                                print(f"  {section}/{group}.{tag} = {val}")
                    print()
                else:
                    print(f"  (no new data)")
            else:
                print(f"  (empty history for {CHANNEL})")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == '__main__':
    main()
