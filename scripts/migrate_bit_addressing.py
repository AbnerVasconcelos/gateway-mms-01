"""
migrate_bit_addressing.py — Corrige enderecos Modbus bit-addressed nos CSVs de temperatura.

Problema:
    Variaveis bit-addressed (At=%MB, Tipo=M) compartilham o mesmo inteiro na coluna
    'Modbus' (ex.: todas mostram '1584'), quando deveriam ter '1584.01' para bit 1,
    '1584.02' para bit 2, etc.

    A coluna 'Delta Adress' e ambigua: '.1' pode significar bit 1 ou bit 10 (zona 2
    vs zona 11). A fonte autoritativa e a POSICAO SEQUENCIAL das linhas no CSV —
    dentro de cada registro, a ordem das linhas reflete exatamente a ordem dos bits.

Logica:
    1. Agrupa todas as linhas por registro Modbus (coluna 'Modbus' como inteiro)
    2. Identifica registros bit-addressed: aqueles que possuem pelo menos uma linha
       Tipo=M com At=%MB
    3. Dentro de cada registro bit-addressed, ordena as linhas conforme aparecem no CSV:
       - A primeira entrada (Tipo=D) e o bit 0 — mantem Modbus como inteiro
       - Entradas subsequentes (Tipo=M, At=%MB) sao bits 1, 2, ..., 15
    4. Para cada Tipo=M: Modbus = "{registro}.{bit_index:02d}" (ex.: "1584.01")

    Nota: o bit 0 pode ter At=%MB ou At=%MW dependendo do padrao. Registros cujo
    bit 0 usa At=%MW (ex.: ligaDesligaZona17) nao sao contabilizados como linhas
    %MB, mas SAO contados como bit 0 ao determinar o offset dos bits subsequentes.

Uso:
    python scripts/migrate_bit_addressing.py [tables_dir]

    tables_dir: caminho para o diretorio tables/ (default: tables/)
"""

import logging
import os
import shutil
import sys

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("migrate_bit_addressing")


def get_base_register(modbus_val) -> int | None:
    """Extrai o registro inteiro base de um valor Modbus (ignorando sufixo decimal)."""
    try:
        return int(float(modbus_val))
    except (ValueError, TypeError):
        return None


def build_register_groups(df: pd.DataFrame) -> dict[int, list[dict]]:
    """Agrupa linhas por registro Modbus base, preservando ordem do CSV.

    Inclui TODAS as linhas com At=%MB (Tipo=D ou Tipo=M) num registro.
    Tambem inclui Tipo=D com At=%MW SE o registro tiver pelo menos uma linha
    Tipo=M com At=%MB (indica que o Tipo=D/%MW e o bit 0 do mesmo registro).

    Returns:
        {register: [{"idx": int, "tag": str, "tipo": str, "at": str}, ...]}
        Apenas registros que possuem pelo menos uma linha Tipo=M, At=%MB.
    """
    # Primeira passada: coleta todas as linhas %MB e identifica registros com Tipo=M
    mb_rows_by_register: dict[int, list[dict]] = {}
    registers_with_m: set[int] = set()

    for idx, row in df.iterrows():
        at = str(row.get("At", "")).strip()
        tipo = str(row.get("Tipo", "")).strip()
        tag = str(row.get("ObjecTag", "")).strip()

        if at != "%MB":
            continue

        base_reg = get_base_register(row.get("Modbus"))
        if base_reg is None:
            continue

        mb_rows_by_register.setdefault(base_reg, []).append({
            "idx": idx,
            "tag": tag,
            "tipo": tipo,
            "at": at,
        })

        if tipo == "M":
            registers_with_m.add(base_reg)

    # Segunda passada: para registros com Tipo=M/%MB, busca Tipo=D/%MW
    # que sao bit-0 anchors expressos como holding register
    d_mw_anchors: dict[int, list[dict]] = {}

    for idx, row in df.iterrows():
        at = str(row.get("At", "")).strip()
        tipo = str(row.get("Tipo", "")).strip()
        tag = str(row.get("ObjecTag", "")).strip()

        if at != "%MW" or tipo != "D":
            continue

        base_reg = get_base_register(row.get("Modbus"))
        if base_reg is None or base_reg not in registers_with_m:
            continue

        # Verifica se este registro JA tem uma ancora %MB (Tipo=D, At=%MB)
        # Se sim, o %MW e um registro independente, nao um bit-0 anchor
        has_mb_anchor = any(
            r["tipo"] == "D" for r in mb_rows_by_register.get(base_reg, [])
        )
        if has_mb_anchor:
            continue

        d_mw_anchors.setdefault(base_reg, []).append({
            "idx": idx,
            "tag": tag,
            "tipo": tipo,
            "at": at,
        })

    # Monta resultado: combina anchors %MW + linhas %MB, ordenado por idx
    result: dict[int, list[dict]] = {}

    for reg in registers_with_m:
        entries = []
        # Adiciona anchor %MW se existir
        if reg in d_mw_anchors:
            entries.extend(d_mw_anchors[reg])
        # Adiciona linhas %MB
        if reg in mb_rows_by_register:
            entries.extend(mb_rows_by_register[reg])
        # Ordena pela posicao original no CSV
        entries.sort(key=lambda e: e["idx"])
        result[reg] = entries

    return result


