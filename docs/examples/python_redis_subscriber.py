#!/usr/bin/env python3
"""
Example: Direct Redis pub/sub subscriber for the Gateway IoT Industrial.

Subscribes to Redis channels and prints incoming Modbus data.
No Hub dependency — connects directly to Redis.

Requirements:
    pip install redis

Usage:
    python python_redis_subscriber.py
"""

import json
import redis

REDIS_HOST = 'localhost'
REDIS_PORT = 6379

# Channels to subscribe to
CHANNELS = ['plc_alarmes', 'plc_operacao', 'plc_retentivas']


def main():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0)

    # Verify connection
    try:
        r.ping()
        print(f"Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
    except redis.ConnectionError as e:
        print(f"Failed to connect to Redis: {e}")
        return

    ps = r.pubsub()
    ps.subscribe(*CHANNELS)
    print(f"Subscribed to: {CHANNELS}")
    print("Waiting for messages... (Ctrl+C to stop)\n")

    try:
        for msg in ps.listen():
            if msg['type'] != 'message':
                continue

            channel = msg['channel'].decode()
            try:
                data = json.loads(msg['data'])
            except json.JSONDecodeError:
                print(f"[{channel}] Invalid JSON: {msg['data']}")
                continue

            timestamp = data.get('timestamp', '?')
            coils = data.get('coils', {})
            registers = data.get('registers', {})

            print(f"[{timestamp}] Channel: {channel}")

            for group, tags in coils.items():
                for tag, val in tags.items():
                    print(f"  COIL  {group}.{tag} = {val}")

            for group, tags in registers.items():
                for tag, val in tags.items():
                    print(f"  REG   {group}.{tag} = {val}")

            print()

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        ps.unsubscribe()
        ps.close()
        r.close()


if __name__ == '__main__':
    main()
