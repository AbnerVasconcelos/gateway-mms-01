"""
simulator_manager — Gerencia simuladores Modbus embarcados no Hub.

Cada SimulatorInstance encapsula um ModbusTcpServer (pymodbus) com contexto
carregado dos CSVs do gateway. Suporta TCP e RTU over TCP (framer RTU
transportado via TCP).

Funcionalidades:
  - Criar/remover/iniciar/parar simuladores
  - Lock de tags (impede simulação automática, permite escrita manual)
  - Leitura em massa de valores atuais
  - Persistência da configuração em simulator_config.json
"""

import asyncio
import json
import logging
import math
import os
import random
import sys

import pandas as pd
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import ModbusTcpServer
from pymodbus.framer import ModbusRtuFramer, ModbusSocketFramer

logger = logging.getLogger("simulator_manager")

# ---------------------------------------------------------------------------
# Importa funções reutilizáveis de tests/modbus_simulator.py
# ---------------------------------------------------------------------------
_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tests')
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from modbus_simulator import (   # noqa: E402
    LoggingDataBlock,
    load_csv,
    _initial_register_value,
)


# ---------------------------------------------------------------------------
# SimulatorInstance
# ---------------------------------------------------------------------------

class SimulatorInstance:
    """Encapsula um servidor Modbus embarcado com simulação de valores."""

    def __init__(self, sim_id: str, config: dict):
        self.sim_id = sim_id
        self.config = dict(config)
        self.server: ModbusTcpServer | None = None
        self.context: ModbusServerContext | None = None
        self.running = False

        self._locked_tags: set[str] = set()
        self._variables: list[dict] = []       # [{tag, group, type, address}]
        self._reg_addresses: list[int] = []    # endereços de registers para simulate
        self._coil_addresses: list[int] = []   # endereços de coils para simulate
        self._tag_to_address: dict[str, dict] = {}  # tag → {address, fc}

        self._tasks: list[asyncio.Task] = []

    def build_context(self, tables_dir: str) -> None:
        """Carrega CSVs do config e monta ModbusServerContext."""
        all_coils: dict[int, int] = {}
        all_regs: dict[int, int] = {}
        self._variables = []
        self._tag_to_address = {}

        csv_files = self.config.get('csv_files', [])
        for fname in csv_files:
            path = os.path.join(tables_dir, fname)
            if not os.path.exists(path):
                logger.warning("CSV não encontrado: %s", path)
                continue

            coils, regs = load_csv(path)
            all_coils.update(coils)
            all_regs.update(regs)

            # Carrega metadados das variáveis para o grid
            try:
                df = pd.read_csv(path, skipinitialspace=True)
                df = df.dropna(subset=['Tipo', 'Modbus'])
                df['Modbus'] = pd.to_numeric(df['Modbus'], errors='coerce')
                df = df.dropna(subset=['Modbus'])
                df['Modbus'] = df['Modbus'].astype(int)

                for _, row in df.iterrows():
                    tag = str(row.get('ObjecTag', '')).strip()
                    group = str(row.get('key', '')).strip()
                    tipo = str(row['Tipo']).strip()
                    addr = int(row['Modbus'])
                    var_at = str(row.get('At', '')).strip()

                    # fc: 1 = coils, 3 = holding registers
                    fc = 1 if tipo == 'M' else 3
                    self._variables.append({
                        'tag': tag,
                        'group': group,
                        'type': var_at,
                        'address': addr,
                        'fc': fc,
                    })
                    self._tag_to_address[tag] = {'address': addr, 'fc': fc}
            except Exception as exc:
                logger.error("Erro ao ler metadados de %s: %s", path, exc)

            logger.info(
                "  [%s] %-22s → %3d coils, %3d registers",
                self.sim_id, fname, len(coils), len(regs),
            )

        # Fallback se nenhum CSV carregou dados
        if not all_coils:
            all_coils = {2150 + i: random.randint(0, 1) for i in range(30)}
        if not all_regs:
            all_regs = {39770 + i: 1500 + i * 10 for i in range(50)}

        max_coil = max(all_coils.keys())
        max_reg = max(all_regs.keys())

        coil_vals = [0] * (max_coil + 2)
        for addr, val in all_coils.items():
            coil_vals[addr] = val

        reg_vals = [0] * (max_reg + 2)
        for addr, val in all_regs.items():
            reg_vals[addr] = val

        slave = ModbusSlaveContext(
            co=LoggingDataBlock(0, coil_vals, f"{self.sim_id}/COIL"),
            hr=LoggingDataBlock(0, reg_vals, f"{self.sim_id}/HREG"),
            zero_mode=True,
        )
        self.context = ModbusServerContext(slaves=slave, single=True)
        self._reg_addresses = list(all_regs.keys())
        self._coil_addresses = list(all_coils.keys())

        logger.info(
            "[%s] Contexto pronto: %d coils, %d registers",
            self.sim_id, len(all_coils), len(all_regs),
        )

    async def start(self) -> None:
        """Inicia o servidor Modbus e a task de simulação."""
        if self.running:
            logger.warning("[%s] Já está rodando.", self.sim_id)
            return
        if not self.context:
            raise RuntimeError(f"Contexto não construído para '{self.sim_id}'. Chame build_context() primeiro.")

        port = self.config.get('port', 5020)
        protocol = self.config.get('protocol', 'tcp')
        unit_id = self.config.get('unit_id', 1)

        framer = ModbusRtuFramer if protocol == 'rtu_tcp' else ModbusSocketFramer

        self.server = ModbusTcpServer(
            context=self.context,
            address=('0.0.0.0', port),
            framer=framer,
        )

        # Task do servidor
        task_serve = asyncio.create_task(self.server.serve_forever())
        self._tasks.append(task_serve)

        # Task de simulação (se habilitada)
        if self.config.get('simulate', True):
            task_sim = asyncio.create_task(self._simulate_values())
            self._tasks.append(task_sim)

        self.running = True
        logger.info(
            "[%s] Servidor iniciado em 0.0.0.0:%d (%s, unit_id=%d)",
            self.sim_id, port, protocol, unit_id,
        )

    async def stop(self) -> None:
        """Para o servidor e cancela tasks."""
        if not self.running:
            return

        if self.server:
            await self.server.shutdown()
            self.server = None

        for task in self._tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        self.running = False
        logger.info("[%s] Servidor parado.", self.sim_id)

    def lock_tag(self, tag: str) -> None:
        """Trava tag — simulação não sobrescreve."""
        self._locked_tags.add(tag)
        logger.info("[%s] Tag '%s' travada.", self.sim_id, tag)

    def unlock_tag(self, tag: str) -> None:
        """Destrava tag — simulação volta a sobrescrever."""
        self._locked_tags.discard(tag)
        logger.info("[%s] Tag '%s' destravada.", self.sim_id, tag)

    def is_locked(self, tag: str) -> bool:
        return tag in self._locked_tags

    def write_value(self, tag: str, value) -> bool:
        """Escreve valor direto no data store do simulador."""
        if not self.context:
            return False
        info = self._tag_to_address.get(tag)
        if not info:
            logger.warning("[%s] Tag '%s' não encontrada.", self.sim_id, tag)
            return False

        slave = self.context[0x00]
        addr = info['address']
        fc = info['fc']

        if fc == 1:
            val = 1 if value else 0
            slave.setValues(fc, addr, [val])
        else:
            val = int(value) & 0xFFFF
            slave.setValues(fc, addr, [val])

        logger.info("[%s] Escrita manual: %s = %s (addr=%d, fc=%d)", self.sim_id, tag, val, addr, fc)
        return True

    def read_all_values(self) -> dict[str, int | bool]:
        """Lê todos os valores atuais do data store."""
        if not self.context:
            return {}

        slave = self.context[0x00]
        result = {}
        for var in self._variables:
            tag = var['tag']
            addr = var['address']
            fc = var['fc']
            try:
                vals = slave.getValues(fc, addr, 1)
                result[tag] = vals[0] if vals else 0
            except Exception:
                result[tag] = 0
        return result

    def get_variables_info(self) -> list[dict]:
        """Retorna lista de variáveis com valores atuais e estado de lock."""
        values = self.read_all_values()
        result = []
        for var in self._variables:
            tag = var['tag']
            result.append({
                'tag': tag,
                'group': var['group'],
                'type': var['type'],
                'address': var['address'],
                'value': values.get(tag, 0),
                'locked': tag in self._locked_tags,
            })
        return result

    def to_state_dict(self) -> dict:
        """Retorna representação do estado para a API."""
        return {
            'sim_id': self.sim_id,
            'running': self.running,
            'config': self.config,
            'variable_count': len(self._variables),
            'locked_count': len(self._locked_tags),
        }

    async def _simulate_values(self, interval: float = 2.0) -> None:
        """Varia valores de registers e coils, pulando tags travadas."""
        slave = self.context[0x00]
        step = 0

        # Monta mapa reverso: address → tag (para checar locks)
        addr_to_tag_reg: dict[int, str] = {}
        addr_to_tag_coil: dict[int, str] = {}
        for var in self._variables:
            if var['fc'] == 3:
                addr_to_tag_reg[var['address']] = var['tag']
            elif var['fc'] == 1:
                addr_to_tag_coil[var['address']] = var['tag']

        while True:
            await asyncio.sleep(interval)
            step += 1

            # Simula registers
            n_reg = min(8, len(self._reg_addresses))
            for addr in self._reg_addresses[:n_reg]:
                tag = addr_to_tag_reg.get(addr)
                if tag and tag in self._locked_tags:
                    continue
                try:
                    vals = slave.getValues(3, addr, 1)
                    current = vals[0] if vals else 1000
                    swing = int(math.sin(step * 0.2) * max(current * 0.04, 10))
                    noise = random.randint(-3, 3)
                    new_val = max(0, min(65535, current + swing + noise))
                    slave.setValues(3, addr, [new_val])
                except Exception:
                    pass

            # Simula coils (toggle aleatório com baixa probabilidade)
            if step % 5 == 0:
                for addr in self._coil_addresses[:6]:
                    tag = addr_to_tag_coil.get(addr)
                    if tag and tag in self._locked_tags:
                        continue
                    if random.random() < 0.1:
                        try:
                            vals = slave.getValues(1, addr, 1)
                            current = vals[0] if vals else 0
                            slave.setValues(1, addr, [1 - current])
                        except Exception:
                            pass


