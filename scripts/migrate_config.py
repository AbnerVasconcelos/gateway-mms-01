"""
migrate_config.py — Migra configuracao do gateway para formato per-device.

Mudancas aplicadas:
  1. Move channels globais para dentro de cada device
  2. Adiciona command_channel a cada device
  3. Remove aggregate_channel e backward_compatible de _meta
  4. Remove secao global 'channels'
  5. Particiona variable_overrides.json em arquivos per-device
  6. Tags orfas (sem CSV correspondente) vao para variable_overrides_orphans.json
  7. Renomeia variable_overrides.json original para .bak

Uso:
    python scripts/migrate_config.py [tables_dir]

    tables_dir: caminho para o diretorio tables/ (default: tables/)
"""

import json
import logging
import os
import sys

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("migrate_config")


def load_json(path: str) -> dict:
    """Carrega um arquivo JSON. Retorna {} se nao encontrado ou vazio."""
    if not os.path.exists(path):
        logger.warning("Arquivo nao encontrado: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return {}
        return json.loads(content)


def save_json(path: str, data: dict) -> None:
    """Salva dict como JSON formatado."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Salvo: %s (%d entradas)", path, len(data))


def collect_tags_from_csv(tables_dir: str, csv_files: list[str]) -> set[str]:
    """Le os CSVs de um device e coleta todos os ObjecTag."""
    tags: set[str] = set()
    for csv_name in csv_files:
        csv_path = os.path.join(tables_dir, csv_name)
        if not os.path.exists(csv_path):
            logger.warning("CSV nao encontrado: %s", csv_path)
            continue
        try:
            df = pd.read_csv(csv_path, sep=",")
            if "ObjecTag" not in df.columns:
                logger.warning("CSV sem coluna ObjecTag: %s", csv_name)
                continue
            df = df.dropna(subset=["ObjecTag"])
            for tag in df["ObjecTag"]:
                tag_str = str(tag).strip()
                if tag_str:
                    tags.add(tag_str)
        except Exception as e:
            logger.error("Erro ao ler %s: %s", csv_name, e)
    return tags


def is_already_migrated(config: dict) -> bool:
    """Verifica se ja foi migrado — algum device tem 'channels' key."""
    for dev_cfg in config.get("devices", {}).values():
        if "channels" in dev_cfg:
            return True
    return False


def migrate(tables_dir: str) -> None:
    """Executa a migracao completa."""
    tables_dir = os.path.abspath(tables_dir)
    logger.info("Diretorio tables: %s", tables_dir)

    # ── 1. Carrega group_config.json ──────────────────────────────────────────
    gc_path = os.path.join(tables_dir, "group_config.json")
    config = load_json(gc_path)
    if not config:
        logger.error("group_config.json vazio ou nao encontrado em %s", tables_dir)
        sys.exit(1)

    # ── 2. Verifica se ja migrado ─────────────────────────────────────────────
    if is_already_migrated(config):
        logger.info("Configuracao ja migrada (devices possuem 'channels'). Abortando.")
        sys.exit(0)

    devices = config.get("devices", {})
    if not devices:
        logger.error("Nenhum device encontrado em group_config.json.")
        sys.exit(1)

    global_channels = config.get("channels", {})
    meta = config.get("_meta", {})

    logger.info("Devices encontrados: %s", list(devices.keys()))
    logger.info("Canais globais: %s", list(global_channels.keys()))

    # ── 3. Move channels para dentro de cada device ───────────────────────────
    for dev_id, dev_cfg in devices.items():
        # Copia channels globais para o device
        dev_cfg["channels"] = {
            ch: dict(ch_cfg) for ch, ch_cfg in global_channels.items()
        }
        # Adiciona command_channel
        dev_cfg["command_channel"] = f"{dev_id}_commands"
        logger.info(
            "Device '%s': %d canais copiados, command_channel='%s_commands'",
            dev_id,
            len(global_channels),
            dev_id,
        )

    # ── 4. Limpa _meta e remove channels global ──────────────────────────────
    meta.pop("aggregate_channel", None)
    meta.pop("backward_compatible", None)
    config["_meta"] = meta

    config.pop("channels", None)
    logger.info("Secao global 'channels' removida. _meta limpo.")

    # ── 5. Salva group_config.json migrado ────────────────────────────────────
    save_json(gc_path, config)

    # ── 6. Particiona variable_overrides.json ─────────────────────────────────
    ov_path = os.path.join(tables_dir, "variable_overrides.json")
    overrides = load_json(ov_path)

    # Coleta tags por device
    device_tags: dict[str, set[str]] = {}
    for dev_id, dev_cfg in devices.items():
        csv_files = dev_cfg.get("csv_files", [])
        tags = collect_tags_from_csv(tables_dir, csv_files)
        device_tags[dev_id] = tags
        logger.info("Device '%s': %d tags em %d CSVs", dev_id, len(tags), len(csv_files))

    # Atribui overrides a devices (first-match)
    device_overrides: dict[str, dict] = {dev_id: {} for dev_id in devices}
    orphan_overrides: dict = {}
    assigned_count = 0
    orphan_count = 0

    # Ordem deterministica dos devices (preserva ordem do JSON)
    device_order = list(devices.keys())

    for tag, tag_cfg in overrides.items():
        assigned = False
        for dev_id in device_order:
            if tag in device_tags[dev_id]:
                device_overrides[dev_id][tag] = tag_cfg
                assigned = True
                assigned_count += 1
                break
        if not assigned:
            orphan_overrides[tag] = tag_cfg
            orphan_count += 1

    logger.info(
        "Overrides particionados: %d atribuidos, %d orfaos (de %d total)",
        assigned_count,
        orphan_count,
        len(overrides),
    )

    # Salva per-device overrides
    for dev_id in device_order:
        dev_ov_path = os.path.join(tables_dir, f"variable_overrides_{dev_id}.json")
        save_json(dev_ov_path, device_overrides[dev_id])

    # Salva orfaos (se houver)
    if orphan_overrides:
        orphan_path = os.path.join(tables_dir, "variable_overrides_orphans.json")
        save_json(orphan_path, orphan_overrides)
    else:
        logger.info("Nenhum override orfao.")

    # ── 7. Renomeia original para .bak ────────────────────────────────────────
    if os.path.exists(ov_path):
        bak_path = ov_path + ".bak"
        os.rename(ov_path, bak_path)
        logger.info("Renomeado: %s -> %s", ov_path, bak_path)

    # ── Sumario ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("Migracao concluida com sucesso!")
    logger.info("  group_config.json: canais movidos para dentro dos devices")
    for dev_id in device_order:
        n = len(device_overrides[dev_id])
        logger.info("  variable_overrides_%s.json: %d tags", dev_id, n)
    if orphan_overrides:
        logger.info("  variable_overrides_orphans.json: %d tags", len(orphan_overrides))
    logger.info("  variable_overrides.json.bak: backup do original")
    logger.info("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        tables = sys.argv[1]
    else:
        tables = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tables")
    migrate(tables)
