"""VIA macro bytecode -> OEM records, checked against hand-derived expectations."""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REC = 12


def rec(delay, mods=0, keys=()):
    r = bytearray(REC)
    r[0], r[1] = delay & 255, delay >> 8
    k = 0
    if mods:
        r[2], r[3] = 1, mods
        k += 1
    for key in keys:
        r[2 + k * 3], r[3 + k * 3] = 2, key
        k += 1
    return bytes(r)


TERM = rec(0)
EMPTY = rec(2) + TERM          # an empty macro still needs a non-zero first record


class MacroCompile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        so = Path(cls.tmp.name) / 'macro.so'
        subprocess.run(['clang', '-shared', '-fPIC', '-O1', '-Wall', '-Wextra', '-Werror', '-I', str(ROOT / 'firmware'),
                        str(ROOT / 'firmware/macro.c'), '-o', str(so)], check=True)
        cls.lib = C.CDLL(str(so))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def compile(self, data, max_records=96):
        buf = (C.c_uint8 * max(len(data), 1))(*data)
        out = (C.c_uint8 * (max_records * REC))()
        lengths = (C.c_uint8 * 8)()
        used = C.c_uint()
        dry = self.lib.macro_compile(buf, len(data), None, max_records, lengths, C.byref(used))
        ok = self.lib.macro_compile(buf, len(data), out, max_records, lengths, C.byref(used))
        self.assertEqual(dry, ok)
        records = bytes(out)[:used.value * REC] if ok else b''
        return ok, records, list(lengths)

    def macro(self, data):
        """Records of macro 0 only; the other seven must be a lone terminator."""
        ok, records, lengths = self.compile(data)
        self.assertEqual(ok, 1)
        self.assertEqual(lengths[1:], [2] * 7)
        self.assertEqual(records[(lengths[0]) * REC:], EMPTY * 7)
        return records[:lengths[0] * REC]

    def test_empty_buffer_gives_eight_safe_empty_macros(self):
        ok, records, lengths = self.compile(b'')
        self.assertEqual((ok, records, lengths), (1, EMPTY * 8, [2] * 8))

    def test_single_char_press_release_terminator(self):
        self.assertEqual(self.macro(b'a'), rec(10, 0, [4]) + rec(10) + TERM)

    def test_different_keys_replace_without_gap_and_same_key_needs_one(self):
        self.assertEqual(self.macro(b'ab'), rec(10, 0, [4]) + rec(10, 0, [5]) + rec(10) + TERM)
        self.assertEqual(self.macro(b'aa'), rec(10, 0, [4]) + rec(10) + rec(10, 0, [4]) + rec(10) + TERM)

    def test_every_printable_ascii_character_maps_to_the_us_layout(self):
        plain = "abcdefghijklmnopqrstuvwxyz1234567890\n\x1b\x08\t -=[]\\\x00;'`,./"
        shifted = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()\x00\x00\x00\x00\x00_+{}|\x00:"~<>?'
        want = {}
        for i, (lo, hi) in enumerate(zip(plain, shifted)):         # usage 4 + i
            if lo not in '\x00\x1b\x08':
                want[ord(lo)] = (4 + i, 0)
            if hi != '\x00':
                want[ord(hi)] = (4 + i, 2)
        self.assertEqual(sorted(want), [9, 10] + list(range(0x20, 0x7f)))
        for ch in range(2, 256):
            if ch in want:
                key, mods = want[ch]
                self.assertEqual(self.macro(bytes([ch])), rec(10, mods, [key]) + rec(10) + TERM, ch)
            else:
                self.assertEqual(self.compile(bytes([ch]))[0], 0, ch)

    def test_uppercase_and_symbols_use_shift_only_for_that_key(self):
        self.assertEqual(self.macro(b'A'), rec(10, 2, [4]) + rec(10) + TERM)
        self.assertEqual(self.macro(b'Ab'), rec(10, 2, [4]) + rec(10, 0, [5]) + rec(10) + TERM)
        self.assertEqual(self.macro(b'!'), rec(10, 2, [0x1e]) + rec(10) + TERM)
        self.assertEqual(self.macro(b'0)'), rec(10, 0, [0x27]) + rec(10) + rec(10, 2, [0x27]) + rec(10) + TERM)  # same key: gap
        self.assertEqual(self.macro(b'\n'), rec(10, 0, [0x28]) + rec(10) + TERM)

    def test_explicit_down_tap_up_and_modifier_combo(self):
        data = bytes([1, 2, 0xe1, 1, 1, 4, 1, 3, 0xe1])
        self.assertEqual(self.macro(data), rec(10, 2) + rec(10, 2, [4]) + rec(10) + TERM)

    def test_delay_extends_previous_hold_and_leading_delay_makes_empty_state(self):
        self.assertEqual(self.macro(bytes([1, 1, 4, 1, 4]) + b'100|'), rec(110, 0, [4]) + rec(10) + TERM)
        self.assertEqual(self.macro(bytes([1, 4]) + b'50|a'), rec(50) + rec(10, 0, [4]) + rec(10) + TERM)

    def test_sixteen_bit_forms_accept_mod_plus_key(self):
        self.assertEqual(self.macro(bytes([1, 5, 4, 2])), rec(10, 2, [4]) + rec(10) + TERM)      # LSFT(KC_A)
        self.assertEqual(self.macro(bytes([1, 5, 0x29, 0x11])), rec(10, 0x10, [0x29]) + rec(10) + TERM)  # right Ctrl
        self.assertEqual(self.macro(bytes([1, 5, 0x29, 0x12])), rec(10, 0x20, [0x29]) + rec(10) + TERM)  # right Shift

    def test_multiple_macros_split_on_nul_and_report_lengths(self):
        ok, records, lengths = self.compile(b'a\0b\0')
        self.assertEqual(ok, 1)
        self.assertEqual(lengths, [3, 3, 2, 2, 2, 2, 2, 2])
        self.assertEqual(records[:3 * REC], rec(10, 0, [4]) + rec(10) + TERM)
        self.assertEqual(records[3 * REC:6 * REC], rec(10, 0, [5]) + rec(10) + TERM)

    def test_zero_delay_is_ignored_and_tiny_first_delay_is_raised(self):
        self.assertEqual(self.macro(bytes([1, 1, 4, 1, 4]) + b'0|'), rec(10, 0, [4]) + rec(10) + TERM)
        self.assertEqual(self.macro(bytes([1, 4]) + b'1|a'), rec(2) + rec(10, 0, [4]) + rec(10) + TERM)

    def test_incomplete_trailing_action_is_ignored(self):
        self.assertEqual(self.macro(b'a' + bytes([1, 4]) + b'12'), rec(10, 0, [4]) + rec(10) + TERM)
        self.assertEqual(self.macro(b'a' + bytes([1])), rec(10, 0, [4]) + rec(10) + TERM)
        self.assertEqual(self.macro(b'a' + bytes([1, 1])), rec(10, 0, [4]) + rec(10) + TERM)

    def test_rejections(self):
        for bad in (bytes([1, 1, 3]), bytes([1, 9, 4]), b'a\x07', bytes([1, 5, 0, 0x30]),
                    bytes([1, 2, 4, 1, 2, 5, 1, 2, 6, 1, 2, 7]),
                    bytes([1, 2, 0xe1, 1, 2, 4, 1, 2, 5, 1, 2, 6])):
            self.assertEqual(self.compile(bad)[0], 0, bad)

    def test_record_budget_is_enforced_atomically(self):
        ok, _, _ = self.compile(b'abcdefghij', max_records=24)       # needs 12 + 14 for the empty macros
        self.assertEqual(ok, 0)
        ok, records, lengths = self.compile(b'abc', max_records=24)
        self.assertEqual((ok, lengths[0]), (1, 5))

    def test_text_hello_world_record_count_is_about_one_per_char(self):
        ok, records, lengths = self.compile(b'Hello World')
        self.assertEqual(ok, 1)
        self.assertLessEqual(lengths[0], 11 + 5)


if __name__ == '__main__':
    unittest.main()