# ---------------------------------------------------------------------------
# SimulatorManager
# ---------------------------------------------------------------------------

class SimulatorManager:
    """Gerencia múltiplos SimulatorInstance com persistência em JSON."""

    def __init__(self, tables_dir: str, config_path: str | None = None):
        self._tables_dir = tables_dir
        self._config_path = config_path or os.path.join(tables_dir, 'simulator_config.json')
        self._simulators: dict[str, SimulatorInstance] = {}

    def load_config(self) -> dict:
        """Carrega simulator_config.json. Retorna {} se ausente."""
        if not os.path.exists(self._config_path):
            return {}
        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as exc:
            logger.error("Erro ao carregar %s: %s", self._config_path, exc)
            return {}

    def save_config(self) -> None:
        """Persiste configurações de todos os simuladores."""
        data = {}
        for sim_id, sim in self._simulators.items():
            data[sim_id] = sim.config
        with open(self._config_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("simulator_config.json salvo com %d simulador(es).", len(data))

    async def init_from_config(self) -> None:
        """Carrega config e cria instâncias (sem iniciar). Auto-start se configurado."""
        config = self.load_config()
        for sim_id, cfg in config.items():
            sim = SimulatorInstance(sim_id, cfg)
            try:
                sim.build_context(self._tables_dir)
            except Exception as exc:
                logger.error("[%s] Erro ao construir contexto: %s", sim_id, exc)
            self._simulators[sim_id] = sim

            if cfg.get('auto_start', False):
                try:
                    await sim.start()
                    logger.info("[%s] Auto-start concluído.", sim_id)
                except Exception as exc:
                    logger.error("[%s] Falha no auto-start: %s", sim_id, exc)

    def create_simulator(self, sim_id: str, config: dict) -> SimulatorInstance:
        """Cria um novo simulador e persiste."""
        if sim_id in self._simulators:
            raise ValueError(f"Simulador '{sim_id}' já existe.")
        sim = SimulatorInstance(sim_id, config)
        sim.build_context(self._tables_dir)
        self._simulators[sim_id] = sim
        self.save_config()
        return sim

    async def delete_simulator(self, sim_id: str) -> None:
        """Para e remove um simulador."""
        sim = self._simulators.get(sim_id)
        if not sim:
            raise KeyError(f"Simulador '{sim_id}' não encontrado.")
        await sim.stop()
        del self._simulators[sim_id]
        self.save_config()

    async def start_simulator(self, sim_id: str) -> None:
        sim = self._simulators.get(sim_id)
        if not sim:
            raise KeyError(f"Simulador '{sim_id}' não encontrado.")
        await sim.start()

    async def stop_simulator(self, sim_id: str) -> None:
        sim = self._simulators.get(sim_id)
        if not sim:
            raise KeyError(f"Simulador '{sim_id}' não encontrado.")
        await sim.stop()

    def get_simulator(self, sim_id: str) -> SimulatorInstance | None:
        return self._simulators.get(sim_id)

    def list_simulators(self) -> dict:
        """Retorna {sim_id: state_dict} para todos os simuladores."""
        return {sid: sim.to_state_dict() for sid, sim in self._simulators.items()}

    async def shutdown_all(self) -> None:
        """Para todos os simuladores. Chamado no shutdown do Hub."""
        for sim_id, sim in self._simulators.items():
            try:
                await sim.stop()
            except Exception as exc:
                logger.error("[%s] Erro ao parar no shutdown: %s", sim_id, exc)
        logger.info("Todos os simuladores parados.")
