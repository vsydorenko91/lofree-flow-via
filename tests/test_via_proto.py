import json
from pathlib import Path
import random
import struct
import sys
import unittest

from via_proto import Action, Codec, Endpoint, HERE, LAYERS, SLOTS, Unsupported, packet


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.e = Endpoint.stock()

    def get(self, layer, row, col):
        return self.e.handle(packet(4, layer, row, col))

    def set(self, code, layer=0, row=3, col=0):
        return self.e.handle(packet(5, layer, row, col, code >> 8, code & 255))

    def test_protocol_dictionary_and_layer_count_vectors(self):
        self.assertEqual(self.e.handle(packet(1)), packet(1, 0, 13))
        self.assertEqual(self.e.handle(packet(2, 6)), packet(2, 6, 0, 0, 0, 9))
        self.assertEqual(self.e.handle(packet(0x11)), packet(0x11, 4))

    def test_caps_to_ctrl_read_write_vectors(self):
        self.assertEqual(self.get(0, 3, 0), packet(4, 0, 3, 0, 0, 0x39))
        self.assertEqual(self.set(0xE0), packet(5, 0, 3, 0, 0, 0xE0))
        self.assertEqual(self.get(0, 3, 0), packet(4, 0, 3, 0, 0, 0xE0))
        self.assertEqual(self.e.actions[66].encode(), bytes.fromhex('01 01 00 53'))
        self.assertEqual(self.e.export_profile()['changes'],
                         [{'layer': 'win', 'index': 66, 'type': 1, 'value': 1}])

    def test_every_factory_slot_round_trips_without_losing_flags(self):
        self.assertEqual(len(self.e.actions), 528)
        for i, action in enumerate(self.e.original):
            code = self.e.codec.to_via(action, i // SLOTS)
            self.assertEqual(self.e.codec.from_via(code, i // SLOTS, action), action, (i, action, code))
        self.assertEqual(self.e.export_profile()['changes'], [])

    def test_all_modifier_bits_are_keyboard_modifiers(self):
        for bit in range(8):
            self.assertNotEqual(self.set(0xE0 + bit)[0], 0xFF)
            self.assertEqual(self.e.actions[66].semantic, (1, 1 << bit))

    def test_media_usages_are_translated_from_qmk_aliases(self):
        self.assertNotEqual(self.set(0xA8)[0], 0xFF)
        self.assertEqual(self.e.actions[66].semantic, (3, 0xE2))
        self.assertEqual(self.get(0, 3, 0)[4:6], bytes.fromhex('00 a8'))

    def test_left_modifier_combinations(self):
        # QMK C(S(KC_ESC)) -> stock combo lo=Ctrl|Shift, hi=Esc.
        self.assertNotEqual(self.set(0x0329)[0], 0xFF)
        self.assertEqual(self.e.actions[66].semantic, (7, 0x2903))
        self.assertEqual(self.get(0, 3, 0)[4:6], bytes.fromhex('03 29'))

    def test_unsupported_layer_tap_transparent_and_cross_os_layers_rejected(self):
        before = list(self.e.actions)
        for code in (1, 0x2004, 0x4229, 0x4329, 0x4029, 0x4100, 0x5223, 0x5261, 0x7708, 0x10E0, 0x1129, 0xFFFF):
            self.assertEqual(self.set(code)[0], 0xFF, hex(code))
            self.assertEqual(self.e.actions, before)

    def test_all_basic_qmk_keycodes_up_to_a4_are_accepted_and_a5_rejected(self):
        for code in (0x66, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F, 0x85, 0xA4):      # incl. Cut/Copy/Paste, Mute
            self.assertNotEqual(self.set(code)[0], 0xFF, hex(code))
            self.assertEqual(self.get(0, 3, 0)[4:6], bytes.fromhex(f'00 {code:02x}'))
        for code in (0xA5, 0xA6, 0xDF):   # 0xA8.. are the media codes handled separately
            self.assertEqual(self.set(code)[0], 0xFF, hex(code))

    def test_layer_tap_and_macro_keycodes_roundtrip_on_supported_entries(self):
        self.assertNotEqual(self.set(0x4129, layer=0)[0], 0xFF)
        self.assertEqual(self.e.actions[66].semantic, (11, 0x4129))
        self.assertEqual(self.get(0, 3, 0)[4:6], bytes.fromhex('41 29'))
        self.assertEqual(self.set(0x4129, layer=1)[0], 0xFF)          # Fn bank has no LT
        self.assertEqual(self.set(0x4329, layer=0)[0], 0xFF)          # wrong Fn layer for Win
        self.assertNotEqual(self.set(0x4329, layer=2)[0], 0xFF)
        for n in range(8):
            self.assertNotEqual(self.set(0x7700 + n)[0], 0xFF)
            self.assertEqual(self.e.actions[66].semantic, (9, 6 + n))
            self.assertEqual(self.get(0, 3, 0)[4:6], bytes.fromhex(f'77 {n:02x}'))

    def test_mod_tap_marker_roundtrip_masks_and_metadata(self):
        original = self.e.original[1]
        for mask in range(1, 32):
            if mask == 16:
                continue  # Right-hand flag alone is no modifier.
            for tap in (4, 0x29, 0x65, 0x85, 0x87, 0x8B, 0x90, 0x91):
                code = 0x2000 | mask << 8 | tap
                action = self.e.codec.from_via(code, 0, original)
                self.assertEqual(action.kind, 10 | (original.kind & 0x80))
                self.assertEqual(action.value, code)
                self.assertEqual(sum(action.encode()) & 255, 0x55)
                self.assertEqual(self.e.codec.to_via(action, 0), code)
        self.assertEqual(len(self.e.codec.opaque), 10)
        self.assertEqual(len(self.e.custom_keycodes()), 10)

    def test_mod_tap_rejects_empty_masks_and_nonkeyboard_taps(self):
        for code in (0x2000, 0x2004, 0x3004, 0x2100, 0x2101, 0x2103,
                     0x21A5, 0x21A8, 0x21E0, 0x3FFF):
            before = list(self.e.actions)
            self.assertEqual(self.set(code)[0], 0xFF, hex(code))
            self.assertEqual(self.e.actions, before)

    def test_fn_layer_is_relative_to_hardware_os_bank(self):
        for layer in range(4):
            code = 0x5221 if layer < 2 else 0x5223
            self.assertNotEqual(self.set(code, layer=layer)[0], 0xFF)
            self.assertEqual(self.e.actions[layer * SLOTS + 66].semantic, (6, 2))

    def test_opaque_factory_actions_cannot_be_moved(self):
        i = next(i for i, a in enumerate(self.e.original) if a.semantic[0] == 5)
        code = self.e.codec.to_via(self.e.original[i], i // SLOTS)
        self.assertTrue(0x7E00 <= code < 0x7E20)
        self.assertEqual(self.set(code)[0], 0xFF)
        layer, idx = divmod(i, SLOTS)
        row, col = divmod(idx, 22)
        self.assertNotEqual(self.set(code, layer, row, col)[0], 0xFF)

    def test_entry_bit7_flags_are_preserved_when_assigning_ordinary_keys(self):
        i = next(i for i, a in enumerate(self.e.original) if a.kind & 0x80)
        layer, idx = divmod(i, SLOTS)
        row, col = divmod(idx, 22)
        self.set(0xE0, layer, row, col)
        self.assertEqual(self.e.actions[i].kind, 0x81)
        self.assertEqual(sum(self.e.actions[i].encode()) & 255, 0x55)

    def test_bulk_get_uses_big_endian_words_and_byte_offsets(self):
        raw = self.e.keycode_bytes()
        self.assertEqual(len(raw), 1056)
        for offset in (0, 1, 27, 131, 264, 1028):
            response = self.e.handle(packet(0x12, offset >> 8, offset & 255, 28))
            self.assertEqual(response[4:32], raw[offset:offset + 28])

    def test_bulk_write_atomic_when_one_code_is_unsupported(self):
        original = list(self.e.actions)
        offset = 66 * 2
        # Left Ctrl followed by MT with no modifier: neither slot may change.
        req = packet(0x13, offset >> 8, offset & 255, 4, 0, 0xE0, 0x20, 0x04)
        self.assertEqual(self.e.handle(req)[0], 0xFF)
        self.assertEqual(self.e.actions, original)

    def test_bulk_write_allows_odd_offset_after_validating_resulting_word(self):
        offset = 66 * 2 + 1
        self.assertNotEqual(self.e.handle(packet(0x13, 0, offset, 1, 0xE0))[0], 0xFF)
        self.assertEqual(self.e.actions[66].semantic, (1, 1))

    def test_full_keymap_upload_keeps_stock_actions_exactly(self):
        raw = self.e.keycode_bytes()
        for offset in range(0, len(raw), 28):
            chunk = raw[offset:offset + 28]
            reply = self.e.handle(packet(0x13, offset >> 8, offset & 255, len(chunk), *chunk))
            self.assertEqual(reply[0], 0x13)
        self.assertEqual(tuple(self.e.actions), self.e.original)

    def test_invalid_layer_matrix_and_buffer_bounds(self):
        for request in (packet(4, 4, 0, 0), packet(4, 0, 6, 0), packet(5, 0, 0, 22, 0, 4),
                        packet(0x12, 0, 0, 29), packet(0x13, 4, 31, 2, 0, 4),
                        packet(2, 3, 6)):
            self.assertEqual(self.e.handle(request)[0], 0xFF)
        self.assertEqual(tuple(self.e.actions), self.e.original)

    def test_reset_is_model_only_and_macros_are_not_claimed(self):
        self.set(0xE0)
        self.e.handle(packet(6))
        self.assertEqual(tuple(self.e.actions), self.e.original)
        self.assertEqual(self.e.handle(packet(0x0C)), packet(0x0C, 0))
        self.assertEqual(self.e.handle(packet(0x0D)), packet(0x0D, 0, 0))
        for command in (0x0A, 0x0B, 0x0E, 0x0F, 0x10, 0x14, 0x15):
            self.assertEqual(self.e.handle(packet(command))[0], 0xFF)

    def test_malformed_reports_rejected_without_mutation(self):
        for data in (b'', b'\0' * 31, b'\0' * 33, bytearray(32)):
            with self.assertRaises(ValueError): self.e.handle(data)
        self.assertEqual(tuple(self.e.actions), self.e.original)

    def test_uptime_wrap_and_no_live_matrix_claim(self):
        values = iter((500, 500 + 0x100000002))
        e = Endpoint(list(self.e.original), clock=lambda: next(values))
        self.assertEqual(e.handle(packet(2, 1)), packet(2, 1, 0, 0, 0, 2))
        self.assertEqual(self.e.handle(packet(2, 3, 0))[3:21], b'\0' * 18)

    def test_random_valid_reports_do_not_escape_bounds(self):
        r = random.Random(915)
        for _ in range(1000):
            request = r.randbytes(32)
            reply = self.e.handle(request)
            self.assertEqual(len(reply), 32)
            self.assertEqual(len(self.e.actions), 528)
        for action in self.e.actions: Action.decode(action.encode())

    def test_draft_definition_has_84_unique_bounded_coordinates(self):
        definition = json.loads((HERE / 'definitions/flow84.json').read_text())
        coordinates = [key for row in definition['layouts']['keymap'] for key in row if isinstance(key, str)]
        self.assertEqual(len(coordinates), 84)
        self.assertEqual(len(set(coordinates)), 84)
        for key in coordinates:
            row, col = map(int, key.split(','))
            self.assertTrue(0 <= row < 6 and 0 <= col < 22)
        self.assertEqual(definition['matrix'], {'rows': 6, 'cols': 22})
        self.assertEqual(len(definition['customKeycodes']), 10)


if __name__ == '__main__': unittest.main()