def compute_bit_assignments(
    register_groups: dict[int, list[dict]],
) -> dict[int, int]:
    """Calcula bit_index para cada linha baseado na posicao sequencial dentro do registro.

    Dentro de cada registro, a primeira entrada e bit 0, a segunda e bit 1, etc.

    Returns:
        {df_index: bit_index}
    """
    assignments: dict[int, int] = {}

    for register, entries in register_groups.items():
        for bit_index, entry in enumerate(entries):
            assignments[entry["idx"]] = bit_index

    return assignments


def process_csv(csv_path: str) -> dict:
    """Processa um CSV de temperatura, corrigindo a coluna Modbus para bit-addressing.

    Returns:
        dict com estatisticas: {total_rows, bit_rows, changed, unchanged, errors, changes}
    """
    csv_name = os.path.basename(csv_path)
    logger.info("Processando: %s", csv_name)

    df = pd.read_csv(csv_path, sep=",")
    stats = {
        "total_rows": len(df),
        "bit_rows": 0,
        "changed": 0,
        "unchanged": 0,
        "errors": 0,
        "changes": [],
    }

    # Verifica colunas necessarias
    required_cols = {"ObjecTag", "Tipo", "Modbus", "At"}
    missing = required_cols - set(df.columns)
    if missing:
        logger.error("CSV '%s' faltando colunas: %s", csv_name, missing)
        stats["errors"] = len(df)
        return stats

    # Converte Modbus para string para permitir valores como "1584.01"
    df["Modbus"] = df["Modbus"].apply(
        lambda x: str(int(x)) if pd.notna(x) and isinstance(x, (int, float)) and '.' not in str(x) else str(x) if pd.notna(x) else x
    )

    # ── Fase 1: Agrupa por registro e calcula bit assignments ────────────────
    register_groups = build_register_groups(df)

    if not register_groups:
        logger.warning("Nenhum registro bit-addressed encontrado em '%s'.", csv_name)
        return stats

    bit_assignments = compute_bit_assignments(register_groups)

    # Log dos registros encontrados
    logger.info("Registros bit-addressed encontrados: %d", len(register_groups))
    for register in sorted(register_groups.keys()):
        entries = register_groups[register]
        anchor = entries[0] if entries else None
        m_count = sum(1 for e in entries if e["tipo"] == "M")
        anchor_info = f"{anchor['tag']} ({anchor['at']})" if anchor else "nenhuma"
        logger.info(
            "  Registro %d: %d bits total (ancora: %s, Tipo=M: %d)",
            register, len(entries), anchor_info, m_count,
        )

    # ── Fase 2: Corrige Modbus para linhas Tipo=M, At=%MB ────────────────────
    for idx, row in df.iterrows():
        at = str(row.get("At", "")).strip()
        tipo = str(row.get("Tipo", "")).strip()
        tag = str(row.get("ObjecTag", "")).strip()

        # So processa linhas bit-addressed: Tipo=M, At=%MB
        if at != "%MB" or tipo != "M":
            continue

        stats["bit_rows"] += 1

        if idx not in bit_assignments:
            logger.warning(
                "  Linha %d: tag '%s' sem assignment de bit. Ignorando.",
                idx + 2, tag,  # +2: header + 0-indexed
            )
            stats["errors"] += 1
            continue

        bit_index = bit_assignments[idx]

        if bit_index == 0:
            # Bit 0 com Tipo=M e inesperado (deveria ser Tipo=D), mas se aparecer,
            # mantem como inteiro
            logger.warning(
                "  Linha %d: tag '%s' tem bit_index=0 mas Tipo=M (inesperado). Mantendo.",
                idx + 2, tag,
            )
            stats["unchanged"] += 1
            continue

        # Calcula novo valor Modbus: "registro.bit_index" com zero-padding de 2 digitos
        old_modbus = str(row["Modbus"]).strip()
        base_register = get_base_register(old_modbus)
        if base_register is None:
            logger.warning(
                "  Linha %d: tag '%s' Modbus='%s' nao e numerico. Ignorando.",
                idx + 2, tag, old_modbus,
            )
            stats["errors"] += 1
            continue

        new_modbus = f"{base_register}.{bit_index:02d}"

        if old_modbus == new_modbus:
            stats["unchanged"] += 1
            continue

        # Aplica a correcao
        df.at[idx, "Modbus"] = new_modbus
        stats["changed"] += 1
        stats["changes"].append({
            "line": idx + 2,
            "tag": tag,
            "old": old_modbus,
            "new": new_modbus,
            "bit": bit_index,
        })

    # ── Fase 3: Salva CSV corrigido ─────────────────────────────────────────
    if stats["changed"] > 0:
        df.to_csv(csv_path, index=False)
        logger.info("CSV salvo com %d correcoes: %s", stats["changed"], csv_path)
    else:
        logger.info("Nenhuma correcao necessaria em: %s", csv_name)

    return stats


