"""Unicorn ARM harness: runs the ORIGINAL OE915 matrix/HID code plus a built image in emulation."""
from pathlib import Path
import hashlib
import struct
from oe915 import load_stock, PAYLOAD_SHA
from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB

TABLES = (0x22cc0, 0x22ed0, 0x230e0, 0x232f0)
BASE = 0x2000
PHYS = 66
TAP = 19
HOLD = 20


def entry(kind, value):
    lo, hi = value & 255, value >> 8
    return bytes((kind, lo, hi, (0x55 - kind - lo - hi) & 255))


def bl(address, target):
    displacement = ((target & ~1) - address - 4) // 2
    if not -(1 << 21) <= displacement < (1 << 21):
        raise ValueError('Thumb BL out of range')
    displacement &= 0x3fffff
    return struct.pack('<HH', 0xf000 | displacement >> 11, 0xf800 | displacement & 0x7ff)


class Board:
    def __init__(self, elf_path, hold=(6, 2), tap=0x29, term=4, minimum=0, second=None):
        self.payload = load_stock()[0x2000:]
        assert hashlib.sha256(self.payload).hexdigest() == PAYLOAD_SHA
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.mem_map(0, 0x40000)
        self.uc.mem_map(0x400000, 0x10000)
        self.uc.mem_map(0x800000, 0x10000)
        self.uc.mem_write(BASE, self.payload)
        self.uc.mem_write(0x400020, self.read(0x23bac, 0x7b8))
        self.uc.mem_write(0x401c34, self.read(TABLES[0], 0x420))
        self.sym = {}
        with open(elf_path, 'rb') as file:
            elf = ELFFile(file)
            for section in elf.iter_sections():
                if section['sh_flags'] & 2 and section['sh_type'] != 'SHT_NOBITS':
                    self.uc.mem_write(section['sh_addr'], section.data())
            for symbol in elf.get_section_by_name('.symtab').iter_symbols():
                if symbol.name: self.sym[symbol.name] = symbol['st_value']
            self.ram_end = max(s['sh_addr'] + s['sh_size'] for s in elf.iter_sections()
                               if s['sh_flags'] & 2 and s['sh_type'] == 'SHT_NOBITS')
        self.w32(0x400408, 0x401c34)
        self.options = self.u32(0x400414)
        assert 0x400000 <= self.options < 0x405000, hex(self.options)
        self.mode = self.u32(0x1dc60)
        self.write(self.mode, b'\x01')
        self.write(0x400418, b'\x01')  # stock event processing is initialized
        self.write(0x401c04, b'\0' * 0x1a)  # no active macro
        self.write(0x22e0, b'\xff' * 16)
        self.config(0, PHYS, TAP, HOLD, hold, tap, term, minimum)
        if second:
            self.config(1, *second)
        # Redirect the stock matrix reader to a PRIVATE shadow; IRQ matrix untouched.
        self.w32(0x1c14c, self.sym['th_shadow'])
        dis = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        activity_sites = [i.address for i in dis.disasm(self.read(0x1beb4, 0x290), 0x1beb4)
                          if i.mnemonic == 'bl' and i.op_str == '#0x166f0']
        assert len(activity_sites) == 1, activity_sites
        self.patch_call(activity_sites[0], 0x166f0, self.sym['th_activity'])
        self.patch_call(0x1bfce, 0x19898, self.sym['th_key_event'])
        self.patch_call(0x1f84c, 0x121ec, self.sym['th_usb_submit'])
        self.reports = []
        self.fifo = []
        self.writes = []
        self.uc.hook_add(UC_HOOK_MEM_WRITE, self.on_write)
        self.uc.hook_add(UC_HOOK_CODE, self.on_code)
        self.returned = False
        self.calls = 0
        self.instructions = 0
        self.max_instructions = 0

    def config(self, k, phys, tap_slot, hold_slot, hold, tap, term, minimum=0):
        self.write(0x22e0 + k * 8, struct.pack('<BBBBHH', phys, tap_slot, hold_slot, 0, term, minimum))
        for base in TABLES:
            self.write(base + tap_slot * 4, entry(2, tap))
            self.write(base + hold_slot * 4, entry(*hold))
        self.write(0x401c34, self.read(TABLES[0], 0x420))

    def read(self, a, n): return bytes(self.uc.mem_read(a, n))
    def write(self, a, data): self.uc.mem_write(a, bytes(data))
    def u32(self, a): return struct.unpack('<I', self.read(a, 4))[0]
    def w32(self, a, value): self.write(a, struct.pack('<I', value))

    def patch_call(self, address, original, replacement):
        assert self.read(address, 4) == bl(address, original), hex(address)
        self.write(address, bl(address, replacement))

    def on_code(self, uc, address, size, data):
        self.instructions += 1
        if address == 0x2f000:
            self.returned = True
            uc.emu_stop()

    def on_write(self, uc, access, address, size, value, data):
        self.writes.append((address, size, value))
        if address == 0x804011 and value == 8:
            self.fifo = []
        elif address == 0x804024:
            self.fifo.append(value & 255)
        elif address == 0x804011 and value == 1:
            self.reports.append(bytes(self.fifo))

    def call(self, name, *args):
        self.returned = False
        before = self.instructions
        address = self.sym[name] if isinstance(name, str) else name
        self.uc.reg_write(UC_ARM_REG_SP, 0x40f000)
        self.uc.reg_write(UC_ARM_REG_LR, 0x2f001)
        for reg, arg in zip((UC_ARM_REG_R0, UC_ARM_REG_R1), args):
            self.uc.reg_write(reg, arg)
        try:
            self.uc.emu_start(address | 1, 0x2f000, count=1000000)
        except Exception as error:
            raise AssertionError(f'{name}: PC={self.uc.reg_read(UC_ARM_REG_PC):#x}: {error}') from error
        assert self.returned or self.uc.reg_read(UC_ARM_REG_PC) == 0x2f000, name
        self.calls += 1
        self.max_instructions = max(self.max_instructions, self.instructions - before)
        return self.uc.reg_read(UC_ARM_REG_R0)

    def matrix(self, *indices, submit=True):
        rows = [0] * 6
        for idx in indices: rows[idx // 22] |= 1 << (idx % 22)
        raw = struct.pack('<6I', *rows)
        self.write(0x401aec, raw)
        self.call('th_matrix_process')
        assert self.read(0x401aec, 24) == raw, 'physical IRQ matrix was modified'
        assert sum(self.read(0x401b7c, 31)) & 255 == 0x55
        self.call(0x1b8c8)  # ORIGINAL report staging
        if submit: self.send()
        return self.reports[-1] if self.reports else b''

    def send(self, ready=True):
        self.write(0x4003b4, bytes([ready]))
        self.write(0x4003b0, b'\0')
        self.write(0x4003c4, b'\x01')
        self.write(0x401080 + 0x1a, b'\0\0')
        self.call(0x1f7d8)  # ORIGINAL comparator, gates, packet and FIFO writer

    def find(self, kind, value, layer=0):
        for idx in range(132):
            p = self.read(0x401c34 + idx * 4 + layer * 0x210, 3)
            if idx % 22 < 15 and p[0] & 0x7f == kind and p[1] | p[2] << 8 == value:
                return idx
        raise AssertionError((kind, value, layer))

    def held_keys(self): return set(self.read(0x401a78 + 0x1f, 5)) - {0}
    def mods(self): return self.read(0x401a78 + 0x1e, 1)[0]
    def faults(self): return self.read(self.sym['th_faults'], 1)[0]
    def drain(self, limit=20):
        for _ in range(limit): self.matrix()


