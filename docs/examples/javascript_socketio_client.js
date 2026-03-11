/**
 * Example: Socket.IO client for the Gateway IoT Industrial.
 *
 * Works in both browser and Node.js environments.
 *
 * Browser: include socket.io-client via CDN
 *   <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
 *
 * Node.js:
 *   npm install socket.io-client
 *   node javascript_socketio_client.js
 */

// Node.js import (comment out for browser)
const { io } = require('socket.io-client');

const HUB_URL = 'http://localhost:4567';
const ROOMS = ['simulador']; // Join the 'simulador' device room

const socket = io(HUB_URL, {
  transports: ['websocket', 'polling'],
  reconnection: true,
  reconnectionDelay: 1000,
});

socket.on('connection_ack', (data) => {
  console.log('Connected. Available rooms:', data.available_rooms);
  socket.emit('join', { rooms: ROOMS });
  console.log('Joined rooms:', ROOMS);
});

socket.on('device:data', (data) => {
  const { device_id, channel, data: payload } = data;
  const { coils = {}, registers = {}, timestamp } = payload;

  console.log(`\n[${timestamp}] ${device_id}/${channel}`);

  for (const [group, tags] of Object.entries(coils)) {
    for (const [tag, val] of Object.entries(tags)) {
      console.log(`  COIL  ${group}.${tag} = ${val}`);
    }
  }

  for (const [group, tags] of Object.entries(registers)) {
    for (const [tag, val] of Object.entries(tags)) {
      console.log(`  REG   ${group}.${tag} = ${val}`);
    }
  }
});

socket.on('channel:data', (data) => {
  const { coils = {}, registers = {}, timestamp } = data;
  console.log(`\n[channel:data] ${timestamp}`);
  const all = { ...coils, ...registers };
  for (const [group, tags] of Object.entries(all)) {
    for (const [tag, val] of Object.entries(tags)) {
      console.log(`  ${group}.${tag} = ${val}`);
    }
  }
});

socket.on('disconnect', () => {
  console.log('Disconnected from Hub.');
});

socket.on('connect_error', (err) => {
  console.error('Connection error:', err.message);
});

console.log(`Connecting to ${HUB_URL}...`);
