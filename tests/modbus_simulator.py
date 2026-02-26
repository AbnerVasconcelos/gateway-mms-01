#!/usr/bin/env python3
"""
Simulador Modbus TCP — substituto do CLP para testes do gateway IoT.

Lê os CSVs de mapeamento (operacao.csv, configuracao.csv) e inicializa
um servidor Modbus TCP com valores realistas. Permite testar Delfos
(leitura) e Atena (escrita) sem precisar de um CLP físico.

Uso:
    python tests/modbus_simulator.py                 # bind 0.0.0.0:5020
    python tests/modbus_simulator.py --port 502      # porta Modbus padrão
    python tests/modbus_simulator.py --simulate      # varia valores continuamente
    python tests/modbus_simulator.py -v              # log detalhado (mostra leituras)

Configurar Delfos/Atena para usar o simulador:
    MODBUS_HOST=127.0.0.1
    MODBUS_PORT=5020
"""

import asyncio
import argparse
import logging
import math
import os
import random

import pandas as pd
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

logger = logging.getLogger("modbus_sim")


# ---------------------------------------------------------------------------
# Data block com logging de leituras e escritas
# ---------------------------------------------------------------------------

class LoggingDataBlock(ModbusSequentialDataBlock):
    """Sequential data block que registra toda leitura e escrita no log."""

    def __init__(self, start_address: int, values: list, name: str = ""):
        super().__init__(start_address, values)
        self._name = name

    def getValues(self, address: int, count: int = 1) -> list:
        values = super().getValues(address, count)
        logger.debug(
            "READ  [%s] addr=%-6d count=%d  → %s",
            self._name, address, count, values,
        )
        return values

    def setValues(self, address: int, values) -> None:
        logger.info(
            "WRITE [%s] addr=%-6d          ← %s",
            self._name, address, list(values),
        )
        super().setValues(address, values)


# ---------------------------------------------------------------------------
# Valores iniciais realistas por tipo de tag
# ---------------------------------------------------------------------------

def _initial_register_value(tag: str) -> int:
    t = tag.lower()
    if any(k in t for k in ("speed", "velocidade", "veloc", "feedbackspeed")):
        return random.randint(1400, 1600)
    if any(k in t for k in ("temp", "temperatura")):
        return random.randint(1800, 2200)   # ×10 → 180.0–220.0 °C
    if any(k in t for k in ("pressao", "pressure")):
        return random.randint(40, 60)
    if any(k in t for k in ("largura",)):
        return random.randint(900, 1100)
    if any(k in t for k in ("espessura", "thickness")):
        return random.randint(20, 80)
    if any(k in t for k in ("peso", "weight", "nivel", "level")):
        return random.randint(200, 800)
    if any(k in t for k in ("kg", "grama", "gramatura")):
        return random.randint(100, 500)
    if any(k in t for k in ("ref", "programado", "setpoint")):
        return random.randint(1300, 1550)
    if any(k in t for k in ("percentual",)):
        return random.randint(0, 100)
    return random.randint(0, 500)


# ---------------------------------------------------------------------------
# Leitura dos CSVs
# ---------------------------------------------------------------------------

def load_csv(csv_path: str) -> tuple[dict, dict]:
    """
    Parse de um CSV de mapeamento.

    Retorna:
        coils     → {modbus_address: valor_inicial (0 ou 1)}
        registers → {modbus_address: valor_inicial (int)}
    """
    try:
        df = pd.read_csv(csv_path, skipinitialspace=True)
    except Exception as exc:
        logger.error("Falha ao ler %s: %s", csv_path, exc)
        return {}, {}

    df = df.dropna(subset=["Tipo", "Modbus"])
    df["Modbus"] = pd.to_numeric(df["Modbus"], errors="coerce")
    df = df.dropna(subset=["Modbus"])
    df["Modbus"] = df["Modbus"].astype(int)

    coils: dict[int, int] = {}
    regs:  dict[int, int] = {}

    for _, row in df.iterrows():
        addr = int(row["Modbus"])
        tag  = str(row.get("ObjecTag", ""))
        tipo = str(row["Tipo"]).strip()
        if tipo == "M":
            coils[addr] = random.randint(0, 1)
        elif tipo == "D":
            regs[addr] = _initial_register_value(tag)

    return coils, regs


# ---------------------------------------------------------------------------
# Construção do contexto Modbus
# ---------------------------------------------------------------------------

