#!/usr/bin/env python3
"""
Example: Real-time Socket.IO subscriber for the Gateway IoT Industrial.

Connects to the Hub and receives live Modbus data via Socket.IO.

Requirements:
    pip install python-socketio[client] websocket-client

Usage:
    python python_socketio_client.py
"""

import json
import socketio

HUB_URL = 'http://localhost:4567'

# Which device rooms to join (device_id or device_id:channel)
ROOMS = ['simulador']  # All channels for 'simulador' device
# ROOMS = ['simulador:plc_alarmes']  # Only the alarm channel

sio = socketio.Client(logger=False)


@sio.on('connection_ack')
def on_connect(data):
    print(f"Connected. Available rooms: {data.get('available_rooms', [])}")
    sio.emit('join', {'rooms': ROOMS})
    print(f"Joined rooms: {ROOMS}")


@sio.on('device:data')
def on_device_data(data):
    """Received when joined to a device room (e.g., 'simulador')."""
    channel = data.get('channel', '?')
    device_id = data.get('device_id', '?')
    payload = data.get('data', {})
    timestamp = payload.get('timestamp', '')

    coils = payload.get('coils', {})
    registers = payload.get('registers', {})

    print(f"\n[{timestamp}] {device_id}/{channel}")
    for group, tags in coils.items():
        for tag, val in tags.items():
            print(f"  COIL  {group}.{tag} = {val}")
    for group, tags in registers.items():
        for tag, val in tags.items():
            print(f"  REG   {group}.{tag} = {val}")


@sio.on('channel:data')
def on_channel_data(data):
    """Received when joined to a specific channel room (e.g., 'simulador:plc_alarmes')."""
    timestamp = data.get('timestamp', '')
    coils = data.get('coils', {})
    registers = data.get('registers', {})
    print(f"\n[channel:data] {timestamp}")
    for group, tags in {**coils, **registers}.items():
        for tag, val in tags.items():
            print(f"  {group}.{tag} = {val}")


@sio.on('disconnect')
def on_disconnect():
    print("Disconnected from Hub.")


def main():
    print(f"Connecting to {HUB_URL}...")
    sio.connect(HUB_URL)
    try:
        sio.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        sio.disconnect()


if __name__ == '__main__':
    main()
