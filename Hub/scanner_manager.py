"""
scanner_manager — Scanner de variáveis Modbus.

Lê variáveis de um device uma a uma, registra quais retornam valor válido
e quais dão erro, salva os resultados e emite progresso via callback.
Permite desabilitar variáveis problemáticas antes de colocar em produção.
"""

import asyncio
import datetime
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger("scanner_manager")

_thread_pool = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# ScanSession
# ---------------------------------------------------------------------------

@dataclass
class ScanSession:
    """Estado de uma sessão de scan."""

    device_id: str
    status: str = 'running'       # running | completed | cancelled | error
    config: dict = field(default_factory=dict)
    total: int = 0
    scanned: int = 0
    ok_count: int = 0
    error_count: int = 0
    results: list[dict] = field(default_factory=list)
    started_at: str = ''
    finished_at: str = ''

    _cancelled: bool = field(default=False, repr=False)

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def to_dict(self) -> dict:
        return {
            'device_id':   self.device_id,
            'status':      self.status,
            'config':      self.config,
            'total':       self.total,
            'scanned':     self.scanned,
            'ok_count':    self.ok_count,
            'error_count': self.error_count,
            'results':     self.results,
            'started_at':  self.started_at,
            'finished_at': self.finished_at,
        }

    def to_summary(self) -> dict:
        return {
            'device_id':   self.device_id,
            'status':      self.status,
            'total':       self.total,
            'scanned':     self.scanned,
            'ok_count':    self.ok_count,
            'error_count': self.error_count,
            'started_at':  self.started_at,
            'finished_at': self.finished_at,
        }


# ---------------------------------------------------------------------------
# Modbus single-variable scan (blocking — runs in thread pool)
# ---------------------------------------------------------------------------

def _create_modbus_client(device_cfg: dict):
    """Cria um ModbusClientWrapper a partir da config do device. Bloqueante."""
    protocol = device_cfg.get('protocol', 'tcp')
    host = device_cfg.get('host', 'localhost')
    port = device_cfg.get('port', 502)
    unit_id = device_cfg.get('unit_id', 1)

    if protocol == 'rtu_tcp':
        from pymodbus.client import ModbusTcpClient
        from pymodbus.framer import ModbusRtuFramer
        raw_client = ModbusTcpClient(host, port=port, framer=ModbusRtuFramer)
        connected = raw_client.connect()
        if not connected:
            raise ConnectionError(f"Falha ao conectar via RTU over TCP em {host}:{port}")
        from shared.modbus_functions import ModbusClientWrapper
        return ModbusClientWrapper(raw_client, 'rtu_tcp', unit_id)
    else:
        from pyModbusTCP.client import ModbusClient
        raw_client = ModbusClient(host, port, unit_id=unit_id, auto_open=True, timeout=3)
        if not raw_client.open():
            raise ConnectionError(f"Falha ao conectar via TCP em {host}:{port}")
        from shared.modbus_functions import ModbusClientWrapper
        return ModbusClientWrapper(raw_client, 'tcp', unit_id)


