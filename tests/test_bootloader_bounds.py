"""Execute authenticated device bootloader bounds/erase code in Unicorn only.

No HID access. Hooks replace watchdog and flash operations; all branch and
sector-address arithmetic is executed from the actual archived bootloader.
"""
import hashlib
from pathlib import Path
import unittest
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                              UC_ARM_REG_R5, UC_ARM_REG_SP, UC_ARM_REG_LR,
                              UC_ARM_REG_PC)

ROOT = Path(__file__).resolve().parents[1]
BOOT = ROOT / 'local/bootloader-d386f6ff.bin'   # vendor bootloader dump: kept out of the repository
SHA256 = 'd386f6ffc99ee62cf1c304e3a452871beaed8ed553afe9dd5c5ca25bc8d2038e'
STOP = 0x3000  # Emulated return sentinel, outside the archived bootloader.


class BootloaderBounds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BOOT.exists():
            raise unittest.SkipTest('bootloader dump not present (local/ only)')
        cls.boot = BOOT.read_bytes()
        if len(cls.boot) != 0x2000 or hashlib.sha256(cls.boot).hexdigest() != SHA256:
            raise ValueError('bootloader dump identity mismatch')

    def cpu(self):
        u = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        u.mem_map(0, 0x4000)
        u.mem_write(0, self.boot)
        u.mem_map(0x400000, 0x8000)
        u.reg_write(UC_ARM_REG_SP, 0x407E00)
        u.reg_write(UC_ARM_REG_LR, STOP | 1)
        return u

    def branch(self, start, register, value, destinations):
        u = self.cpu()
        u.reg_write(register, value)
        visited = []

        def stop(uc, address, size, data):
            if address in destinations:
                visited.append(address)
                uc.emu_stop()

        u.hook_add(UC_HOOK_CODE, stop)
        u.emu_start(start | 1, 0x4000, count=100)
        self.assertEqual(len(visited), 1, 'expected exactly one branch destination')
        return visited[0]

    def test_prepare_accepts_one_byte_below_limit(self):
        self.assertEqual(self.branch(0x13E4, UC_ARM_REG_R5, 0x25DFF,
                                     (0x13F0, 0x163C)), 0x13F0)

    def test_prepare_accepts_exact_payload_limit(self):
        self.assertEqual(self.branch(0x13E4, UC_ARM_REG_R5, 0x25E00,
                                     (0x13F0, 0x163C)), 0x13F0)

    def test_prepare_rejects_one_byte_above_limit(self):
        self.assertEqual(self.branch(0x13E4, UC_ARM_REG_R5, 0x25E01,
                                     (0x13F0, 0x163C)), 0x163C)

    def test_data_rejects_last_bootloader_word(self):
        self.assertEqual(self.branch(0x1432, UC_ARM_REG_R0, 0x7FF,
                                     (0x143E, 0x1442)), 0x143E)

    def test_data_accepts_first_application_word(self):
        self.assertEqual(self.branch(0x1432, UC_ARM_REG_R0, 0x800,
                                     (0x143E, 0x1442)), 0x1442)

    def test_data_accepts_last_application_word(self):
        self.assertEqual(self.branch(0x1432, UC_ARM_REG_R0, 0x9F7F,
                                     (0x143E, 0x1442)), 0x1442)

    def test_data_rejects_first_word_above_application(self):
        self.assertEqual(self.branch(0x1432, UC_ARM_REG_R0, 0x9F80,
                                     (0x143E, 0x1442)), 0x143E)

    def test_prepare_erase_wrapper_covers_exact_303_sectors(self):
        u = self.cpu()
        # Exact arguments at the prepare call site 0x1400.
        u.reg_write(UC_ARM_REG_R0, 0)
        u.reg_write(UC_ARM_REG_R1, 0x800)
        u.reg_write(UC_ARM_REG_R2, 0x25E00)
        erases, blank_checks, returned = [], [], []

        def hook(uc, address, size, data):
            if address == STOP:
                returned.append(True)
                uc.emu_stop()
            elif address == 0x1284:  # Watchdog service, unrelated MMIO.
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            elif address == 0xE78:  # Erase primitive: record, no flash writes.
                erases.append((uc.reg_read(UC_ARM_REG_R0),
                               uc.reg_read(UC_ARM_REG_R1)))
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            elif address == 0x4C0:  # Blank check returns success.
                blank_checks.append((uc.reg_read(UC_ARM_REG_R0),
                                     uc.reg_read(UC_ARM_REG_R1),
                                     uc.reg_read(UC_ARM_REG_R2)))
                uc.reg_write(UC_ARM_REG_R0, 1)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

        u.hook_add(UC_HOOK_CODE, hook)
        u.emu_start(0x11C5, 0x4000, count=30000)
        self.assertEqual(returned, [True])
        self.assertEqual(u.reg_read(UC_ARM_REG_R0), 1)
        self.assertEqual(erases, [(0, 0x800 + i * 0x80) for i in range(303)])
        self.assertEqual(blank_checks, [(0, a, 512) for _, a in erases])
        self.assertEqual(erases[0][1], 0x800)
        self.assertEqual(erases[-1][1] + 0x80, 0x9F80)
        self.assertEqual((erases[-1][1] + 0x80) * 4, 0x27E00)

    def test_data_handler_checks_start_but_not_packet_end(self):
        u = self.cpu()
        # Begin after the wire byte address has been converted to words.
        # A 32-byte packet beginning at the last word crosses the official end,
        # yet the actual bootloader reaches its write call. This regression
        # documents why the host must validate the full image interval.
        u.reg_write(UC_ARM_REG_R0, 0x9F7F)
        sp = u.reg_read(UC_ARM_REG_SP)
        u.mem_write(sp + 4, (32).to_bytes(4, 'little'))
        writes = []

        def hook(uc, address, size, data):
            if address == 0x12A8:
                writes.append((uc.reg_read(UC_ARM_REG_R0),
                               uc.reg_read(UC_ARM_REG_R1),
                               uc.reg_read(UC_ARM_REG_R2)))
                uc.emu_stop()  # Do not execute the actual program routine.
            elif address == 0x163C:
                self.fail('bootloader unexpectedly rejected end-crossing packet')

        u.hook_add(UC_HOOK_CODE, hook)
        # Store start word exactly as instructions 0x142e would have done.
        u.mem_write(sp, (0x9F7F).to_bytes(4, 'little'))
        u.emu_start(0x1433, 0x4000, count=100)
        self.assertEqual(writes, [(0, 0x9F7F, 32)])
        self.assertGreater(writes[0][1] * 4 + writes[0][2], 0x27E00)

    def reset(self, marker, mmio=lambda: 0):
        """Run the bootloader from reset; return where it ends up and the flash reads it made."""
        from unicorn import UC_MODE_ARM, UC_HOOK_MEM_READ, UcError
        u = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        u.mem_map(0, 0x4000)
        u.mem_write(0, self.boot)
        u.mem_map(0x400000, 0x10000)
        u.mem_map(0x800000, 0x10000)
        seen = {'reads': []}

        def on_mmio(uc, access, address, size, value, data):
            uc.mem_write(address, (mmio() & 0xffffffff).to_bytes(4, 'little')[:size])

        def on_code(uc, address, size, data):
            if address == 0x127C:      # flash read(selector, word, bytes, out)
                sel, word, n, out = (uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, 3 + UC_ARM_REG_R0))
                seen['reads'].append((sel, word, n))
                uc.mem_write(out, (marker if (sel, word) == (1, 0x807C) else b'').ljust(n, b'\xff')[:n])
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            elif address >= 0x2000:
                seen['end'] = ('application', address)
                uc.emu_stop()
            elif address == 0x1358:
                seen['end'] = ('update mode', address)
                uc.emu_stop()
        u.hook_add(UC_HOOK_MEM_READ, on_mmio, begin=0x800000, end=0x80FFFF)
        u.hook_add(UC_HOOK_CODE, on_code)
        try:
            u.emu_start(0, 0x10000, count=500000)
        except UcError:
            pass
        return seen.get('end'), seen['reads']

    def test_only_the_sector_a_marker_keeps_the_bootloader_in_update_mode(self):
        """There is no key or pin check: an image that passed the upload CRC is always started."""
        import random
        body = bytes([0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC])
        mark = lambda kind: body + bytes([kind, (0x55 - sum(body) - kind) & 255])
        for kind in (0xD1, 0xD2, 0xD3):
            self.assertEqual(self.reset(mark(kind)), (('update mode', 0x1358), [(1, 0x807C, 8)]))
        for bad in (b'', mark(0xD4), mark(0xD1)[:7] + b'\0', bytes(8)):
            self.assertEqual(self.reset(bad), (('application', 0x2020), [(1, 0x807C, 8)]))
        # No hardware register value changes the decision (they are only configured, never tested).
        r = random.Random(1)
        for source in [lambda: 0xFFFFFFFF] + [lambda: r.getrandbits(32)] * 100:
            self.assertEqual(self.reset(b'', source)[0], ('application', 0x2020))
            self.assertEqual(self.reset(mark(0xD1), source)[0], ('update mode', 0x1358))


if __name__ == '__main__':
    unittest.main()
