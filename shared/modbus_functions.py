import logging
import os
from collections import defaultdict
import struct
import threading
import time
from time import sleep
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_MODBUS_HOST     = os.environ.get('MODBUS_HOST', '192.168.1.2')
_MODBUS_PORT     = int(os.environ.get('MODBUS_PORT', 502))
_MODBUS_UNIT_ID  = int(os.environ.get('MODBUS_UNIT_ID', 2))
_MODBUS_PROTOCOL = os.environ.get('MODBUS_PROTOCOL', 'tcp')
_SERIAL_PORT     = os.environ.get('SERIAL_PORT', '')
_SERIAL_BAUDRATE = int(os.environ.get('SERIAL_BAUDRATE', 19200))
_SERIAL_PARITY   = os.environ.get('SERIAL_PARITY', 'N')
_SERIAL_STOPBITS = int(os.environ.get('SERIAL_STOPBITS', 1))



class RawRtuClient:
    """Cliente Modbus RTU via serial raw — resiliente a barramentos compartilhados.

    Abre e fecha a porta serial a cada transacao (como west_read.py) para
    garantir estado limpo do buffer. Usa wait_for_silence para evitar colisao
    com outro mestre no barramento RS485.
    """

    def __init__(self, port, baudrate=19200, parity='N', stopbits=1, bytesize=8, timeout=0.1):
        self._port = port
        self._baudrate = baudrate
        self._parity = parity
        self._stopbits = stopbits
        self._bytesize = bytesize
        self._timeout = timeout
        self._connected = False

    @staticmethod
    def _crc16(data: bytes) -> bytes:
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return struct.pack("<H", crc)

    @staticmethod
    def _wait_for_silence(ser, silence_ms=5, max_wait=0.5):
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            ser.reset_input_buffer()
            time.sleep(silence_ms / 1000)
            if ser.in_waiting == 0:
                return

    def _open_serial(self):
        import serial as _serial
        return _serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            parity=self._parity,
            stopbits=self._stopbits,
            bytesize=self._bytesize,
            timeout=self._timeout,
        )

    def _transact(self, request: bytes, expected_len: int, retries: int = 12) -> bytes | None:
        expected_slave = request[0]
        expected_func = request[1]
        # Byte count esperado para FC03 (read holding registers)
        expected_byte_count = (expected_len - 5) if expected_func == 0x03 else -1
        for attempt in range(retries):
            try:
                with self._open_serial() as ser:
                    # Aguarda silencio prolongado (>10ms = gap seguro no ciclo de 52ms)
                    self._wait_for_silence(ser, silence_ms=12, max_wait=0.8)
                    ser.write(request)
                    ser.flush()
                    # Le resposta com deadline
                    deadline = time.monotonic() + 1.0
                    buf = b""
                    while time.monotonic() < deadline:
                        chunk = ser.read(expected_len - len(buf))
                        buf += chunk
                        if len(buf) >= expected_len:
                            break
            except Exception as exc:
                logger.debug("RTU tentativa %d/%d: erro serial: %s", attempt + 1, retries, exc)
                time.sleep(0.08)
                continue
            if len(buf) < 3:
                logger.debug("RTU tentativa %d/%d: sem resposta (%d bytes)", attempt + 1, retries, len(buf))
                time.sleep(0.08)
                continue
            # Verifica slave e funcao
            if buf[0] != expected_slave or buf[1] != expected_func:
                logger.debug("RTU tentativa %d/%d: resposta slave=%d func=%d (esperado %d/%d)",
                             attempt + 1, retries, buf[0], buf[1], expected_slave, expected_func)
                time.sleep(0.08)
                continue
            # Para FC03: verifica se byte count corresponde ao que pedimos
            if expected_byte_count > 0 and buf[2] != expected_byte_count:
                logger.debug("RTU tentativa %d/%d: byte_count=%d (esperado %d) — resposta de outro request",
                             attempt + 1, retries, buf[2], expected_byte_count)
                time.sleep(0.08)
                continue
            # Valida CRC antes de aceitar a resposta
            if len(buf) >= 4:
                if self._crc16(buf[:-2]) != bytes(buf[-2:]):
                    logger.debug("RTU tentativa %d/%d: CRC invalido — dados corrompidos",
                                 attempt + 1, retries)
                    time.sleep(0.08)
                    continue
            return buf
        return None

    def read_holding_registers(self, address, count, slave=1):
        payload = struct.pack(">BBHH", slave, 0x03, address, count)
        request = payload + self._crc16(payload)
        expected_len = 3 + count * 2 + 2

        buf = self._transact(request, expected_len)
        if buf is None:
            return _RtuError(f"No response (addr={address}, count={count})")
        if buf[2] != count * 2:
            return _RtuError(f"Bad byte count (addr={address}): expected {count*2}, got {buf[2]}")
        if self._crc16(buf[:-2]) != bytes(buf[-2:]):
            return _RtuError(f"CRC error (addr={address})")

        regs = [struct.unpack(">H", buf[3 + i*2:5 + i*2])[0] for i in range(count)]
        return _RtuResult(regs)

    def read_coils(self, address, count, slave=1):
        payload = struct.pack(">BBHH", slave, 0x01, address, count)
        request = payload + self._crc16(payload)
        byte_count = (count + 7) // 8
        expected_len = 3 + byte_count + 2

        buf = self._transact(request, expected_len)
        if buf is None:
            return _RtuError(f"No response (coils addr={address})")
        if buf[0] != slave or buf[1] != 0x01 or buf[2] != byte_count:
            return _RtuError(f"Bad coil response (addr={address})")
        if self._crc16(buf[:-2]) != bytes(buf[-2:]):
            return _RtuError(f"CRC error (coils addr={address})")

        bits = []
        for i in range(count):
            byte_idx = 3 + i // 8
            bit_idx = i % 8
            bits.append(bool((buf[byte_idx] >> bit_idx) & 1))
        return _RtuResult(bits)

    def write_coil(self, address, value, slave=1):
        coil_val = 0xFF00 if value else 0x0000
        payload = struct.pack(">BBHH", slave, 0x05, address, coil_val)
        request = payload + self._crc16(payload)
        buf = self._transact(request, 8)
        if buf is None:
            return _RtuError(f"No response (write coil addr={address})")
        if self._crc16(buf[:-2]) != bytes(buf[-2:]):
            return _RtuError(f"CRC error (write coil addr={address})")
        return _RtuResult(None)

    def write_register(self, address, value, slave=1):
        payload = struct.pack(">BBHH", slave, 0x06, address, int(value))
        request = payload + self._crc16(payload)
        buf = self._transact(request, 8)
        if buf is None:
            return _RtuError(f"No response (write reg addr={address})")
        if self._crc16(buf[:-2]) != bytes(buf[-2:]):
            return _RtuError(f"CRC error (write reg addr={address})")
        return _RtuResult(None)

    def connect(self):
        try:
            with self._open_serial():
                self._connected = True
                return True
        except Exception:
            return False

    def close(self):
        self._connected = False