def _scan_single_variable(client, var: dict, retries: int) -> dict:
    """
    Lê uma única variável via Modbus. Bloqueante.

    Suporta variáveis bit-addressed: quando var['bit_index'] não é None,
    lê como holding register e extrai o bit correspondente.

    Retorna dict com status/value/latency/error/retries_used.
    """
    tag = var['tag']
    address = var['address']
    var_type = var.get('type', '')
    bit_index = var.get('bit_index')

    # Variáveis bit-addressed sempre lêem como register
    if bit_index is not None:
        is_coil = False
    else:
        is_coil = var_type == '%MB' or var.get('tipo') == 'M'

    last_error = None
    for attempt in range(1, retries + 1):
        t0 = time.monotonic()
        try:
            if is_coil:
                result = client.read_coils(address, 1)
            else:
                result = client.read_holding_registers(address, 1)

            latency = round((time.monotonic() - t0) * 1000, 2)

            if result is None:
                last_error = 'Sem resposta do device'
                continue

            value = result[0] if result else None

            if bit_index is not None and value is not None:
                # Extrai bit do valor do registrador
                value = bool((value >> bit_index) & 1)
            elif is_coil:
                value = bool(value) if value is not None else None

            return {
                'tag':          tag,
                'address':      address,
                'type':         var_type,
                'bit_index':    bit_index,
                'status':       'ok',
                'value':        value,
                'latency_ms':   latency,
                'error':        None,
                'retries_used': attempt,
                'timestamp':    datetime.datetime.now().isoformat(),
            }

        except Exception as exc:
            latency = round((time.monotonic() - t0) * 1000, 2)
            last_error = str(exc)

    return {
        'tag':          tag,
        'address':      address,
        'type':         var_type,
        'bit_index':    bit_index,
        'status':       'error',
        'value':        None,
        'latency_ms':   latency,
        'error':        last_error,
        'retries_used': retries,
        'timestamp':    datetime.datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Async scan orchestrator
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[str, dict], Coroutine[Any, Any, None]]


async def _run_scan(
    session: ScanSession,
    device_cfg: dict,
    variables: list[dict],
    progress_callback: ProgressCallback | None,
) -> None:
    """Orquestra o scan completo de um device."""
    loop = asyncio.get_event_loop()
    interval_s = session.config.get('interval_ms', 50) / 1000.0
    retries = session.config.get('retries', 3)

    client = None
    try:
        client = await loop.run_in_executor(_thread_pool, _create_modbus_client, device_cfg)
    except Exception as exc:
        session.status = 'error'
        session.finished_at = datetime.datetime.now().isoformat()
        logger.error("[scan:%s] Falha ao conectar: %s", session.device_id, exc)
        if progress_callback:
            await progress_callback('scan:complete', {
                'device_id': session.device_id,
                'status': 'error',
                'error': str(exc),
            })
        return

    try:
        for var in variables:
            if session.is_cancelled:
                session.status = 'cancelled'
                break

            result = await loop.run_in_executor(
                _thread_pool, _scan_single_variable, client, var, retries,
            )
            session.results.append(result)
            session.scanned += 1
            if result['status'] == 'ok':
                session.ok_count += 1
            else:
                session.error_count += 1

            if progress_callback:
                await progress_callback('scan:variable', {
                    'device_id': session.device_id,
                    'result':    result,
                    'scanned':   session.scanned,
                    'total':     session.total,
                    'ok_count':  session.ok_count,
                    'error_count': session.error_count,
                })

            if interval_s > 0:
                await asyncio.sleep(interval_s)

        if session.status == 'running':
            session.status = 'completed'

    except Exception as exc:
        session.status = 'error'
        logger.error("[scan:%s] Erro durante scan: %s", session.device_id, exc)

    finally:
        session.finished_at = datetime.datetime.now().isoformat()
        try:
            client.close()
        except Exception:
            pass

        if progress_callback:
            await progress_callback('scan:complete', {
                'device_id':   session.device_id,
                'status':      session.status,
                'total':       session.total,
                'scanned':     session.scanned,
                'ok_count':    session.ok_count,
                'error_count': session.error_count,
            })

    logger.info(
        "[scan:%s] Finalizado: status=%s, total=%d, ok=%d, erro=%d",
        session.device_id, session.status, session.total,
        session.ok_count, session.error_count,
    )


# ---------------------------------------------------------------------------
# ScannerManager
# ---------------------------------------------------------------------------

