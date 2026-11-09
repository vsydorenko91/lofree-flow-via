"""Delta store (keys + settings + blob) against a NOR model with power cut at every byte."""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SLOTS, REC, AREA, MAX = 17, 28, 512, 85
NO_LEVEL, BLOB_MAX = 0xff, 192
U8 = C.c_uint8
READ = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint16, C.c_uint16, C.POINTER(U8))
ERASE = C.CFUNCTYPE(C.c_int, C.c_void_p)
PROGRAM = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint16, C.c_uint16, C.POINTER(U8))
FACTORY = C.CFUNCTYPE(C.c_uint16, C.c_void_p, C.c_uint)


class Entry(C.Structure):
    _fields_ = [('pos', C.c_uint16), ('code', C.c_uint16)]


class State(C.Structure):
    _fields_ = [('entries', Entry * MAX), ('count', C.c_uint), ('level', U8), ('light', U8),
                ('blob', U8 * BLOB_MAX), ('blob_len', C.c_uint), ('next', C.c_uint)]


class Backend(C.Structure):
    _fields_ = [('user', C.c_void_p), ('read', READ), ('erase', ERASE), ('program', PROGRAM), ('factory', FACTORY)]


def factory_code(pos):
    return (pos * 7 + 1) & 0x7fff


class Flash:
    def __init__(self, lib):
        self.lib = lib
        self.data = bytearray(b'\xff' * AREA)
        self.budget = None      # remaining programmable bytes before the "power cut"
        self.dead = False
        self.log = []
        self.errors = []

        def rd(_, off, n, out):
            if self.dead:
                return 0
            if off + n > SLOTS * REC or off % 4 or n % 4:
                self.errors.append('bad read')
                return 0
            for i in range(n):
                out[i] = self.data[off + i]
            return 1

        def er(_):
            if self.dead:
                return 0
            self.log.append('erase')
            if self.budget is not None:
                if self.budget <= 0:
                    self.dead = True
                    return 0
                self.budget -= 1
            self.data[:] = b'\xff' * AREA
            return 1

        def pr(_, off, n, data):
            if self.dead:
                return 0
            if off + n > SLOTS * REC or off % 4 or n % 4 or n > 32:
                self.errors.append('bad program')
                return 0
            self.log.append(('program', off, n))
            for i in range(n):
                if self.budget is not None:
                    if self.budget <= 0:
                        self.dead = True
                        return 0
                    self.budget -= 1
                if data[i] & self.data[off + i] != data[i]:
                    self.errors.append('NOR 0->1')
                    self.dead = True
                    return 0
                self.data[off + i] = data[i]
            return 1

        self.cb = (READ(rd), ERASE(er), PROGRAM(pr), FACTORY(lambda _, pos: factory_code(pos)))
        self.be = Backend(None, *self.cb)

    def load(self):
        st = State()
        n = self.lib.ds_load(C.byref(self.be), C.byref(st))
        entries = {st.entries[i].pos: st.entries[i].code for i in range(st.count)}
        return n, entries, st.level, st.light, bytes(st.blob[:st.blob_len]), st.next

    def save(self, entries, level=2, light=0x80, blob=b''):
        want = (Entry * max(len(entries), 1))(*[Entry(p, c) for p, c in sorted(entries.items())])
        buf = (U8 * max(len(blob), 1))(*blob)
        return self.lib.ds_save(C.byref(self.be), want, len(entries), level, light, buf, len(blob), C.byref(State()))


class DeltaStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        so = Path(cls.tmp.name) / 'dstore.so'
        subprocess.run(['clang', '-shared', '-fPIC', '-O1', '-Wall', '-Wextra', '-Werror',
                        '-I', str(ROOT / 'firmware'), str(ROOT / 'firmware/dstore.c'), '-o', str(so)], check=True)
        cls.lib = C.CDLL(str(so))
        cls.lib.ds_load.argtypes = [C.POINTER(Backend), C.POINTER(State)]
        cls.lib.ds_save.argtypes = [C.POINTER(Backend), C.POINTER(Entry), C.c_uint, C.c_uint8, C.c_uint8,
                                    C.POINTER(U8), C.c_uint, C.POINTER(State)]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def flash(self):
        return Flash(self.lib)

    def test_empty_loads_nothing(self):
        n, entries, level, light, blob, nxt = self.flash().load()
        self.assertEqual((n, entries, level, light, blob, nxt), (0, {}, NO_LEVEL, 0, b'', 0))

    def test_round_trip_unchanged_and_incremental(self):
        f = self.flash()
        a = {3: 0x0004, 70: 0x2129}
        self.assertEqual(f.save(a, 3, 0x80), 1)
        n, entries, level, light, blob, nxt = f.load()
        self.assertEqual((entries, level, light, nxt, n), (a, 3, 0x80, 1, 1))
        before = len(f.log)
        self.assertEqual(f.save(a, 3, 0x80), 2)               # UNCHANGED: no flash access
        self.assertEqual(len(f.log), before)
        b = {**a, 3: 0x0005, 100: 0x0010, 200: 0x0011}
        self.assertEqual(f.save(b, 3, 0x80), 1)
        self.assertEqual(f.log[-1][1], REC)                    # appended after the first save
        self.assertEqual(f.load()[1], b)
        self.assertEqual(f.errors, [])

    def test_revert_to_factory_level_and_light_only_changes(self):
        f = self.flash()
        f.save({3: 0x0004}, 2, 0x80)
        self.assertEqual(f.save({}, 2, 0x80), 1)
        n, entries, level, light, _, _ = f.load()
        self.assertEqual(entries.get(3), factory_code(3))      # explicit factory code recorded
        self.assertEqual(f.save({}, 4, 0x80), 1)
        self.assertEqual(f.load()[2], 4)
        self.assertEqual(f.save({}, 4, 0x80 | 2 << 3 | 5), 1)
        self.assertEqual(f.load()[3], 0x80 | 2 << 3 | 5)
        self.assertEqual(f.save({}, 4, 0x80 | 2 << 3 | 5), 2)

    def test_multi_record_save_and_overflow_refused_before_erase(self):
        f = self.flash()
        big = {i: 0x1000 + i for i in range(MAX)}               # exactly 17 records
        self.assertEqual(f.save(big), 1)
        self.assertEqual(f.load()[1], big)
        snapshot = bytes(f.data)
        too = {i: 0x1000 + i for i in range(MAX + 1)}
        self.assertEqual(f.save(too), -2)                       # DS_FULL
        self.assertNotIn('erase', f.log)
        self.assertEqual(bytes(f.data), snapshot)

    def test_disjoint_old_and_new_sets_cannot_overflow_ops_buffer(self):
        f = self.flash()
        old = {i: 0x1000 + i for i in range(MAX)}
        f.save(old)
        new = {i + 200: 0x2000 + i for i in range(MAX)}         # 170 operations needed
        self.assertEqual(f.save(new), 1)                        # compacts instead
        self.assertEqual(f.load()[1], new)
        self.assertEqual(f.errors, [])

    def test_compaction_when_log_is_full(self):
        f = self.flash()
        cur = {}
        for k in range(17):
            cur[k] = 0x2000 + k
            self.assertEqual(f.save(cur), 1)
        self.assertEqual(f.load()[5], 17)
        cur[400] = 0x3000
        self.assertEqual(f.save(cur), 1)
        self.assertIn('erase', f.log)
        n, entries, level, light, _, nxt = f.load()
        self.assertEqual((entries, level, light, nxt), (cur, 2, 0x80, 4))   # 18 keys -> 4 records
        self.assertEqual(f.errors, [])

    def test_blob_round_trip_change_shrink_and_clear(self):
        f = self.flash()
        macro = bytes(range(1, 60))
        self.assertEqual(f.save({}, 2, 0x80, macro), 1)
        self.assertEqual(f.load()[4], macro)
        self.assertEqual(f.save({}, 2, 0x80, macro), 2)
        longer = macro + b'xyz'
        self.assertEqual(f.save({}, 2, 0x80, longer), 1)
        self.assertEqual(f.load()[4], longer)
        self.assertEqual(f.save({}, 2, 0x80, b'q'), 1)
        self.assertEqual(f.load()[4], b'q')
        self.assertEqual(f.save({}, 2, 0x80, b''), 1)
        self.assertEqual(f.load()[4], b'')
        self.assertEqual(f.errors, [])

    def test_blob_and_keys_share_one_atomic_save_and_compact_together(self):
        f = self.flash()
        a = {5: 0x0004, 6: 0x0005}
        self.assertEqual(f.save(a, 3, 0x80, b'hello\0world'), 1)
        self.assertEqual(f.load()[1:5], (a, 3, 0x80, b'hello\0world')[0:1] + (3, 0x80, b'hello\0world'))
        for k in range(15):                                       # fill the log, then force compaction
            a = {**a, 10 + k: 0x2000 + k}
            f.save(a, 3, 0x80, b'hello\0world')
        n, entries, level, light, blob, _ = f.load()
        self.assertEqual((entries, blob), (a, b'hello\0world'))

    def test_power_cut_at_every_byte_never_mixes_states(self):
        old_keys = {5: 0x0004, 6: 0x0005, 7: 0x0006}
        old_blob = b'old text\0'
        scenarios = []

        def prep_small():
            f = self.flash()
            self.assertEqual(f.save(old_keys, 1, 0x80, old_blob), 1)
            return f
        new_inc = ({**old_keys, **{i: 0x4000 + i for i in range(20, 33)}}, 2, 0x81, b'new macro text that is rather long\0')
        scenarios.append((prep_small, new_inc))

        def prep_full():
            f = self.flash()
            cur = {}
            for k in range(17):
                cur[k] = 0x2000 + k
                f.save(cur, 1, 0x80, old_blob)
            return f
        full = {k: 0x2000 + k for k in range(17)}
        new_compact = ({**full, 300: 0x5555, 301: 0x5556}, 2, 0x81, b'compacted\0')
        scenarios.append((prep_full, new_compact))
        for prepare, (keys, level, light, blob) in scenarios:
            probe = prepare()
            old_state = probe.load()[1:5]
            probe.log.clear()
            self.assertEqual(probe.save(keys, level, light, blob), 1)
            cost = sum(x[2] if isinstance(x, tuple) else 1 for x in probe.log)
            new_state = (keys, level, light, blob)
            for cut in range(cost + 1):
                f = prepare()
                f.budget = cut
                f.save(keys, level, light, blob)
                f.dead = False
                f.budget = None
                got = f.load()[1:5]
                self.assertIn(got, (old_state, new_state, ({}, NO_LEVEL, 0, b'')), f'mixed state after cut at {cut}')
                self.assertEqual(f.errors, [])
                self.assertIn(f.save(keys, level, light, blob), (1, 2))   # recovery from any interruption
                self.assertEqual(f.load()[1:5], new_state)

    def test_torn_record_voids_only_its_own_save(self):
        f = self.flash()
        a = {1: 0x0004}
        f.save(a)
        b = {**a, 2: 0x0005}
        f.save(b)
        f.data[REC + 9] ^= 0x01                                   # corrupt save 2 (CRC fails)
        self.assertEqual(f.load()[1], a)
        c = {**a, 3: 0x0006}
        self.assertEqual(f.save(c), 1)                            # appends after the torn slot
        self.assertEqual(f.load()[1], c)

    def test_entry_with_out_of_range_position_is_rejected(self):
        f = self.flash()
        f.save({1: 0x0004})
        rec = bytearray(f.data[:REC])
        rec[8], rec[9] = 0x10, 0x02                               # position 528
        crc = 0xffff
        for i, byte in enumerate(rec):
            if i in (6, 7):
                continue
            crc ^= byte << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xffff if crc & 0x8000 else (crc << 1) & 0xffff
        rec[6], rec[7] = crc & 255, crc >> 8
        f.data[:REC] = rec
        self.assertEqual(f.load()[1], {})


if __name__ == '__main__':
    unittest.main()
