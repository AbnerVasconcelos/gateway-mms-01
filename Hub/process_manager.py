"""
process_manager — Gerencia subprocessos Delfos e Atena.

Lanca cada processo como subprocess OS via asyncio.create_subprocess_exec(),
com env vars passadas programaticamente (MODBUS_HOST, MODBUS_PORT, etc.).
Captura stdout/stderr e detecta saida automaticamente.
"""

import asyncio
import datetime
import logging
import os
import sys
from typing import Callable, Awaitable

logger = logging.getLogger("process_manager")

MAX_LOG_LINES = 200


class ProcessInstance:
    """Encapsula um subprocess OS (Delfos ou Atena) com log capture."""

    def __init__(self, proc_id: str, proc_type: str, config: dict, device_id: str = ''):
        self.proc_id = proc_id
        self.proc_type = proc_type          # "delfos" ou "atena"
        self.config = dict(config)           # modbus_host, port, unit_id, redis_*, tables_dir
        self.device_id = device_id           # ID do device associado a este processo
        self.process: asyncio.subprocess.Process | None = None
        self.running = False
        self.exit_code: int | None = None
        self.started_at: str | None = None
        self.stopped_at: str | None = None
        self._log_lines: list[str] = []
        self._monitor_task: asyncio.Task | None = None

    async def start(self, python_path: str, gateway_dir: str) -> None:
        """Inicia o subprocesso com env vars derivadas do config."""
        if self.running:
            logger.warning("[%s] Ja esta rodando.", self.proc_id)
            return

        # Monta env herdando os.environ + sobrescrevendo campos Modbus/Redis
        env = dict(os.environ)
        env['MODBUS_HOST'] = str(self.config.get('modbus_host', ''))
        env['MODBUS_PORT'] = str(self.config.get('modbus_port', 502))
        env['MODBUS_UNIT_ID'] = str(self.config.get('modbus_unit_id', 1))
        env['MODBUS_PROTOCOL'] = str(self.config.get('modbus_protocol', 'tcp'))
        env['REDIS_HOST'] = str(self.config.get('redis_host', 'localhost'))
        env['REDIS_PORT'] = str(self.config.get('redis_port', 6379))
        env['TABLES_DIR'] = str(self.config.get('tables_dir', os.path.join(gateway_dir, 'tables')))

        # Per-device isolation env vars
        env['DEVICE_ID'] = str(self.device_id)
        env['COMMAND_CHANNEL'] = str(self.config.get('command_channel', f'{self.device_id}_commands'))
        env['CONFIG_RELOAD_CHANNEL'] = str(self.config.get('config_reload_channel', f'config_reload_{self.device_id}'))

        # Script e cwd
        if self.proc_type == 'delfos':
            script = os.path.join(gateway_dir, 'Delfos', 'delfos.py')
            cwd = os.path.join(gateway_dir, 'Delfos')
        else:
            script = os.path.join(gateway_dir, 'Atena', 'atena.py')
            cwd = os.path.join(gateway_dir, 'Atena')

        if not os.path.exists(script):
            raise FileNotFoundError(f"Script nao encontrado: {script}")

        self._log_lines.clear()
        self.exit_code = None
        self.stopped_at = None

        self.process = await asyncio.create_subprocess_exec(
            python_path, script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=cwd,
        )

        self.running = True
        self.started_at = datetime.datetime.now().isoformat()
        self._monitor_task = asyncio.create_task(self._monitor())

        logger.info(
            "[%s] Processo %s iniciado (PID %d).",
            self.proc_id, self.proc_type, self.process.pid,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """Para o processo: terminate, espera, fallback kill."""
        if not self.running or not self.process:
            return

        try:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        except ProcessLookupError:
            pass

        self.running = False
        self.exit_code = self.process.returncode
        self.stopped_at = datetime.datetime.now().isoformat()

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        logger.info("[%s] Processo parado (exit_code=%s).", self.proc_id, self.exit_code)

    async def _monitor(self) -> None:
        """Le stdout linha a linha e detecta exit."""
        try:
            while self.process and self.process.stdout:
                line = await self.process.stdout.readline()
                if not line:
                    break
                decoded = line.decode('utf-8', errors='replace').rstrip('\n').rstrip('\r')
                self._log_lines.append(decoded)
                if len(self._log_lines) > MAX_LOG_LINES:
                    self._log_lines = self._log_lines[-MAX_LOG_LINES:]

            # Processo encerrou
            if self.process:
                await self.process.wait()
                self.exit_code = self.process.returncode
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("[%s] Erro no monitor: %s", self.proc_id, exc)
        finally:
            if self.running:
                self.running = False
                self.stopped_at = datetime.datetime.now().isoformat()
                logger.info(
                    "[%s] Processo encerrou sozinho (exit_code=%s).",
                    self.proc_id, self.exit_code,
                )

    def get_logs(self, last_n: int = 100) -> list[str]:
        """Retorna as ultimas N linhas de log."""
        return self._log_lines[-last_n:]

    def to_state_dict(self) -> dict:
        return {
            'proc_id': self.proc_id,
            'proc_type': self.proc_type,
            'device_id': self.device_id,
            'running': self.running,
            'exit_code': self.exit_code,
            'started_at': self.started_at,
            'stopped_at': self.stopped_at,
            'config': self.config,
            'log_count': len(self._log_lines),
        }


class ProcessManager:
    """Gerencia instancias de ProcessInstance para Delfos e Atena."""

    def __init__(self, gateway_dir: str):
        self._gateway_dir = gateway_dir
        self._python = self._detect_python()
        self._processes: dict[str, ProcessInstance] = {}
        self._status_callback: Callable[[dict], Awaitable[None]] | None = None
        logger.info("ProcessManager: python=%s, gateway=%s", self._python, self._gateway_dir)

    def _detect_python(self) -> str:
        """Detecta o interpretador Python do venv ou fallback sys.executable."""
        if sys.platform == 'win32':
            venv_python = os.path.join(self._gateway_dir, '.venv', 'Scripts', 'python.exe')
        else:
            venv_python = os.path.join(self._gateway_dir, '.venv', 'bin', 'python')
        if os.path.exists(venv_python):
            return venv_python
        return sys.executable

    def set_status_callback(self, fn: Callable[[dict], Awaitable[None]]) -> None:
        """Define callback async chamado em mudancas de status."""
        self._status_callback = fn

    async def _notify_status(self, proc: ProcessInstance) -> None:
        """Notifica via callback e checa se o processo morreu sozinho."""
        if self._status_callback:
            try:
                await self._status_callback(proc.to_state_dict())
            except Exception as exc:
                logger.error("Erro no status_callback: %s", exc)

    async def start_process(self, proc_type: str, device_id: str, config: dict) -> ProcessInstance:
        """Inicia um novo processo. proc_id e derivado como '{proc_type}:{device_id}'."""
        proc_id = f"{proc_type}:{device_id}"
        if proc_id in self._processes and self._processes[proc_id].running:
            raise RuntimeError(f"Processo '{proc_id}' ja esta rodando.")

        proc = ProcessInstance(proc_id, proc_type, config, device_id=device_id)
        await proc.start(self._python, self._gateway_dir)
        self._processes[proc_id] = proc

        # Inicia watcher para detectar crash
        asyncio.create_task(self._watch_exit(proc))

        await self._notify_status(proc)
        return proc

    async def stop_process(self, proc_id: str) -> ProcessInstance:
        """Para um processo pelo ID."""
        proc = self._processes.get(proc_id)
        if not proc:
            raise KeyError(f"Processo '{proc_id}' nao encontrado.")
        await proc.stop()
        await self._notify_status(proc)
        return proc

    async def _watch_exit(self, proc: ProcessInstance) -> None:
        """Espera o processo encerrar e notifica via callback."""
        if not proc.process:
            return
        try:
            await proc.process.wait()
        except Exception:
            pass
        # Garante que o estado esta atualizado
        if not proc.running:
            return
        proc.running = False
        proc.exit_code = proc.process.returncode if proc.process else None
        proc.stopped_at = datetime.datetime.now().isoformat()
        await self._notify_status(proc)

    def list_processes(self) -> dict:
        """Retorna {proc_id: state_dict}."""
        return {pid: p.to_state_dict() for pid, p in self._processes.items()}

    def get_process(self, proc_id: str) -> ProcessInstance | None:
        return self._processes.get(proc_id)

    async def shutdown_all(self) -> None:
        """Para todos os processos. Chamado no shutdown do Hub."""
        for proc_id, proc in self._processes.items():
            try:
                await proc.stop()
            except Exception as exc:
                logger.error("[%s] Erro ao parar no shutdown: %s", proc_id, exc)
        logger.info("Todos os processos parados.")