def migrate(tables_dir: str) -> None:
    """Executa a migracao de bit-addressing nos CSVs de temperatura."""
    tables_dir = os.path.abspath(tables_dir)
    logger.info("Diretorio tables: %s", tables_dir)

    csv_files = [
        "temperatura_24z.csv",
        "temperatura_28z.csv",
    ]

    # ── 1. Verifica existencia dos CSVs ─────────────────────────────────────
    existing = []
    for csv_name in csv_files:
        csv_path = os.path.join(tables_dir, csv_name)
        if os.path.exists(csv_path):
            existing.append(csv_name)
        else:
            logger.warning("CSV nao encontrado, ignorando: %s", csv_path)

    if not existing:
        logger.error("Nenhum CSV de temperatura encontrado em %s", tables_dir)
        sys.exit(1)

    # ── 2. Cria backups ─────────────────────────────────────────────────────
    logger.info("Criando backups...")
    for csv_name in existing:
        src = os.path.join(tables_dir, csv_name)
        dst = os.path.join(tables_dir, csv_name + ".bak")
        shutil.copy2(src, dst)
        logger.info("  Backup: %s -> %s", csv_name, csv_name + ".bak")

    # ── 3. Processa cada CSV ────────────────────────────────────────────────
    all_stats = {}
    for csv_name in existing:
        csv_path = os.path.join(tables_dir, csv_name)
        stats = process_csv(csv_path)
        all_stats[csv_name] = stats

    # ── 4. Sumario ──────────────────────────────────────────────────────────
    logger.info("=" * 70)
    logger.info("Migracao de bit-addressing concluida!")
    logger.info("=" * 70)

    total_changed = 0
    total_errors = 0

    for csv_name, stats in all_stats.items():
        logger.info("")
        logger.info("  %s:", csv_name)
        logger.info("    Total de linhas:       %d", stats["total_rows"])
        logger.info("    Linhas bit-addressed:  %d", stats["bit_rows"])
        logger.info("    Corrigidas:            %d", stats["changed"])
        logger.info("    Ja corretas:           %d", stats["unchanged"])
        logger.info("    Erros/ignoradas:       %d", stats["errors"])

        total_changed += stats["changed"]
        total_errors += stats["errors"]

        if stats["changes"]:
            logger.info("    Detalhes das correcoes:")
            for change in stats["changes"]:
                logger.info(
                    "      L%d: %-40s  %s -> %s  (bit %d)",
                    change["line"],
                    change["tag"],
                    change["old"],
                    change["new"],
                    change["bit"],
                )

    logger.info("")
    logger.info("  TOTAL: %d correcoes, %d erros", total_changed, total_errors)

    if total_changed > 0:
        logger.info("")
        logger.info("  Backups salvos como *.bak no diretorio tables/")
        logger.info("  Para reverter: renomeie os .bak de volta para .csv")

    logger.info("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        tables = sys.argv[1]
    else:
        tables = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tables"
        )
    migrate(tables)
