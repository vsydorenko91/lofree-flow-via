"""Native firmware core/reference differential tests. No USB access."""
import ctypes as C
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
import via_proto as ref

U8 = C.c_uint8
READ = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint16, C.c_int, C.POINTER(U8))
COMMIT = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint16, U8, C.POINTER(U8))
GUARD = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_uint16, C.POINTER(U8))
RESET = C.CFUNCTYPE(C.c_int, C.c_void_p)


class Native:
    def __init__(self, lib, original=None):
        self.original = list(original or ref.Endpoint.stock().original)
        self.actions = list(self.original)
        self.blocked = set()
        self.inactive = set()
        self.commits = 0
        self.lib = lib
        self.state = (C.c_uint64 * 64)()

        def read(_, index, original, out):
            if index >= 528 or (not original and index // 132 in self.inactive):
                return 0
            raw = (self.original if original else self.actions)[index].encode()
            for j, b in enumerate(raw):
                out[j] = b
            return 1

        def commit(_, first, count, entries):
            if first + count > 528 or any(i // 132 in self.inactive for i in range(first, first + count)):
                return 0
            candidates = [ref.Action.decode(bytes(entries[j * 4 + k] for k in range(4))) for j in range(count)]
            self.actions[first:first + count] = candidates
            self.commits += 1
            return 1

        def guard(_, index, entry):
            return index not in self.blocked and index // 132 not in self.inactive

        def reset(_):
            if self.blocked or self.inactive:
                return 0
            self.actions = list(self.original)
            return 1

        self.callbacks = READ(read), COMMIT(commit), GUARD(guard), RESET(reset)
        if not lib.flow_via_init(self.state, None, *self.callbacks, 0):
            raise ValueError('invalid factory keymap')

    def handle(self, packet, now=123):
        request = (U8 * len(packet)).from_buffer_copy(packet)
        reply = (U8 * 32)(*([0xAA] * 32))
        result = self.lib.flow_via_handle(self.state, request, len(packet), reply, now)
        return bytes(reply) if result else None


class ProtocolNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        path = Path(cls.tmp.name) / 'protocol.dylib'
        subprocess.run(['clang', '-std=c99', '-Wall', '-Wextra', '-Werror', '-O2',
                        '-ffreestanding', '-fno-builtin', '-dynamiclib',
                        str(ROOT / 'firmware/protocol.c'), '-o', str(path)], check=True)
        cls.lib = C.CDLL(str(path))
        cls.lib.flow_via_init.argtypes = [C.c_void_p, C.c_void_p, READ, COMMIT, GUARD, RESET, C.c_uint32]
        cls.lib.flow_via_init.restype = C.c_int
        cls.lib.flow_via_handle.argtypes = [C.c_void_p, C.POINTER(U8), C.c_size_t, C.POINTER(U8), C.c_uint32]
        cls.lib.flow_via_handle.restype = C.c_int
        cls.lib.flow_via_is_mod_tap.argtypes = [C.c_uint16]
        cls.lib.flow_via_is_mod_tap.restype = C.c_int

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.native = Native(self.lib)
        self.clock = 0
        self.model = ref.Endpoint(list(self.native.original), lambda: self.clock)

    def compare(self, packet, now=123):
        self.clock = now
        self.assertEqual(self.native.handle(packet, now), self.model.handle(packet), packet.hex())
        self.assertEqual(self.native.actions, self.model.actions, packet.hex())

    def test_all_528_factory_roundtrips(self):
        for i, action in enumerate(self.model.original):
            layer, slot = divmod(i, 132)
            row, col = divmod(slot, 22)
            self.compare(ref.packet(4, layer, row, col))
            code = self.model.codec.to_via(action, layer)
            self.compare(ref.packet(5, layer, row, col, code >> 8, code & 255))
        self.assertEqual(self.native.actions, list(self.model.original))

    def test_protocol_and_commands(self):
        for command in range(256):
            self.compare(ref.packet(command))
        for value_id in range(9):
            self.compare(ref.packet(2, value_id), 0xfffffff0)
        for row in range(8):
            self.compare(ref.packet(2, 3, row))
        self.compare(ref.packet(3, 5, 7))
        self.compare(ref.packet(3, 2, 0, 0, 0, 1))
        self.compare(ref.packet(2, 1), 0x100000010)

    def test_mutations_and_rejections(self):
        for layer in range(4):
            for code in [0,4,0x65,0xe0,0xe7,0xa8,0xbe,0x0300,0x1100,0x0304,
                         0x1104,0x1000,1,0x2004,0x4004,0x5221,0x5223,0xffff]:
                self.compare(ref.packet(5, layer, 0, 1, code >> 8, code & 255))
        # Opaque codes cannot be copied elsewhere, but can be restored in place.
        for code in self.model.codec.opaque:
            self.compare(ref.packet(5, 0, 0, 0, code >> 8, code & 255))
        self.compare(ref.packet(6))

    def test_bulk_all_bytes_and_odd_writes(self):
        raw = self.model.keycode_bytes()
        for offset in range(0, len(raw), 27):
            part = raw[offset:offset+27]
            self.compare(ref.packet(0x12, offset >> 8, offset & 255, len(part)))
            self.compare(ref.packet(0x13, offset >> 8, offset & 255, len(part), *part))
        for offset, size in [(0,0),(1,0),(1055,0),(1056,0),(1056,1),(1057,0),(1055,2),(0,29)]:
            self.compare(ref.packet(0x12,offset>>8,offset&255,size))
            self.compare(ref.packet(0x13,offset>>8,offset&255,size))
        # Valid first word followed by unsupported KC_TRNS is atomic.
        self.compare(ref.packet(0x13,0,0,4,0,4,0,1))
        self.compare(ref.packet(0x13,0,1,1,4))

    def test_every_16bit_keycode_in_both_os_banks(self):
        # Exhaust the host-controlled keycode space, including original Fn-lock
        # metadata, rather than relying only on random (usually invalid) codes.
        for layer in (0, 2):
            index = layer * 132 + 1
            for code in range(65536):
                packet = ref.packet(5, layer, 0, 1, code >> 8, code & 255)
                actual = self.native.handle(packet)
                expected = self.model.handle(packet)
                if actual != expected or self.native.actions[index] != self.model.actions[index]:
                    self.fail(f'keycode {code:04x} layer {layer}: native/reference mismatch')

    def test_mod_tap_all_masks_and_tap_usage_boundaries(self):
        # Exercise every MT field, including right-hand flag-only (invalid),
        # with tap boundaries, reserved usages and consumer/modifier aliases.
        for mask in range(32):
            for tap in (0, 1, 3, 4, 0x29, 0x65, 0x66, 0x85, 0x86,
                        0x87, 0x8B, 0x8C, 0x90, 0x91, 0x92, 0xA8, 0xE0, 0xFF):
                code = 0x2000 | mask << 8 | tap
                valid = bool(mask & 15) and tap in ref.SUPPORTED_KEYBOARD_USAGES
                self.assertEqual(bool(self.lib.flow_via_is_mod_tap(code)), valid)
                request = ref.packet(5, 0, 0, 1, code >> 8, code & 255)
                before = list(self.native.actions)
                self.compare(request)
                if valid:
                    action = self.native.actions[1]
                    self.assertEqual(action.kind, 0x8A)  # Original Fn-lock retained.
                    self.assertEqual(action.value, code)
                    self.assertEqual(sum(action.encode()) & 255, 0x55)
                    self.compare(ref.packet(4, 0, 0, 1))
                    self.assertEqual(self.native.handle(ref.packet(4, 0, 0, 1))[4:6],
                                     bytes([code >> 8, code & 255]))
                else:
                    self.assertEqual(self.native.actions, before)
        self.assertEqual(len(self.model.codec.opaque), 10)

    def test_mod_tap_bulk_atomicity_and_guard_rejection(self):
        before = list(self.native.actions)
        # Valid MT then invalid MT consumer tap: neither marker may commit.
        self.compare(ref.packet(0x13, 0, 0, 4, 0x21, 0x29, 0x21, 0xA8))
        self.assertEqual(self.native.actions, before)
        self.assertEqual(self.native.commits, 0)
        # Both valid, with opposite modifier sides, preserve full MT codes.
        self.compare(ref.packet(0x13, 0, 0, 4, 0x21, 0x29, 0x31, 0x04))
        self.assertEqual(self.native.actions[0].semantic, (10, 0x2129))
        self.assertEqual(self.native.actions[1].semantic, (10, 0x3104))
        before = list(self.native.actions)
        commits = self.native.commits
        # Adapter guard veto must leave even the preceding valid MT unchanged.
        self.native.blocked.add(1)
        self.assertEqual(self.native.handle(ref.packet(0x13, 0, 0, 4, 0x22, 0x29, 0x32, 0x04))[0], 0xFF)
        self.assertEqual(self.native.actions, before)
        self.assertEqual(self.native.commits, commits)

    def test_reserved_slots_and_inactive_bank_atomicity(self):
        self.native.blocked.add(1)
        before = list(self.native.actions)
        self.assertEqual(self.native.handle(ref.packet(0x13,0,0,4,0,4,0,5))[0],255)
        self.assertEqual(self.native.actions,before)
        self.assertEqual(self.native.commits,0)
        self.assertEqual(self.native.handle(ref.packet(6))[0],255)
        self.native.inactive.add(2)
        self.assertEqual(self.native.handle(ref.packet(4,2,0,0))[0],255)
        self.assertEqual(self.native.handle(ref.packet(5,2,0,0,0,4))[0],255)
        self.assertEqual(self.native.actions,before)

    def test_malformed_lengths_and_aliasing(self):
        for size in [0,1,31,33,255]:
            self.assertIsNone(self.native.handle(bytes(size)))
        same = (U8*32)(1,*range(1,32))
        self.assertEqual(self.lib.flow_via_handle(self.native.state,same,32,same,123),1)
        self.assertEqual(bytes(same),self.model.handle(bytes([1,*range(1,32)])))

    def test_random_differential_requests(self):
        rng=random.Random(915)
        for _ in range(3000):
            raw=bytearray(rng.randbytes(32))
            raw[0]=rng.choice([1,2,3,4,5,6,0xc,0xd,0x11,0x12,0x13,0xff])
            if raw[0] in [4,5]:
                raw[1:4]=bytes([rng.randrange(5),rng.randrange(7),rng.randrange(23)])
            if raw[0] in [0x12,0x13]:
                offset=rng.randrange(1060)
                raw[1:4]=bytes([offset>>8,offset&255,rng.randrange(32)])
            self.compare(bytes(raw))

    def test_fixed_factory_codes_do_not_shift_when_an_action_is_absent(self):
        original = list(self.model.original)
        # Remove every original opaque action except macro4. Dynamic sorting
        # used to number that sole remaining action custom0; pinned ABI is9.
        for i, action in enumerate(original):
            if action.semantic in ref.FACTORY_ACTIONS and action.semantic != (9, 4):
                original[i] = ref.Action(2 | (action.kind & 0x80), 4)
        native = Native(self.lib, original)
        model = ref.Endpoint(original)
        for i, action in enumerate(original):
            if action.semantic == (9, 4):
                layer, slot = divmod(i, 132)
                row, col = divmod(slot, 22)
                request = ref.packet(4, layer, row, col)
                self.assertEqual(native.handle(request), model.handle(request))
                self.assertEqual(native.handle(request)[4:6], bytes.fromhex('7e09'))
        self.assertEqual(tuple(model.codec.opaque.values()), ref.FACTORY_ACTIONS)

    def test_every_fixed_factory_code_retains_only_original_slot(self):
        for number, semantic in enumerate(ref.FACTORY_ACTIONS):
            code = 0x7E00 + number
            i = next(i for i, action in enumerate(self.model.original) if action.semantic == semantic)
            layer, slot = divmod(i, 132)
            row, col = divmod(slot, 22)
            self.assertEqual(self.model.codec.to_via(self.model.original[i], layer), code)
            self.compare(ref.packet(5, layer, row, col, 0, 4))
            self.compare(ref.packet(5, layer, row, col, code >> 8, code & 255))
            self.assertEqual(self.native.actions[i], self.model.original[i])
            self.compare(ref.packet(5, 0, 0, 0, code >> 8, code & 255))
        self.compare(ref.packet(5, 0, 0, 0, 0x7E, 0x0A))

    def test_unknown_factory_actions_fail_initialization(self):
        # A single new action must fail; never allocate another custom code or
        # truncate the value to one byte and accidentally match a known action.
        for action in (ref.Action(5, 3), ref.Action(5, 0x100),
                       ref.Action(9, 5), ref.Action(4, 0), ref.Action(8, 0)):
            original = list(self.model.original)
            original[0] = action
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    Native(self.lib, original)
                with self.assertRaises(ValueError):
                    ref.Endpoint(original)

    def test_unknown_live_action_cannot_alias_a_fixed_factory_code(self):
        action = ref.Action(5, 0x100)
        self.native.actions[0] = self.model.actions[0] = action
        request = ref.packet(4, 0, 0, 0)
        self.compare(request)
        self.assertEqual(self.native.handle(request)[0], 0xFF)


if __name__ == '__main__':
    unittest.main()
