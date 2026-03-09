"""
transform_tables.py — Converte 4 fontes de dados para o formato padrão do gateway.

Fontes:
  1. globais_retentiva (CSV)  → Classe = retentiva
  2. globais_wps (CSV)        → Classe = global
  3. global_io (CSV)          → Classe = io_fisica
  4. Descrições MODBUS.xls    → Classe = temperatura

Saídas:
  tables/csv_individuais/retentivas.csv
  tables/csv_individuais/globais.csv
  tables/csv_individuais/io_fisicas.csv
  tables/csv_individuais/temperatura_24z.csv
  tables/csv_individuais/temperatura_28z.csv
  tables/mapeamento_clp.csv         (fontes 1+2+3 consolidado)
  tables/mapeamento_temperatura.csv (fonte 4 consolidado)
"""

import os
import re
import sys
import warnings

import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GATEWAY_DIR = os.path.dirname(SCRIPT_DIR)
SOURCE_DIR = os.path.join(GATEWAY_DIR, "Tabelas a formatar")
TABLES_DIR = os.path.join(GATEWAY_DIR, "tables")
INDIVIDUAL_DIR = os.path.join(TABLES_DIR, "csv_individuais")

CSV_COLUMNS = [
    "key", "ObjecTag", "Identifiers", "Tipo", "Delta Adress",
    "Modbus", "At", "comentarios", "Classe",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def upper_snake_to_lower_camel(name: str) -> str:
    """GANHO_P_EXT_IHM → ganhoPExt"""
    # Strip _IHM suffix
    name = re.sub(r'_IHM$', '', name)
    parts = name.split('_')
    return parts[0].lower() + ''.join(p.capitalize() for p in parts[1:])


def validate_rows(rows: list[dict], label: str) -> list[str]:
    """Validates output rows. Returns list of warning messages."""
    warns = []
    tags_seen = set()
    addr_by_type: dict[str, list[str]] = {}

    for r in rows:
        tag = r.get("ObjecTag", "")
        if not tag:
            warns.append(f"[{label}] Linha com ObjecTag vazio: {r}")
        if not r.get("key"):
            warns.append(f"[{label}] Linha com key vazio: tag={tag}")
        if tag in tags_seen:
            warns.append(f"[{label}] ObjecTag duplicado: {tag}")
        tags_seen.add(tag)

        tipo = r.get("Tipo", "")
        at = r.get("At", "")
        if tipo == "M" and at != "%MB":
            warns.append(f"[{label}] Tipo/At inconsistente: tag={tag} Tipo={tipo} At={at}")
        if tipo == "D" and at != "%MW":
            warns.append(f"[{label}] Tipo/At inconsistente: tag={tag} Tipo={tipo} At={at}")

        modbus = r.get("Modbus")
        if modbus is not None:
            key_addr = f"{tipo}:{modbus}"
            if key_addr in addr_by_type:
                addr_by_type[key_addr].append(tag)
            else:
                addr_by_type[key_addr] = [tag]

    for key_addr, tag_list in addr_by_type.items():
        if len(tag_list) > 1:
            warns.append(f"[{label}] Endereço Modbus duplicado {key_addr}: {', '.join(tag_list)}")

    return warns


def write_csv(rows: list[dict], output_path: str) -> None:
    """Writes rows to CSV with the standard columns."""
    df = pd.DataFrame(rows, columns=CSV_COLUMNS)
    df.to_csv(output_path, index=False)
    print(f"  ✓ {os.path.basename(output_path)}: {len(rows)} linhas")


# ── Fonte 1: globais_retentiva ────────────────────────────────────────────────

# key mapping based on tag prefixes/names
_RETENTIVA_KEY_MAP = {
    "TEMPO_CORTE": "corte",
    "COMPR_CORTE": "corte",
    "FATOR_AJUSTE_CAL1": "ajuste",
    "FATOR_AJUSTE_CAL2": "ajuste",
    "FATOR_AJUSTE_CAL3": "ajuste",
    "FATOR_AJUSTE_PUXADOR": "ajuste",
    "FATOR_AJUSTE_PREARRASTE": "ajuste",
    "RAMPA_ACELERA_EXT": "rampa_extrusora",
    "RAMPA_DESACELERA_EXT": "rampa_extrusora",
    "GANHO_P_EXT": "pid_extrusora",
    "GANHO_I_EXT": "pid_extrusora",
    "GANHO_D_EXT": "pid_extrusora",
    "RAMPA_ACELERA_PREARRASTE": "rampa_prearraste",
    "RAMPA_DESACELERA_PREARRASTE": "rampa_prearraste",
    "AJUSTE_FINO_COMPR_PREARRASTE": "rampa_prearraste",
    "RAMPA_ACELERA_BOMBA": "rampa_bomba",
    "RAMPA_DESACELERA_BOMBA": "rampa_bomba",
    "ALM_PRESS_H_COEX": "alarme_pressao_coextrusora",
    "ALM_PRESS_HH_COEX": "alarme_pressao_coextrusora",
    "RAMPA_ACELERA_CO_EXT1": "rampa_coextrusora",
    "RAMPA_DESACELERA_CO_EXT1": "rampa_coextrusora",
    "ALM_PRESS_H_BOMBA": "alarme_pressao_bomba",
    "ALM_PRESS_HH_BOMBA": "alarme_pressao_bomba",
    "AJUSTE_RAMPA_ACEL_CAL_PUX": "rampa_calandra_puxador",
    "AJUSTE_RAMPA_DESACEL_CAL_PUX": "rampa_calandra_puxador",
    "SP_PROD_PREARRASTE": "producao_prearraste",
    "ACUM_PROD_PREARRASTE": "producao_prearraste",
    "SP_CONTAMETRO_PREARRASTE": "producao_prearraste",
    "ACUM_CONTAMETRO_PREARRASTE": "producao_prearraste",
}


def transform_retentivas() -> list[dict]:
    """Fonte 1: globais_retentiva → Classe = retentiva"""
    fname = [f for f in os.listdir(SOURCE_DIR)
             if f.startswith("globais_retentiva")]
    if not fname:
        raise FileNotFoundError("CSV globais_retentiva não encontrado")
    df = pd.read_csv(os.path.join(SOURCE_DIR, fname[0]))

    rows = []
    for _, r in df.iterrows():
        tag_raw = str(r["Tag"]).strip()
        base = re.sub(r'_IHM$', '', tag_raw)
        key = _RETENTIVA_KEY_MAP.get(base, "outros")
        obj_tag = upper_snake_to_lower_camel(tag_raw)
        modbus = int(r["Modbus"])
        rows.append({
            "key": key,
            "ObjecTag": obj_tag,
            "Identifiers": tag_raw,
            "Tipo": "D",
            "Delta Adress": str(r["Address"]),
            "Modbus": modbus,
            "At": "%MW",
            "comentarios": "",
            "Classe": "retentiva",
        })
    return rows


# ── Fonte 2: globais_wps ─────────────────────────────────────────────────────

_GLOBAIS_KEY_MAP = {
    # alarmes
    "ALARMES_WORD": "alarmes",
    # controle extrusora
    "TRATE2_EXTR": "controle_extrusora",
    "OUT_EXTR": "controle_extrusora",
    "MAX_EXTR": "controle_extrusora",
    "MIN_EXTR": "controle_extrusora",
    "TS_EXTR": "controle_extrusora",
    # botoes extrusora
    "UP_EXTR": "botoes_extrusora",
    "DOWN_EXTR": "botoes_extrusora",
    "ENRATE_EXTR": "botoes_extrusora",
    "RESET_EP_EXTR": "botoes_extrusora",
    # multistate
    "MULTISTATE_EXTRUSORA": "multistate",
    "MULTISTATE_BOMBA": "multistate",
    "MULTISTATE_CALANDRA1": "multistate",
    "MULTISTATE_CALANDRA2": "multistate",
    "MULTISTATE_CALANDRA3": "multistate",
    "MULTISTATE_PUXADOR": "multistate",
    "MULTISTATE_PREARRASTE": "multistate",
    "MULTISTATE_COEXTRUSORA": "multistate",
    # sistema
    "RESET_ALARME": "sistema",
    "HAB_BLOQ_TEMP": "sistema",
    "IND_DESV_TEMP": "sistema",
    # comandos
    "LIGA_DESL_BOMBA": "comandos",
    "LIGA_DESL_EXTR": "comandos",
    "LIGA_DESL_FACA": "comandos",
    "LIGA_DESL_MARCHA": "comandos",
    "LIGA_DESL_CO_EXT1": "comandos",
    # material
    "PEAD": "material",
    "ACRILICO": "material",
    "ABS": "material",
    "PP": "material",
    "PS": "material",
    "PET": "material",
    "PEBD": "material",
    "PVC": "material",
    "OUTROS": "material",
    "OUTROS_VAL": "material",
    # medidas
    "ESPESSURA": "medidas",
    "LARGURA": "medidas",
    # velocidade
    "VEL_FINAL_EXTRUSORA": "velocidade",
    "VEL_FINAL_BOMBA": "velocidade",
    "VEL_CAL1_MPM": "velocidade",
    "VEL_CAL2_MPM": "velocidade",
    "VEL_CAL3_MPM": "velocidade",
    "VEL_PUXADOR_MPM": "velocidade",
    "VELOCIDADE_CO_EXT1": "velocidade",
    # corrente
    "CORRENTE_EXTRUSORA": "corrente",
    "CORRENTE_CAL1": "corrente",
    "CORRENTE_CAL2": "corrente",
    "CORRENTE_CAL3": "corrente",
    "CORRENTE_BOMBA": "corrente",
    "CORRENTE_PUXADOR": "corrente",
    "CORRENTE_PREARRASTE": "corrente",
    "CORRENTE_CO_EXT1": "corrente",
    # ajuste fino
    "AJUSTE_FINO_PREARRASTE": "ajuste_fino",
    "AJUSTE_FINO_PUXADOR": "ajuste_fino",
    "AJUSTE_FINO_CAL1": "ajuste_fino",
    "AJUSTE_FINO_CAL2": "ajuste_fino",
    "AJUSTE_FINO_CAL3": "ajuste_fino",
    # alarme canal
    "AL_CAN_BOMBA": "alarme_canal",
    "AL_CAN_PREARRASTE": "alarme_canal",
    "AL_CAN_BOBINADOR1": "alarme_canal",
    "AL_CAN_BOBINADOR2": "alarme_canal",
    "AL_CAN_RUW_REM": "alarme_canal",
    "AL_CAN_RUW_PAN": "alarme_canal",
    "AL_CAN_EXTRUSORA": "alarme_canal",
    "AL_CAN_COEXTRUSORA": "alarme_canal",
    "AL_CAN_CALANDRA1": "alarme_canal",
    "AL_CAN_CALANDRA_2": "alarme_canal",
    "AL_CAN_CALANDRA3": "alarme_canal",
    "AL_CAN_PUXADOR": "alarme_canal",
    # pressao
    "FBK_PRESSAO_BOMBA": "pressao",
    "FBK_PRESSAO_COEXTR": "pressao",
    # setpoint
    "SET_POINT_VEL_BOMBA": "setpoint",
    "SET_POINT_VEL_CAL2_MARCHA": "setpoint",
    "ESCRITA_SET_POINT_PRESSAO": "setpoint",
    # producao
    "PROD_ESTIMADA": "producao",
    "PROD_CUM": "producao",
    "PREARRASTE_MPM_ATUAL": "producao",
    # torque bobinador
    "TORQUE_BOBINADOR1": "torque_bobinador",
    "TORQUE_BOBINADOR2": "torque_bobinador",
    "SP_TORQUE_BOBINADOR1": "torque_bobinador",
    "SP_TORQUE_BOBINADOR2": "torque_bobinador",
    # bobinador
    "VEL_BOBINADOR1_RPM_ATUAL": "bobinador",
    "VEL_BOBINADOR2_RPM_ATUAL": "bobinador",
    "CORRENTE_BOBINADOR1": "bobinador",
    "CORRENTE_BOBINADOR2": "bobinador",
    # modo operacao
    "EXT_MAN0_AUT1": "modo_operacao",
    "COEX_MAN0_AUT1": "modo_operacao",
    # alerta troca tela
    "ALERTA_TROCA_TELA_EXT": "alerta_troca_tela",
    "ALERTA_TROCA_TELA_COEX": "alerta_troca_tela",
    # reset
    "RESET_PROD_PREARRASTE": "reset",
    "RESET_CONTAMETRO_PREARRASTE": "reset",
    "RESET_CUM_PROD": "reset",
}


def _globais_key_lookup(tag_base: str) -> str:
    """Lookup key for globais_wps, handling numbered ALARMES_WORD tags."""
    if tag_base in _GLOBAIS_KEY_MAP:
        return _GLOBAIS_KEY_MAP[tag_base]
    # Handle ALARMES_WORD1..12
    m = re.match(r'^(ALARMES_WORD)\d+$', tag_base)
    if m:
        return _GLOBAIS_KEY_MAP.get(m.group(1), "outros")
    return "outros"


def transform_globais() -> list[dict]:
    """Fonte 2: globais_wps → Classe = global"""
    fname = [f for f in os.listdir(SOURCE_DIR)
             if f.startswith("globais_wps")]
    if not fname:
        raise FileNotFoundError("CSV globais_wps não encontrado")
    df = pd.read_csv(os.path.join(SOURCE_DIR, fname[0]))

    rows = []
    for _, r in df.iterrows():
        tag_raw = str(r["Tag"]).strip()
        tipo_src = str(r["Tipo"]).strip()   # %MW or %MB
        base = re.sub(r'_IHM$', '', tag_raw)
        key = _globais_key_lookup(base)
        obj_tag = upper_snake_to_lower_camel(tag_raw)
        modbus = int(r["Modbus"])

        if tipo_src == "%MW":
            tipo, at = "D", "%MW"
        else:  # %MB
            tipo, at = "M", "%MB"

        rows.append({
            "key": key,
            "ObjecTag": obj_tag,
            "Identifiers": tag_raw,
            "Tipo": tipo,
            "Delta Adress": str(r["Address"]),
            "Modbus": modbus,
            "At": at,
            "comentarios": "",
            "Classe": "global",
        })
    return rows


# ── Fonte 3: global_io ───────────────────────────────────────────────────────

def _io_key(tag: str) -> str:
    """Determine key based on I/O tag prefix and number."""
    m = re.match(r'^(DI|DO|AI|AO)(\d+)$', tag)
    if not m:
        return "io_outros"
    prefix, num_str = m.group(1), int(m.group(2))

    type_map = {
        "DI": "entrada_digital",
        "DO": "saida_digital",
        "AI": "entrada_analogica",
        "AO": "saida_analogica",
    }
    base = type_map[prefix]

    if num_str >= 200:
        slot = "slot2"
    elif num_str >= 100:
        slot = "slot1"
    else:
        slot = "base"

    return f"{base}_{slot}"


def transform_io() -> list[dict]:
    """Fonte 3: global_io → Classe = io_fisica"""
    fname = [f for f in os.listdir(SOURCE_DIR)
             if f.startswith("global_io")]
    if not fname:
        raise FileNotFoundError("CSV global_io não encontrado")
    df = pd.read_csv(os.path.join(SOURCE_DIR, fname[0]))

    rows = []
    for _, r in df.iterrows():
        tag_raw = str(r["Tag"]).strip()
        tipo_src = str(r["Tipo"]).strip()   # %IB or %IW
        comment = str(r.get("Comment", "")).strip() if pd.notna(r.get("Comment")) else ""
        modbus = int(r["Modbus"])

        if tipo_src == "%IB":
            tipo, at = "M", "%MB"
        else:  # %IW
            tipo, at = "D", "%MW"

        key = _io_key(tag_raw)
        obj_tag = tag_raw.lower()

        rows.append({
            "key": key,
            "ObjecTag": obj_tag,
            "Identifiers": tag_raw,
            "Tipo": tipo,
            "Delta Adress": "",
            "Modbus": modbus,
            "At": at,
            "comentarios": comment,
            "Classe": "io_fisica",
        })
    return rows


# ── Fonte 4: Descrições MODBUS.xls ───────────────────────────────────────────

# Mapping from description pattern to (key, objtag_prefix)
_TEMP_READ_PATTERNS = [
    (r'^Temperatura Zona (\d+)$',                "temperatura",                      "tempZona"),
    (r'^Corrente (?:na )?Zona (\d+)$',           "corrente_zona",                    "correnteZona"),
    (r'^Temperatura Não Atingida Zona (\d+)$',   "alarme_temp_nao_atingida",         "tempNaoAtingidaZona"),
    (r'^Temparatura Baixa Zona (\d+)$',          "alarme_temp_baixa",                "tempBaixaZona"),
    (r'^Temperatura Baixa Zona (\d+)$',          "alarme_temp_baixa",                "tempBaixaZona"),
    (r'^Corrente Baixa Zona (\d+)$',             "alarme_corrente_baixa",            "correnteBaixaZona"),
    (r'^Intensidade de Aquecimento Zona (\d+)$', "intensidade_aquecimento",          "intensidadeAquecZona"),
    (r'^Pressão Sensor (\d+)$',                  "pressao_sensor",                   "pressaoSensor"),
    (r'^Alarme Alto Pressão (\d+)$',             "alarme_alto_pressao",              "alarmeAltoPressao"),
    (r'^Alarme Alto Alto Pressão (\d+)$',        "alarme_alto_alto_pressao",         "alarmeAltoAltoPressao"),
    (r'^Corrente Zona Pressão (\d+)$',           "corrente_zona_pressao",            "correnteZonaPressao"),
    (r'^Corrente Baixa Pressão (\d+)$',          "corrente_baixa_pressao",           "correnteBaixaPressao"),
    (r'^Intensidade de Aquecimento Pressão (\d+)$', "intensidade_aquecimento_pressao", "intensidadeAquecPressao"),
]

_TEMP_WRITE_PATTERNS = [
    (r'^Setpoint Zona (\d+)$',                   "setpoint",                         "setpointZona"),
    (r'^Valor da Corrente Baixa Zona (\d+)$',    "setpoint_corrente_baixa",          "correnteBaixaSetZona"),
    (r'^Liga / Desliga Zona (\d+)$',             "liga_desliga_zona",                "ligaDesligaZona"),
    (r'^Liga / Desliga Sensor (\d+)$',           "liga_desliga_sensor",              "ligaDesligaSensor"),
    (r'^Alarme Alto Sensor (\d+)$',              "alarme_alto_sensor",               "alarmeAltoSensor"),
    (r'^Alarme Alto Alto Sensor (\d+)$',         "alarme_alto_alto_sensor",          "alarmeAltoAltoSensor"),
]


def _is_excluded(desc: str) -> bool:
    """Check if row should be excluded (reserved zones, unused)."""
    if not desc:
        return True
    desc_lower = desc.strip().lower()
    if "(zona reserva)" in desc_lower:
        return True
    if "(sem uso)" in desc_lower or "(s/ uso)" in desc_lower:
        return True
    return False


def _match_patterns(desc: str, patterns: list) -> tuple[str, str] | None:
    """Match description against patterns, return (key, objtag) or None."""
    for regex, key, prefix in patterns:
        m = re.match(regex, desc.strip())
        if m:
            num = m.group(1)
            return key, f"{prefix}{num}"
    return None


def _is_bit_offset(offset_val) -> bool:
    """Check if offset contains a dot (bit address)."""
    s = str(offset_val)
    return '.' in s


def _parse_modbus_decimal(decimal_val) -> tuple[int, str]:
    """Parse DECIMAL column. Returns (modbus_int, delta_adress_str).
    For bit offsets like 1584.01, returns (1584, "48.1") as delta."""
    s = str(decimal_val)
    if '.' in s:
        parts = s.split('.')
        return int(parts[0]), s
    return int(float(s)), ""


def _process_xls_sheet(df: pd.DataFrame, sheet_label: str) -> list[dict]:
    """Process one sheet of the XLS file."""
    rows = []

    # Process rows starting from row 2 (row 0 = section header, row 1 = column headers)
    for i in range(2, len(df)):
        # Read side (cols 0-5)
        read_offset = df.iloc[i, 0]
        read_desc = str(df.iloc[i, 1]).strip() if pd.notna(df.iloc[i, 1]) else ""
        read_decimal = df.iloc[i, 5] if pd.notna(df.iloc[i, 5]) else None

        # Write side (cols 7-12)
        write_offset = df.iloc[i, 7] if pd.notna(df.iloc[i, 7]) else None
        write_desc = str(df.iloc[i, 8]).strip() if pd.notna(df.iloc[i, 8]) else ""
        write_decimal = df.iloc[i, 12] if pd.notna(df.iloc[i, 12]) else None

        # Process read side
        if read_desc and not _is_excluded(read_desc) and read_decimal is not None:
            match = _match_patterns(read_desc, _TEMP_READ_PATTERNS)
            if match:
                key, obj_tag = match
                modbus_int, delta = _parse_modbus_decimal(read_decimal)
                is_bit = _is_bit_offset(read_offset) if pd.notna(read_offset) else False

                if is_bit:
                    tipo, at = "M", "%MB"
                    offset_str = str(read_offset) if pd.notna(read_offset) else ""
                    comment = f"{read_desc} (bit {offset_str} do registro {modbus_int})"
                    delta = offset_str
                else:
                    tipo, at = "D", "%MW"
                    comment = read_desc

                rows.append({
                    "key": key,
                    "ObjecTag": obj_tag,
                    "Identifiers": f"{sheet_label}_read_{read_offset}",
                    "Tipo": tipo,
                    "Delta Adress": delta,
                    "Modbus": modbus_int,
                    "At": at,
                    "comentarios": comment,
                    "Classe": "temperatura",
                })

        # Process write side
        if write_desc and not _is_excluded(write_desc) and write_decimal is not None:
            match = _match_patterns(write_desc, _TEMP_WRITE_PATTERNS)
            if match:
                key, obj_tag = match
                modbus_int, delta = _parse_modbus_decimal(write_decimal)
                is_bit = _is_bit_offset(write_offset) if write_offset is not None else False

                if is_bit:
                    tipo, at = "M", "%MB"
                    offset_str = str(write_offset)
                    comment = f"{write_desc} (bit {offset_str} do registro {modbus_int})"
                    delta = offset_str
                else:
                    tipo, at = "D", "%MW"
                    comment = write_desc

                rows.append({
                    "key": key,
                    "ObjecTag": obj_tag,
                    "Identifiers": f"{sheet_label}_write_{write_offset}",
                    "Tipo": tipo,
                    "Delta Adress": delta,
                    "Modbus": modbus_int,
                    "At": at,
                    "comentarios": comment,
                    "Classe": "temperatura",
                })

    return rows


def _process_xls_sheet_28z_secondary(df: pd.DataFrame) -> list[dict]:
    """Process the secondary English section of Compl. 28 Zonas (rows 63+).
    These are duplicates in English — skip them per plan."""
    return []


def transform_temperatura() -> tuple[list[dict], list[dict]]:
    """Fonte 4: Descrições MODBUS.xls → Classe = temperatura.
    Returns (rows_24z, rows_28z)."""
    xls_path = os.path.join(SOURCE_DIR, "Descrições MODBUS.xls")
    if not os.path.exists(xls_path):
        raise FileNotFoundError("Descrições MODBUS.xls não encontrado")

    xls = pd.ExcelFile(xls_path)

    # Sheet "24 zonas Padrão"
    df_24 = pd.read_excel(xls, "24 zonas Padrão", header=None)
    rows_24 = _process_xls_sheet(df_24, "24z")

    # Sheet "Compl. 28 Zonas" — only rows before the English section (row 56+ are empty/English)
    df_28 = pd.read_excel(xls, "Compl. 28 Zonas", header=None)
    # Find where the English section starts (empty rows block)
    cutoff = len(df_28)
    for i in range(2, len(df_28)):
        # Check if this is the start of the blank/English section
        if pd.isna(df_28.iloc[i, 0]) and pd.isna(df_28.iloc[i, 1]):
            # Check if next few rows are also blank
            blank_count = 0
            for j in range(i, min(i + 5, len(df_28))):
                if pd.isna(df_28.iloc[j, 0]) and pd.isna(df_28.iloc[j, 1]):
                    blank_count += 1
            if blank_count >= 3:
                cutoff = i
                break
    df_28_trimmed = df_28.iloc[:cutoff]
    rows_28 = _process_xls_sheet(df_28_trimmed, "28z")

    return rows_24, rows_28


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    os.makedirs(INDIVIDUAL_DIR, exist_ok=True)

    all_warnings = []

    # Fonte 1: retentivas
    print("\n[1/4] Processando globais_retentiva...")
    rows_ret = transform_retentivas()
    all_warnings.extend(validate_rows(rows_ret, "retentivas"))
    write_csv(rows_ret, os.path.join(INDIVIDUAL_DIR, "retentivas.csv"))

    # Fonte 2: globais
    print("\n[2/4] Processando globais_wps...")
    rows_glob = transform_globais()
    all_warnings.extend(validate_rows(rows_glob, "globais"))
    write_csv(rows_glob, os.path.join(INDIVIDUAL_DIR, "globais.csv"))

    # Fonte 3: io
    print("\n[3/4] Processando global_io...")
    rows_io = transform_io()
    all_warnings.extend(validate_rows(rows_io, "io_fisicas"))
    write_csv(rows_io, os.path.join(INDIVIDUAL_DIR, "io_fisicas.csv"))

    # Fonte 4: temperatura
    print("\n[4/4] Processando Descrições MODBUS.xls...")
    rows_24, rows_28 = transform_temperatura()
    all_warnings.extend(validate_rows(rows_24, "temperatura_24z"))
    all_warnings.extend(validate_rows(rows_28, "temperatura_28z"))
    write_csv(rows_24, os.path.join(INDIVIDUAL_DIR, "temperatura_24z.csv"))
    write_csv(rows_28, os.path.join(INDIVIDUAL_DIR, "temperatura_28z.csv"))

    # Consolidados
    print("\n[Consolidados]")
    clp_rows = rows_ret + rows_glob + rows_io
    all_warnings.extend(validate_rows(clp_rows, "mapeamento_clp"))
    write_csv(clp_rows, os.path.join(TABLES_DIR, "mapeamento_clp.csv"))

    temp_rows = rows_24 + rows_28
    all_warnings.extend(validate_rows(temp_rows, "mapeamento_temperatura"))
    write_csv(temp_rows, os.path.join(TABLES_DIR, "mapeamento_temperatura.csv"))

    # Summary
    print(f"\n{'='*60}")
    print(f"Resumo:")
    print(f"  retentivas:    {len(rows_ret)} linhas")
    print(f"  globais:       {len(rows_glob)} linhas")
    print(f"  io_fisicas:    {len(rows_io)} linhas")
    print(f"  temperatura_24z: {len(rows_24)} linhas")
    print(f"  temperatura_28z: {len(rows_28)} linhas")
    print(f"  mapeamento_clp:  {len(clp_rows)} linhas (consolidado)")
    print(f"  mapeamento_temp: {len(temp_rows)} linhas (consolidado)")

    if all_warnings:
        print(f"\n⚠ {len(all_warnings)} warnings:")
        for w in all_warnings:
            print(f"  {w}")
    else:
        print("\n✓ Nenhum warning.")

    return 0 if not any("ObjecTag duplicado" in w and "mapeamento" in w for w in all_warnings) else 1


if __name__ == "__main__":
    sys.exit(main())
