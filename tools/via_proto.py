#!/usr/bin/env python3
"""Offline VIA protocol-13 reference endpoint for OE915; never opens USB."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys
import time

HERE = Path(__file__).resolve().parents[1]
LAYERS = ('win', 'winfn', 'mac', 'macfn')
ROWS, COLS, SLOTS = 6, 22, 132
SUPPORTED_KEYBOARD_USAGES = set(range(4, 0xA5))   # all QMK basic keycodes; OEM boot-report path carries them (emulator-verified)
TABLES_SHA256 = '0f596e7444fb075124925443711a64618078afee21aabe0e64fb08fda2e1b056'
REPORT_SIZE = 32
PROTOCOL_VERSION = 13
KEYCODES_VERSION = 9  # QMK keycode dictionary 0.0.9, checked 2026-11-08
STOCK_CONTAINER_SHA256 = '7c061d9ce66c0b45e7d9f02e5d467af78cd537a88529b90fccb5dbf6dbccc7de'
STOCK_PAYLOAD_SHA256 = '783b524191d4a165371600b66b528d9348e3152bb12ce94d156f2c9e35e20b7f'
# QMK basic consumer aliases -> USB Consumer usage, not Keyboard-page usages.
CONSUMERS = {0xA8: 0xE2, 0xA9: 0xE9, 0xAA: 0xEA, 0xAB: 0xB5,
             0xAC: 0xB6, 0xAE: 0xCD, 0xB2: 0x192, 0xB4: 0x221,
             0xBD: 0x6F, 0xBE: 0x70}


# Presentation only. Values/ordering remain the original sorted opaque codec.
# Source evidence and confidence limits are recorded in docs/MEMORY.md.
FACTORY_LABELS = {
    (5, 0x00): ('Bluetooth 1', 'BT1', 'Stock Bluetooth slot 1; hold may enter pairing.'),
    (5, 0x01): ('Bluetooth 2', 'BT2', 'Stock Bluetooth slot 2; hold may enter pairing.'),
    (5, 0x02): ('Bluetooth 3', 'BT3', 'Stock Bluetooth slot 3; hold may enter pairing.'),
    (5, 0x04): ('Factory lighting mode (unverified)', 'Mode?', 'Firmware cycles a 3-state index. Physical effect on this white-only Flow84 is unverified. Stock handler saves settings.'),
    (5, 0x07): ('Factory color index (unsupported)', 'Color?', 'Firmware-only color index 0..7; no RGB key backlight on this white-only Flow84. Physical side-light behavior is unverified. Stock handler saves settings.'),
    (5, 0x0F): ('Backlight brightness up', 'BL+', 'Raises stock brightness index up to 4. Stock handler saves settings.'),
    (5, 0x10): ('Backlight brightness down', 'BL-', 'Lowers stock brightness index down to 0. Stock handler saves settings.'),
    (5, 0x18): ('Windows mode', 'WinOS', 'Selects stock Windows OS bank. Stock handler changes saved settings.'),
    (5, 0x19): ('Mac mode', 'MacOS', 'Selects stock Mac OS bank. Stock handler changes saved settings.'),
    (9, 0x04): ('Factory macro 4: Win+Tab', 'WinTab', 'Plays the existing stock Win+Tab macro; this is not an editable VIA macro.'),
}


# Fixed ABI with firmware/protocol.c, independent of presentation labels.
FACTORY_ACTIONS = ((5, 0), (5, 1), (5, 2), (5, 4), (5, 7),
                   (5, 15), (5, 16), (5, 24), (5, 25), (9, 4))


class Unsupported(ValueError):
    """The stock action engine cannot represent this VIA request faithfully."""


@dataclass(frozen=True)
class Action:
    kind: int
    value: int

    @classmethod
    def decode(cls, raw: bytes) -> 'Action':
        if len(raw) != 4 or sum(raw) & 255 != 0x55:
            raise ValueError('bad OE915 keymap entry checksum')
        return cls(raw[0], raw[1] | raw[2] << 8)

    def encode(self) -> bytes:
        if not 0 <= self.kind <= 255 or not 0 <= self.value <= 65535:
            raise ValueError('action outside byte/word bounds')
        lo, hi = self.value & 255, self.value >> 8
        return bytes((self.kind, lo, hi, (0x55 - self.kind - lo - hi) & 255))

    @property
    def semantic(self) -> tuple[int, int]:
        return self.kind & 0x7F, self.value


def _mods_to_qmk(mask: int) -> int:
    if 0 < mask <= 0x0F:
        return mask
    if mask & 0x0F == 0 and 0 < mask <= 0xF0:
        return 0x10 | (mask >> 4)
    raise Unsupported('QMK modified keycode cannot encode mixed left/right modifiers')


def _mods_from_qmk(mods: int) -> int:
    mask = mods & 0x0F
    if not mask:
        raise Unsupported('empty modifier combination')
    return mask << 4 if mods & 0x10 else mask


def is_mod_tap(code: int) -> bool:
    """Bounded MT syntax only; firmware adapter owns engine slots and capacity."""
    return (0x2000 <= code <= 0x3FFF and (code >> 8) & 15 != 0
            and code & 255 in SUPPORTED_KEYBOARD_USAGES)


# VIA M0..M7 = 0x7700.. -> OEM type-9 values 6.. (OEM macro ids are 1-based; 1..5 factory).
MACRO_BASE = 6
MACRO_MAX = 8


def is_layer_tap(code: int, layer: int) -> bool:
    """LT(1,kc) on a Win base entry, LT(3,kc) on a Mac base entry (hold = Fn)."""
    return (code & 0xF000 == 0x4000 and layer % 2 == 0 and (code >> 8) & 15 == layer + 1
            and code & 255 in SUPPORTED_KEYBOARD_USAGES)


class Codec:
    """Explicit partial translation; opaque factory actions stay at their own slots."""
    def __init__(self, original: list[Action]):
        for offset, action in enumerate(original):
            try:
                self.standard_code(action, offset // SLOTS)
            except Unsupported:
                if action.semantic not in FACTORY_ACTIONS:
                    raise ValueError('unknown factory action for pinned OE915 codec')
        self.opaque = {0x7E00 + i: action for i, action in enumerate(FACTORY_ACTIONS)}
        self.reverse_opaque = {action: code for code, action in self.opaque.items()}

    @staticmethod
    def standard_code(action: Action, layer: int) -> int:
        kind, value = action.semantic
        if kind == 0 and value == 0:
            return 0
        if kind == 2 and value in SUPPORTED_KEYBOARD_USAGES:
            return value
        if kind == 10 and is_mod_tap(value):
            return value
        if kind == 11 and is_layer_tap(value, layer):
            return value
        if kind == 9 and MACRO_BASE <= value < MACRO_BASE + MACRO_MAX:
            return 0x7700 + value - MACRO_BASE
        if kind == 1:
            if value and value & (value - 1) == 0 and value <= 0x80:
                return 0xE0 + value.bit_length() - 1
            return _mods_to_qmk(value) << 8
        if kind == 3:
            reverse = {usage: code for code, usage in CONSUMERS.items()}
            if value in reverse:
                return reverse[value]
        if kind == 6 and value == 2:
            # OS banks are not freely stackable QMK layers. Fn selects the
            # corresponding second layer inside the current hardware OS bank.
            return 0x5221 if layer < 2 else 0x5223
        if kind == 7 and value >> 8 in SUPPORTED_KEYBOARD_USAGES:
            return (_mods_to_qmk(value & 255) << 8) | (value >> 8)
        raise Unsupported(f'no standard VIA mapping for OE915 {kind:02x}:{value:04x}')

    def to_via(self, action: Action, layer: int) -> int:
        try:
            return self.standard_code(action, layer)
        except Unsupported:
            try:
                return self.reverse_opaque[action.semantic]
            except KeyError as error:
                raise Unsupported('unregistered factory action') from error

    def from_via(self, code: int, layer: int, original: Action) -> Action:
        if code in self.opaque:
            if self.opaque[code] == original.semantic:
                return original
            raise Unsupported('factory action may only be retained/restored at its original slot')
        if code == 0:
            kind, value = 0, 0
        elif code in SUPPORTED_KEYBOARD_USAGES:
            kind, value = 2, code
        elif is_mod_tap(code):
            # Runtime-only marker consumed by the custom tap-hold adapter.
            kind, value = 10, code
        elif is_layer_tap(code, layer):
            kind, value = 11, code
        elif 0x7700 <= code < 0x7700 + MACRO_MAX:
            kind, value = 9, MACRO_BASE + code - 0x7700
        elif 0xE0 <= code <= 0xE7:
            kind, value = 1, 1 << (code - 0xE0)
        elif code in CONSUMERS:
            kind, value = 3, CONSUMERS[code]
        elif code == (0x5221 if layer < 2 else 0x5223):
            kind, value = 6, 2
        elif 0x0100 <= code <= 0x1FFF:
            value = _mods_from_qmk(code >> 8)
            key = code & 255
            if key == 0:
                kind = 1
            elif key in SUPPORTED_KEYBOARD_USAGES and value <= 0x0F:
                kind, value = 7, value | key << 8
            else:
                raise Unsupported('unsupported modified basic key')
        else:
            # Includes KC_TRNS, layer-tap, macros and arbitrary layers.
            raise Unsupported(f'keycode 0x{code:04x} needs a missing firmware engine')
        return Action(kind | (original.kind & 0x80), value)


class Endpoint:
    """Pure in-memory 32-byte request/response endpoint. This is not a HID transport."""
    def __init__(self, original: list[Action], clock=None):
        if len(original) != len(LAYERS) * SLOTS:
            raise ValueError('expected four complete 6x22 tables')
        self.original = tuple(original)
        self.actions = list(original)
        self.codec = Codec(original)
        self.layout_options = 0
        self.clock = clock or (lambda: int(time.monotonic() * 1000))
        self.started = self.clock()
        self.indication = 0

    @classmethod
    def stock(cls) -> 'Endpoint':
        stock = Path(__file__).resolve().parents[1] / 'stock' / 'CompxUpgradeBinFile.bin'
        data = stock.read_bytes()
        if hashlib.sha256(data[0x2000:]).hexdigest() != STOCK_PAYLOAD_SHA256:
            raise ValueError('stock firmware mismatch')
        layers = [data[at:at + SLOTS * 4] for at in (0x22cc0, 0x22ed0, 0x230e0, 0x232f0)]
        if hashlib.sha256(b''.join(layers)).hexdigest() != TABLES_SHA256:
            raise ValueError('stock keymap digest mismatch')
        return cls([Action.decode(layer[i:i + 4]) for layer in layers for i in range(0, len(layer), 4)])

    @staticmethod
    def position(layer: int, row: int, col: int) -> int:
        if not (0 <= layer < 4 and 0 <= row < ROWS and 0 <= col < COLS):
            raise Unsupported('matrix position outside bounds')
        return (layer * ROWS + row) * COLS + col

    def keycode_bytes(self) -> bytes:
        return b''.join(struct.pack('>H', self.codec.to_via(a, i // SLOTS))
                        for i, a in enumerate(self.actions))

    def set_keycode(self, offset: int, code: int):
        self.actions[offset] = self.codec.from_via(code, offset // SLOTS, self.original[offset])

    def set_buffer(self, offset: int, raw: bytes):
        current = bytearray(self.keycode_bytes())
        if not 0 <= offset <= len(current) or offset + len(raw) > len(current):
            raise Unsupported('buffer outside keymap')
        current[offset:offset + len(raw)] = raw
        candidate = list(self.actions)
        # Validate every affected code before changing any action. VIA may write
        # an odd number of bytes; decode the full resulting big-endian words.
        for i in range(offset // 2, (offset + len(raw) + 1) // 2):
            code = struct.unpack_from('>H', current, i * 2)[0]
            candidate[i] = self.codec.from_via(code, i // SLOTS, self.original[i])
        self.actions = candidate

    def handle(self, request: bytes) -> bytes:
        if not isinstance(request, bytes) or len(request) != REPORT_SIZE:
            raise ValueError('VIA payload must be exactly 32 bytes, excluding a HID Report ID')
        reply = bytearray(request)
        command = request[0]
        try:
            if command == 0x01:
                reply[1:3] = struct.pack('>H', PROTOCOL_VERSION)
            elif command == 0x02:
                value_id = request[1]
                if value_id == 1:
                    value = (self.clock() - self.started) & 0xFFFFFFFF
                elif value_id == 2:
                    value = self.layout_options
                elif value_id == 3:
                    # Host reference has no live switch observations. Return
                    # secure-style zeros, never pretend to read the keyboard.
                    if request[2] >= ROWS: raise Unsupported('matrix row offset out of range')
                    reply[3:3 + (ROWS - request[2]) * 3] = b'\0' * ((ROWS - request[2]) * 3)
                    return bytes(reply)
                elif value_id == 4:
                    value = 1  # prototype implementation version, not vendor 2.52
                elif value_id == 6:
                    value = KEYCODES_VERSION
                else:
                    raise Unsupported('unknown keyboard value')
                reply[2:6] = struct.pack('>I', value)
            elif command == 0x03:
                if request[1] == 2:
                    options = struct.unpack_from('>I', request, 2)[0]
                    if options: raise Unsupported('no layout options implemented')
                    self.layout_options = options
                elif request[1] == 5:
                    self.indication = request[2]  # recorded only; no LED/device access
                else:
                    raise Unsupported('unknown keyboard setting')
            elif command in (0x04, 0x05):
                offset = self.position(*request[1:4])
                if command == 0x04:
                    reply[4:6] = struct.pack('>H', self.codec.to_via(self.actions[offset], request[1]))
                else:
                    self.set_keycode(offset, struct.unpack_from('>H', request, 4)[0])
            elif command == 0x06:
                self.actions = list(self.original)
            elif command == 0x0C:
                reply[1] = 0  # does NOT expose or overwrite stock macro storage
            elif command == 0x0D:
                reply[1:3] = b'\0\0'
            elif command == 0x11:
                reply[1] = len(LAYERS)
            elif command in (0x12, 0x13):
                offset = struct.unpack_from('>H', request, 1)[0]
                size = request[3]
                if size > 28: raise Unsupported('bulk transfer exceeds report')
                if offset + size > len(self.actions) * 2: raise Unsupported('keymap transfer out of bounds')
                if command == 0x12:
                    reply[4:4 + size] = self.keycode_bytes()[offset:offset + size]
                else:
                    self.set_buffer(offset, request[4:4 + size])
            else:
                # No firmware reset, EEPROM erase, bootloader jump, macros,
                # encoders, lighting or undefined custom commands.
                raise Unsupported('command is not implemented')
        except Unsupported:
            reply[0] = 0xFF
        return bytes(reply)

    def export_profile(self) -> dict:
        return {'changes': [{'layer': LAYERS[i // SLOTS], 'index': i % SLOTS,
                             'type': action.kind & 0x7F, 'value': action.value}
                            for i, action in enumerate(self.actions) if action != self.original[i]]}

    def custom_keycodes(self) -> list[dict]:
        result = []
        for i, (kind, value) in enumerate(self.codec.opaque.values()):
            label, short, details = FACTORY_LABELS.get(
                (kind, value), (f'Factory {kind:02X}:{value:04X}', f'F{i}', 'Unidentified factory action.'))
            result.append({'name': f'{label} ({kind:02X}:{value:04X})',
                           'title': details + ' Retain/restore at original slot only.',
                           'shortName': short})
        return result


def packet(*values: int) -> bytes:
    if len(values) > REPORT_SIZE: raise ValueError('too many packet bytes')
    return bytes(values).ljust(REPORT_SIZE, b'\0')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('demo', 'serve', 'definition'))
    args = parser.parse_args()
    endpoint = Endpoint.stock()
    if args.mode == 'definition':
        definition = json.loads((HERE / 'definitions/flow84.json').read_text())
        definition['customKeycodes'] = endpoint.custom_keycodes()
        print(json.dumps(definition, ensure_ascii=False, indent=2))
    elif args.mode == 'demo':
        for label, request in (
            ('protocol', packet(1)), ('layers', packet(0x11)),
            ('read Caps', packet(4, 0, 3, 0)),
            ('set Caps -> Left Ctrl in RAM model', packet(5, 0, 3, 0, 0, 0xE0)),
            ('read remapped Caps', packet(4, 0, 3, 0))):
            print(f'{label}: {endpoint.handle(request).hex(" ")}')
        print(json.dumps(endpoint.export_profile(), indent=2))
    else:
        print('OFFLINE stdin/stdout only; enter one 32-byte hex VIA payload per line.', file=sys.stderr)
        for line in sys.stdin:
            if not line.strip(): continue
            try:
                print(endpoint.handle(bytes.fromhex(line)).hex(' '), flush=True)
            except ValueError as error:
                print(f'ERROR: {error}', file=sys.stderr)


if __name__ == '__main__': main()
