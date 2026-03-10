#!/usr/bin/env python3
"""
Testes do módulo shared/bit_addressing.py — parsing e manipulação de bits.

Uso:
    python -m pytest tests/test_bit_addressing.py -v
"""

import os
import sys
import unittest

GATEWAY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, GATEWAY_DIR)

from shared.bit_addressing import (
    parse_modbus_address,
    extract_bit,
    set_bit,
    is_bit_addressed,
)


class TestParseModbusAddress(unittest.TestCase):
    """Testa parse_modbus_address()."""

    def test_parse_normal_address(self):
        """Endereço sem sufixo → registrador inteiro, bit_index=None."""
        self.assertEqual(parse_modbus_address("1584"), (1584, None))
        self.assertEqual(parse_modbus_address("0"), (0, None))
        self.assertEqual(parse_modbus_address("39810"), (39810, None))

    def test_parse_normal_float_string(self):
        """Endereço como float sem parte decimal real (pandas pode gerar '1584.0')."""
        self.assertEqual(parse_modbus_address("1584.0"), (1584, 0))

    def test_parse_normal_int(self):
        """Aceita int direto."""
        self.assertEqual(parse_modbus_address(1584), (1584, None))

    def test_parse_bit_01_to_09(self):
        """Sufixo zero-padded de 2 dígitos: .01=1, .02=2, ..., .09=9."""
        for bit in range(1, 10):
            addr_str = f"1584.{bit:02d}"
            self.assertEqual(
                parse_modbus_address(addr_str), (1584, bit),
                f"Falha para '{addr_str}'"
            )

    def test_parse_bit_10_truncated(self):
        """Caso legado: sufixo de 1 dígito .1 → bit 10 (×10)."""
        self.assertEqual(parse_modbus_address("1584.1"), (1584, 10))

    def test_parse_bit_10_full(self):
        """Caso regenerado: .10 → bit 10."""
        self.assertEqual(parse_modbus_address("1584.10"), (1584, 10))

    def test_parse_bit_11_to_15(self):
        """Sufixo .11-.15 → bits 11-15."""
        for bit in range(11, 16):
            addr_str = f"1584.{bit}"
            self.assertEqual(
                parse_modbus_address(addr_str), (1584, bit),
                f"Falha para '{addr_str}'"
            )

    def test_parse_bit_0_explicit(self):
        """Sufixo .00 → bit 0."""
        self.assertEqual(parse_modbus_address("1584.00"), (1584, 0))

    def test_parse_different_registers(self):
        """Funciona com diferentes registradores base."""
        self.assertEqual(parse_modbus_address("1840.05"), (1840, 5))
        self.assertEqual(parse_modbus_address("1585.15"), (1585, 15))

    def test_parse_invalid_bit_raises(self):
        """Bit fora do range 0-15 levanta ValueError."""
        with self.assertRaises(ValueError):
            parse_modbus_address("1584.16")
        with self.assertRaises(ValueError):
            parse_modbus_address("1584.20")

    def test_parse_whitespace_stripped(self):
        """Espaços são removidos."""
        self.assertEqual(parse_modbus_address("  1584  "), (1584, None))
        self.assertEqual(parse_modbus_address(" 1584.01 "), (1584, 1))


class TestExtractBit(unittest.TestCase):
    """Testa extract_bit()."""

    def test_extract_all_16_bits(self):
        """Extrai cada bit de um valor de 16 bits."""
        # Valor: 0b1010_0101_0011_1100 = 0xA53C = 42300
        value = 0xA53C
        expected = [
            False, False, True, True, True, True, False, False,
            True, False, True, False, False, True, False, True,
        ]
        for bit in range(16):
            self.assertEqual(
                extract_bit(value, bit), expected[bit],
                f"Bit {bit} de 0x{value:04X}"
            )

    def test_extract_bit_0(self):
        self.assertTrue(extract_bit(1, 0))
        self.assertFalse(extract_bit(0, 0))
        self.assertTrue(extract_bit(0xFFFF, 0))

    def test_extract_bit_15(self):
        self.assertTrue(extract_bit(0x8000, 15))
        self.assertFalse(extract_bit(0x7FFF, 15))


class TestSetBit(unittest.TestCase):
    """Testa set_bit()."""

    def test_set_bit_on(self):
        """Ativa um bit."""
        self.assertEqual(set_bit(0, 0, True), 1)
        self.assertEqual(set_bit(0, 5, True), 32)
        self.assertEqual(set_bit(0, 15, True), 0x8000)

    def test_set_bit_off(self):
        """Desativa um bit."""
        self.assertEqual(set_bit(0xFFFF, 0, False), 0xFFFE)
        self.assertEqual(set_bit(0xFFFF, 15, False), 0x7FFF)

    def test_set_bit_idempotent(self):
        """Setar bit que já está ativo não muda o valor."""
        self.assertEqual(set_bit(1, 0, True), 1)
        self.assertEqual(set_bit(0, 0, False), 0)

    def test_set_and_extract_roundtrip(self):
        """Set + extract = valor original."""
        value = 0
        for bit in [0, 3, 7, 11, 15]:
            value = set_bit(value, bit, True)
        for bit in range(16):
            expected = bit in [0, 3, 7, 11, 15]
            self.assertEqual(extract_bit(value, bit), expected, f"Bit {bit}")


class TestIsBitAddressed(unittest.TestCase):
    """Testa is_bit_addressed()."""

    def test_normal_address(self):
        self.assertFalse(is_bit_addressed("1584"))
        self.assertFalse(is_bit_addressed(1584))

    def test_bit_addressed(self):
        self.assertTrue(is_bit_addressed("1584.01"))
        self.assertTrue(is_bit_addressed("1584.1"))
        self.assertTrue(is_bit_addressed("1584.15"))

    def test_float_string(self):
        """Float-like string com .0 é detectado como bit addressed."""
        self.assertTrue(is_bit_addressed("1584.0"))


if __name__ == '__main__':
    unittest.main()