class ScannerManager:
    """Gerencia sessões de scan por device."""

    def __init__(self, tables_dir: str):
        self._tables_dir = tables_dir
        self._scans: dict[str, ScanSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def start_scan(
        self,
        device_id: str,
        device_cfg: dict,
        variables: list[dict],
        config: dict,
        progress_callback: ProgressCallback | None = None,
    ) -> ScanSession:
        """Inicia uma sessão de scan para um device."""
        existing = self._scans.get(device_id)
        if existing and existing.status == 'running':
            raise RuntimeError(f"Scan já em andamento para device '{device_id}'.")

        session = ScanSession(
            device_id=device_id,
            config=config,
            total=len(variables),
            started_at=datetime.datetime.now().isoformat(),
        )
        self._scans[device_id] = session

        task = asyncio.create_task(
            _run_scan(session, device_cfg, variables, progress_callback)
        )
        task.add_done_callback(lambda t: self._on_scan_done(device_id, t))
        self._tasks[device_id] = task

        logger.info("[scan:%s] Scan iniciado: %d variáveis", device_id, len(variables))
        return session

    def _on_scan_done(self, device_id: str, task: asyncio.Task) -> None:
        """Callback quando a task de scan termina — persiste resultados."""
        self._tasks.pop(device_id, None)
        session = self._scans.get(device_id)
        if session:
            self._save_results(session)

    def cancel_scan(self, device_id: str) -> ScanSession | None:
        """Cancela um scan em andamento."""
        session = self._scans.get(device_id)
        if not session or session.status != 'running':
            return session
        session.cancel()
        task = self._tasks.get(device_id)
        if task and not task.done():
            task.cancel()
        return session

    def get_scan(self, device_id: str) -> ScanSession | None:
        return self._scans.get(device_id)

    def is_scanning(self, device_id: str) -> bool:
        session = self._scans.get(device_id)
        return session is not None and session.status == 'running'

    def get_results_for_grid(self, device_id: str) -> dict:
        """Retorna {tag: {status, latency_ms, error, value}} para o AG Grid."""
        session = self._scans.get(device_id)
        if not session:
            return {}
        result = {}
        for r in session.results:
            result[r['tag']] = {
                'status':     r['status'],
                'latency_ms': r['latency_ms'],
                'error':      r['error'],
                'value':      r['value'],
            }
        return result

    # -- Persistência --

    def _results_path(self, device_id: str) -> str:
        return os.path.join(self._tables_dir, f'scan_results_{device_id}.json')

    def _save_results(self, session: ScanSession) -> None:
        path = self._results_path(session.device_id)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(session.to_dict(), f, indent=2, ensure_ascii=False)
            logger.info("[scan:%s] Resultados salvos em %s", session.device_id, path)
        except Exception as exc:
            logger.error("[scan:%s] Erro ao salvar resultados: %s", session.device_id, exc)

    def load_cached_results(self, device_id: str) -> ScanSession | None:
        """Carrega resultados anteriores do disco."""
        path = self._results_path(device_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            session = ScanSession(
                device_id=data.get('device_id', device_id),
                status=data.get('status', 'completed'),
                config=data.get('config', {}),
                total=data.get('total', 0),
                scanned=data.get('scanned', 0),
                ok_count=data.get('ok_count', 0),
                error_count=data.get('error_count', 0),
                results=data.get('results', []),
                started_at=data.get('started_at', ''),
                finished_at=data.get('finished_at', ''),
            )
            self._scans[device_id] = session
            return session
        except Exception as exc:
            logger.error("[scan:%s] Erro ao carregar cache: %s", device_id, exc)
            return None

    def load_all_cached(self) -> None:
        """Carrega todos os scan_results_*.json do diretório de tabelas."""
        if not os.path.exists(self._tables_dir):
            return
        prefix = 'scan_results_'
        suffix = '.json'
        for fname in os.listdir(self._tables_dir):
            if fname.startswith(prefix) and fname.endswith(suffix):
                device_id = fname[len(prefix):-len(suffix)]
                self.load_cached_results(device_id)
                logger.info("Cache de scan carregado para device '%s'.", device_id)

    async def shutdown(self) -> None:
        """Cancela todos os scans em andamento."""
        for device_id in list(self._tasks):
            self.cancel_scan(device_id)
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._tasks.clear()
