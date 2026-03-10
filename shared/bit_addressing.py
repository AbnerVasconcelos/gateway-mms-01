"""
bit_addressing — Parsing e manipulação de endereços Modbus com bit indexing.

Variáveis digitais de controladores de temperatura usam bits individuais dentro
de holding registers de 16 bits. A coluna Modbus nos CSVs codifica isso como:

    "1584"       → registrador 1584 inteiro (sem bit, leitura normal)
    "1584.01"    → registrador 1584, bit 1
    "1584.09"    → registrador 1584, bit 9
    "1584.10"    → registrador 1584, bit 10
    "1584.15"    → registrador 1584, bit 15

Convenção de sufixo:
    - 2 dígitos com zero à esquerda (.01-.09): bit 1-9
    - 2 dígitos sem zero (.10-.15): bit 10-15
    - 1 dígito (.1): legado, interpretado como bit 10 (×10)
    - Sem sufixo: registrador inteiro (bit_index = None)
"""


def parse_modbus_address(raw) -> tuple[int, int | None]:
    """
    Parseia string de endereço Modbus com possível sufixo de bit.

    Retorna (register_address, bit_index) onde bit_index é None para
    registradores normais ou 0-15 para bit-addressed.

    Regras de disambiguação do sufixo:
        - len==2: interpretado literal → "01"=1, "10"=10, "15"=15
        - len==1: multiplicado por 10 → "1"=10 (caso legado de truncamento)
    """
    s = str(raw).strip()
    if '.' not in s:
        return int(float(s)), None

    parts = s.split('.', 1)
    register = int(parts[0])
    suffix = parts[1]

    if len(suffix) == 1:
        # Caso legado: .1 = bit 10 (truncado de .10)
        bit_index = int(suffix) * 10
    else:
        # .01=1, .09=9, .10=10, .15=15
        bit_index = int(suffix)

    if bit_index < 0 or bit_index > 15:
        raise ValueError(f"Bit index {bit_index} fora do range 0-15 para endereço '{raw}'")

    return register, bit_index


def extract_bit(register_value: int, bit_index: int) -> bool:
    """Extrai um bit de um valor de registrador de 16 bits."""
    return bool((register_value >> bit_index) & 1)


def set_bit(register_value: int, bit_index: int, bit_val: bool) -> int:
    """Define ou limpa um bit em um valor de registrador de 16 bits."""
    if bit_val:
        return register_value | (1 << bit_index)
    else:
        return register_value & ~(1 << bit_index)


def is_bit_addressed(raw) -> bool:
    """Verifica se o endereço Modbus contém sufixo de bit."""
    return '.' in str(raw)