def build_context(tables_dir: str) -> tuple[ModbusServerContext, list[int]]:
    """
    Carrega todos os CSVs, mescla endereços e cria um ModbusServerContext.

    zero_mode=True: endereço Modbus X no protocolo → índice X no data block
    (sem o offset +1 que pymodbus aplica por padrão).
    """
    all_coils: dict[int, int] = {}
    all_regs:  dict[int, int] = {}

    for fname in ("operacao.csv", "configuracao.csv"):
        path = os.path.join(tables_dir, fname)
        if os.path.exists(path):
            coils, regs = load_csv(path)
            all_coils.update(coils)
            all_regs.update(regs)
            logger.info(
                "  %-22s → %3d coils, %3d registers",
                fname, len(coils), len(regs),
            )
        else:
            logger.warning("CSV não encontrado: %s", path)

    # Fallback se nenhum CSV foi encontrado
    if not all_coils:
        logger.warning("Nenhum coil carregado — usando endereços de exemplo.")
        all_coils = {2150 + i: random.randint(0, 1) for i in range(30)}
    if not all_regs:
        logger.warning("Nenhum register carregado — usando endereços de exemplo.")
        all_regs = {39770 + i: 1500 + i * 10 for i in range(50)}

    max_coil = max(all_coils.keys())
    max_reg  = max(all_regs.keys())

    # Blocos sequenciais começando em 0, cobrindo o maior endereço do CSV.
    # Endereços intermediários não mapeados ficam em 0.
    coil_vals = [0] * (max_coil + 2)
    for addr, val in all_coils.items():
        coil_vals[addr] = val

    reg_vals = [0] * (max_reg + 2)
    for addr, val in all_regs.items():
        reg_vals[addr] = val

    slave = ModbusSlaveContext(
        co=LoggingDataBlock(0, coil_vals, "COIL"),
        hr=LoggingDataBlock(0, reg_vals,  "HREG"),
        zero_mode=True,
    )
    context = ModbusServerContext(slaves=slave, single=True)

    logger.info(
        "Contexto pronto: %d coils (max addr=%d), %d registers (max addr=%d)",
        len(all_coils), max_coil,
        len(all_regs),  max_reg,
    )
    return context, list(all_regs.keys())


# ---------------------------------------------------------------------------
# Loop de simulação (opcional): varia lentamente alguns registers
# ---------------------------------------------------------------------------

async def simulate_values(
    context: ModbusServerContext,
    reg_addresses: list[int],
    interval: float = 2.0,
) -> None:
    """
    Nudge de valores de processo: imita variação real de velocidade/produção.
    Apenas os primeiros N registros são atualizados para manter o log legível.
    """
    N     = min(8, len(reg_addresses))
    slave = context[0x00]
    step  = 0

    while True:
        await asyncio.sleep(interval)
        step += 1
        for addr in reg_addresses[:N]:
            vals    = slave.getValues(3, addr, 1)
            current = vals[0] if vals else 1000
            swing   = int(math.sin(step * 0.2) * max(current * 0.04, 10))
            noise   = random.randint(-3, 3)
            new_val = max(0, min(65535, current + swing + noise))
            slave.setValues(3, addr, [new_val])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run(args: argparse.Namespace) -> None:
    tables_dir = os.path.abspath(args.tables)

    logger.info("=" * 52)
    logger.info("  Simulador Modbus TCP")
    logger.info("  Bind     : %s:%d", args.host, args.port)
    logger.info("  Tabelas  : %s", tables_dir)
    logger.info("=" * 52)

    context, reg_addresses = build_context(tables_dir)

    if args.simulate:
        asyncio.create_task(simulate_values(context, reg_addresses))
        logger.info("Simulação de variação de valores ativada (intervalo=2s).")

    logger.info("Servidor pronto. Aguardando conexões…  (Ctrl+C para parar)")
    await StartAsyncTcpServer(context=context, address=(args.host, args.port))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Simulador Modbus TCP para testes do gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--host", default="0.0.0.0",
        help="Endereço de bind (padrão: 0.0.0.0)",
    )
    ap.add_argument(
        "--port", type=int, default=5020,
        help="Porta TCP (padrão: 5020, use 502 para Modbus padrão)",
    )
    ap.add_argument(
        "--tables",
        default=os.path.join(os.path.dirname(__file__), "..", "tables"),
        help="Diretório dos CSVs de mapeamento (padrão: ../tables)",
    )
    ap.add_argument(
        "--simulate", action="store_true",
        help="Simula variação contínua de valores de processo",
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="Nível DEBUG — exibe todas as leituras no log",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