class _RtuResult:
    def __init__(self, data):
        self.registers = data if isinstance(data, list) else []
        self.bits = data if isinstance(data, list) else []
        self._data = data

    def isError(self):
        return False


class _RtuError:
    def __init__(self, msg):
        self._msg = msg

    def isError(self):
        return True

    def __str__(self):
        return self._msg

    def __repr__(self):
        return f"RtuError({self._msg})"



class SnifferClient:
    """Cliente Modbus RTU passivo — escuta o barramento RS485 sem transmitir.

    Uma thread daemon decodifica frames FC01/FC03 (request+response) do trafego
    existente e armazena os ultimos valores em cache por (slave, endereco).
    Leituras retornam do cache. Escritas delegam ao RawRtuClient interno.
    """

    def __init__(self, port, baudrate=19200, parity='N', stopbits=1,
                 bytesize=8, unit_id=1, stale_timeout=10.0):
        self._port = port
        self._baudrate = baudrate
        self._parity = parity
        self._stopbits = stopbits
        self._bytesize = bytesize
        self._unit_id = unit_id
        self._stale_timeout = stale_timeout

        # Cache: {(key_prefix, slave, addr): {'value': val, 'ts': float}}
        self._cache = {}
        self._cache_lock = threading.Lock()

        self._running = False
        self._thread = None

        # RawRtuClient para escritas ativas
        self._rtu_writer = RawRtuClient(
            port=port, baudrate=baudrate, parity=parity,
            stopbits=stopbits, bytesize=bytesize,
        )

    def connect(self):
        """Inicia a thread de escuta passiva."""
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._listener_loop, daemon=True)
        self._thread.start()
        logger.info("SnifferClient: escuta passiva iniciada em %s @%s",
                     self._port, self._baudrate)
        return True

    def close(self):
        """Para a thread de escuta."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    # -- Listener thread --

    def _listener_loop(self):
        """Loop principal: coleta frames do barramento e decodifica."""
        import serial as _serial
        inter_frame_gap = 0.003  # 3ms a 19200 baud

        while self._running:
            try:
                with _serial.Serial(
                    port=self._port, baudrate=self._baudrate,
                    parity=self._parity, stopbits=self._stopbits,
                    bytesize=self._bytesize, timeout=inter_frame_gap,
                ) as ser:
                    ser.reset_input_buffer()
                    last_req = None
                    while self._running:
                        frame = self._collect_frame(ser, inter_frame_gap)
                        if not frame:
                            continue
                        decoded = self._try_decode(frame)
                        if decoded is None:
                            # Tenta fatiar frames colados
                            for start in range(len(frame) - 7):
                                sub = frame[start:start+8]
                                d = self._decode_request(sub)
                                if d:
                                    last_req = d
                            continue
                        if decoded['type'] == 'REQ':
                            last_req = decoded
                        elif decoded['type'] == 'RSP' and last_req:
                            if last_req['slave'] == decoded['slave']:
                                self._update_cache(decoded['slave'],
                                                   last_req['addr'],
                                                   decoded['values'],
                                                   last_req['fc'])
                            last_req = None
            except Exception as exc:
                logger.error("SnifferClient: erro no listener: %s", exc)
                if self._running:
                    time.sleep(1)

    @staticmethod
    def _collect_frame(ser, gap):
        buf = b""
        ser.timeout = gap
        while True:
            chunk = ser.read(256)
            if not chunk:
                break
            buf += chunk
        return buf

    @staticmethod
    def _crc16(data: bytes) -> bytes:
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        return struct.pack("<H", crc)

    @classmethod
    def _crc_ok(cls, frame: bytes) -> bool:
        if len(frame) < 4:
            return False
        return cls._crc16(frame[:-2]) == bytes(frame[-2:])

    @classmethod
    def _decode_request(cls, frame: bytes):
        """Decodifica request FC01 ou FC03 (8 bytes fixos)."""
        if len(frame) != 8:
            return None
        fc = frame[1]
        if fc not in (0x01, 0x03):
            return None
        if not cls._crc_ok(frame):
            return None
        return {
            'type':  'REQ',
            'slave': frame[0],
            'fc':    fc,
            'addr':  (frame[2] << 8) | frame[3],
            'count': (frame[4] << 8) | frame[5],
        }

    @classmethod
    def _decode_response(cls, frame: bytes):
        """Decodifica response FC01 ou FC03."""
        if len(frame) < 5:
            return None
        fc = frame[1]
        if fc not in (0x01, 0x03):
            return None
        byte_count = frame[2]
        if len(frame) != 3 + byte_count + 2:
            return None
        if not cls._crc_ok(frame):
            return None
        if fc == 0x03:
            values = [
                (frame[3 + i*2] << 8) | frame[3 + i*2 + 1]
                for i in range(byte_count // 2)
            ]
        else:  # FC01 - coils
            values = []
            for i in range(byte_count * 8):
                byte_idx = 3 + i // 8
                bit_idx = i % 8
                values.append(bool((frame[byte_idx] >> bit_idx) & 1))
        return {
            'type':   'RSP',
            'slave':  frame[0],
            'fc':     fc,
            'values': values,
        }

    @classmethod
    def _try_decode(cls, frame: bytes):
        d = cls._decode_request(frame)
        if d:
            return d
        return cls._decode_response(frame)

    def _update_cache(self, slave, base_addr, values, fc):
        """Atualiza o cache com os valores decodificados."""
        key_prefix = 'coil' if fc == 0x01 else 'reg'
        now = time.monotonic()
        with self._cache_lock:
            for i, val in enumerate(values):
                cache_key = (key_prefix, slave, base_addr + i)
                self._cache[cache_key] = {'value': val, 'ts': now}

    # -- Interface publica (mesma que ModbusClientWrapper) --

    def read_holding_registers(self, address, count, slave=None):
        if slave is None:
            slave = self._unit_id
        now = time.monotonic()
        regs = []
        missing = []
        with self._cache_lock:
            for i in range(count):
                entry = self._cache.get(('reg', slave, address + i))
                if entry is None or (now - entry['ts'] > self._stale_timeout):
                    missing.append(i)
                else:
                    regs.append((i, entry['value']))

        # If we have missing registers, try active read via RawRtuClient
        if missing:
            logger.debug("SnifferClient: %d regs missing, active fill addr=0x%04X count=%d slave=%d",
                         len(missing), address, count, slave)
            active_result = self._rtu_writer.read_holding_registers(address, count, slave=slave)
            if hasattr(active_result, 'registers'):
                # Update cache with active read results
                now2 = time.monotonic()
                with self._cache_lock:
                    for i, val in enumerate(active_result.registers):
                        self._cache[('reg', slave, address + i)] = {'value': val, 'ts': now2}
                return active_result
            else:
                # Active read also failed — return what we have from cache or error
                if not regs:
                    return _RtuError(
                        "Sniff: sem dados em cache e leitura ativa falhou para reg %d slave %d" % (
                            address + missing[0], slave))
                # Return partial from cache (fill missing with 0)
                logger.warning("SnifferClient: leitura ativa falhou, retornando cache parcial")
                result_regs = [0] * count
                for i, val in regs:
                    result_regs[i] = val
                return _RtuResult(result_regs)

        return _RtuResult([val for _, val in sorted(regs)])

    def read_coils(self, address, count, slave=None):
        if slave is None:
            slave = self._unit_id
        now = time.monotonic()
        bits = []
        missing = []
        with self._cache_lock:
            for i in range(count):
                entry = self._cache.get(('coil', slave, address + i))
                if entry is None or (now - entry['ts'] > self._stale_timeout):
                    missing.append(i)
                else:
                    bits.append((i, entry['value']))

        if missing:
            logger.debug("SnifferClient: %d coils missing, active fill addr=0x%04X count=%d slave=%d",
                         len(missing), address, count, slave)
            active_result = self._rtu_writer.read_coils(address, count, slave=slave)
            if hasattr(active_result, 'bits'):
                now2 = time.monotonic()
                with self._cache_lock:
                    for i, val in enumerate(active_result.bits):
                        self._cache[('coil', slave, address + i)] = {'value': val, 'ts': now2}
                return active_result
            elif not bits:
                return _RtuError(
                    "Sniff: sem dados em cache e leitura ativa falhou para coil %d slave %d" % (
                        address + missing[0], slave))

        return _RtuResult([val for _, val in sorted(bits)])

    def write_coil(self, address, value, slave=None):
        if slave is None:
            slave = self._unit_id
        return self._rtu_writer.write_coil(address, value, slave=slave)

    def write_register(self, address, value, slave=None):
        if slave is None:
            slave = self._unit_id
        return self._rtu_writer.write_register(address, value, slave=slave)


class ModbusClientWrapper:
    """Wrapper que unifica a API de pyModbusTCP (TCP puro) e pymodbus (RTU over TCP)."""

    def __init__(self, client, protocol, unit_id):
        self._client = client
        self._protocol = protocol
        self._unit_id = unit_id

    def read_coils(self, address, count, slave=None):
        if self._protocol == 'tcp':
            return self._client.read_coils(address, count)
        unit = slave if slave is not None else self._unit_id
        result = self._client.read_coils(address, count, slave=unit)
        if result.isError():
            raise Exception(f"Modbus RTU error (read_coils addr={address} slave={unit}): {result}")
        return result.bits[:count]

    def read_holding_registers(self, address, count, slave=None):
        if self._protocol == 'tcp':
            return self._client.read_holding_registers(address, count)
        unit = slave if slave is not None else self._unit_id
        result = self._client.read_holding_registers(address, count, slave=unit)
        if result.isError():
            raise Exception(f"Modbus RTU error (read_holding_registers addr={address} slave={unit}): {result}")
        return result.registers[:count]

    def write_single_coil(self, address, value, slave=None):
        if self._protocol == 'tcp':
            return self._client.write_single_coil(address, value)
        unit = slave if slave is not None else self._unit_id
        result = self._client.write_coil(address, bool(value), slave=unit)
        if result.isError():
            raise Exception(f"Modbus RTU error (write_coil addr={address} slave={unit}): {result}")
        return result

    def write_single_register(self, address, value, slave=None):
        if self._protocol == 'tcp':
            return self._client.write_single_register(address, value)
        unit = slave if slave is not None else self._unit_id
        result = self._client.write_register(address, int(value), slave=unit)
        if result.isError():
            raise Exception(f"Modbus RTU error (write_register addr={address} slave={unit}): {result}")
        return result

    def open(self):
        if self._protocol == 'tcp':
            return self._client.open()
        return self._client.connect()

    def close(self):
        return self._client.close()


def setup_modbus(protocol=None):
    if protocol is None:
        protocol = _MODBUS_PROTOCOL

    host = _MODBUS_HOST
    port = _MODBUS_PORT
    unit_id = _MODBUS_UNIT_ID
    attempts = 3
    delay = 1

    for _ in range(attempts):
        try:
            if protocol == 'rtu':
                serial_port = _SERIAL_PORT
                if not serial_port:
                    raise ValueError("SERIAL_PORT env var obrigatoria para protocolo RTU")
                raw_client = RawRtuClient(
                    port=serial_port,
                    baudrate=_SERIAL_BAUDRATE,
                    parity=_SERIAL_PARITY,
                    stopbits=_SERIAL_STOPBITS,
                    bytesize=8,
                )
                if not raw_client.connect():
                    raise ConnectionError(f"Falha ao conectar via RTU serial em {serial_port}")
                client = ModbusClientWrapper(raw_client, 'rtu', unit_id)
                logger.info("Modbus RTU serial conectado em %s @%s (unit_id=%s)",
                            serial_port, _SERIAL_BAUDRATE, unit_id)
            elif protocol == 'sniff':
                serial_port = _SERIAL_PORT
                if not serial_port:
                    raise ValueError("SERIAL_PORT env var obrigatoria para protocolo sniff")
                raw_client = SnifferClient(
                    port=serial_port,
                    baudrate=_SERIAL_BAUDRATE,
                    parity=_SERIAL_PARITY,
                    stopbits=_SERIAL_STOPBITS,
                    unit_id=unit_id,
                )
                if not raw_client.connect():
                    raise ConnectionError("Falha ao iniciar sniffer em %s" % serial_port)
                client = ModbusClientWrapper(raw_client, 'sniff', unit_id)
                logger.info("Modbus Sniffer passivo iniciado em %s @%s (unit_id=%s)",
                            serial_port, _SERIAL_BAUDRATE, unit_id)
            elif protocol == 'rtu_tcp':
                from pymodbus.client import ModbusTcpClient
                from pymodbus.framer import ModbusRtuFramer
                raw_client = ModbusTcpClient(host, port=port, framer=ModbusRtuFramer)
                connected = raw_client.connect()
                if not connected:
                    raise ConnectionError(f"Falha ao conectar via RTU over TCP em {host}:{port}")
                client = ModbusClientWrapper(raw_client, 'rtu_tcp', unit_id)
                logger.info("Modbus RTU over TCP conectado em %s:%s (unit_id=%s)", host, port, unit_id)
            else:
                from pyModbusTCP.client import ModbusClient
                raw_client = ModbusClient(host, port, unit_id=unit_id, auto_open=True)
                raw_client.open()
                client = ModbusClientWrapper(raw_client, 'tcp', unit_id)
                logger.info("Modbus TCP conectado em %s:%s (unit_id=%s)", host, port, unit_id)
            return client
        except Exception as e:
            logger.error("Erro ao configurar Modbus: %s, nova tentativa em %ss.", e, delay)
            sleep(delay)

    logger.critical("Falha ao configurar Modbus apos %s tentativas.", attempts)
    return None

def read_coils(client, groups, tags, keys, coil_slaves=None):
    devices_data = defaultdict(dict)
    total_coils_read = 0

    for idx, (group, tags, keys) in enumerate(zip(groups, tags, keys)):
        slave = coil_slaves[idx] if coil_slaves else None
        try:
            first_address = group[0]
            num_addresses = len(group)
            result = client.read_coils(first_address, num_addresses, slave=slave)

            for key, tag, value in zip(keys, tags, result):
                devices_data[key][tag] = bool(value)

            total_coils_read += num_addresses
        except Exception as e:
            logger.error("Erro ao ler bobinas no endereço %s (slave=%s): %s",
                         group[0] if group else '?', slave, e)

    return devices_data, total_coils_read


def read_registers(client, groups, tags, keys, group_slaves=None):
    devices_data = defaultdict(dict)
    total_registers_read = 0

    for idx, (group, tags, keys) in enumerate(zip(groups, tags, keys)):
        slave = group_slaves[idx] if group_slaves else None
        try:
            first_address = group[0]
            num_addresses = len(group)
            result = client.read_holding_registers(first_address, num_addresses, slave=slave)

            for key, tag, value in zip(keys, tags, result):
                devices_data[key][tag] = value

            total_registers_read += num_addresses
        except Exception as e:
            logger.error("Erro ao ler registros no endereço %s (slave=%s): %s",
                         group[0] if group else '?', slave, e)

    return devices_data, total_registers_read


def read_registers_with_bits(client, groups, tags, keys, bit_vars=None, group_slaves=None):
    """Lê holding registers e extrai bits quando bit_vars indica.

    bit_vars: {register_addr: [{'tag': str, 'key': str, 'bit': int}, ...]}
    group_slaves: lista de unit_id por grupo (mesma ordem que groups).
    Quando um endereço está em bit_vars, o valor do registrador é decomposto
    em bits individuais. Caso contrário, comportamento idêntico a read_registers().
    """
    devices_data = defaultdict(dict)
    total_registers_read = 0

    for idx, (group, g_tags, g_keys) in enumerate(zip(groups, tags, keys)):
        slave = group_slaves[idx] if group_slaves else None
        try:
            first_address = group[0]
            num_addresses = len(group)
            result = client.read_holding_registers(first_address, num_addresses, slave=slave)

            for addr, key, tag, value in zip(group, g_keys, g_tags, result):
                if bit_vars and addr in bit_vars:
                    for bv in bit_vars[addr]:
                        devices_data[bv['key']][bv['tag']] = bool((value >> bv['bit']) & 1)
                else:
                    devices_data[key][tag] = value

            total_registers_read += num_addresses
        except Exception as e:
            logger.error("Erro ao ler registros no endereço %s (slave=%s): %s",
                         group[0] if group else '?', slave, e)

    return devices_data, total_registers_read


def write_coils_to_device(client, modbus, values, slaves=None):
    attempts = 3
    delay = 0.2

    for i, (address, value) in enumerate(zip(modbus, values)):
        slave = slaves[i] if slaves else None
        for _ in range(attempts):
            try:
                client.write_single_coil(address, int(value), slave=slave)
                break
            except Exception as e:
                logger.error("Erro ao escrever coil no endereço %s (slave=%s): %s, nova tentativa em %ss.",
                             address, slave, e, delay)
                sleep(delay)
        else:
            logger.error("Falha ao escrever coil no endereço %s após %s tentativas.", address, attempts)


def write_registers_to_device(client, modbus, values, slaves=None):
    attempts = 3
    delay = 0.2

    for i, (address, value) in enumerate(zip(modbus, values)):
        slave = slaves[i] if slaves else None
        for _ in range(attempts):
            try:
                client.write_single_register(address, int(value), slave=slave)
                break
            except Exception as e:
                logger.error("Erro ao escrever registro no endereço %s (slave=%s): %s, nova tentativa em %ss.",
                             address, slave, e, delay)
                sleep(delay)
        else:
            logger.error("Falha ao escrever registro no endereço %s após %s tentativas.", address, attempts)
