"""Execute the unchanged OEM length scanner; patches are emulator RAM only."""
import hashlib
from pathlib import Path
import struct
import unittest
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC


class OEMMacroScanner(unittest.TestCase):
    def scanner(self):
        stock = (Path(__file__).resolve().parents[1] / 'stock/CompxUpgradeBinFile.bin').read_bytes()
        self.assertEqual(hashlib.sha256(stock).hexdigest(),
                         '7c061d9ce66c0b45e7d9f02e5d467af78cd537a88529b90fccb5dbf6dbccc7de')
        uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        uc.mem_map(0, 0x40000)
        uc.mem_map(0x400000, 0x8000)
        uc.mem_write(0x2000, stock[0x2000:])
        uc.mem_write(0x401000, b'\xaa' * 16)
        uc.reg_write(UC_ARM_REG_R0, 0x401000)
        uc.reg_write(UC_ARM_REG_SP, 0x407000)
        uc.reg_write(UC_ARM_REG_LR, 0x2f001)
        return uc

    def run_scan(self, uc):
        uc.emu_start(0x18e31, 0x2f000, count=20000)
        self.assertEqual(uc.reg_read(UC_ARM_REG_PC), 0x2f000)
        return bytes(uc.mem_read(0x401000, 16))

    def test_original_scanner_includes_terminators_in_five_lengths(self):
        self.assertEqual(self.run_scan(self.scanner()),
                         bytes([4, 4, 4, 3, 4]) + b'\xaa' * 11)

    def test_scanner_can_read_ram_table_beyond_original_nineteen_steps(self):
        uc = self.scanner()
        self.assertEqual(bytes(uc.mem_read(0x18e64, 4)), struct.pack('<I', 0x22b18))
        self.assertEqual(bytes(uc.mem_read(0x18e5c, 2)), b'\x13\x29')
        uc.mem_write(0x18e64, struct.pack('<I', 0x402000))
        uc.mem_write(0x18e5c, b'\x18\x29')  # cmp r1,#24
        records = b''.join(struct.pack('<H', delay) + bytes(10)
                           for _ in range(6) for delay in (2, 4, 6, 0))
        uc.mem_write(0x402000, records)
        # Poison the original table to establish that RAM is the actual source.
        uc.mem_write(0x22b18, b'\xff' * 228)
        self.assertEqual(self.run_scan(uc), bytes([4] * 6) + b'\xaa' * 10)


if __name__ == '__main__':
    unittest.main()
