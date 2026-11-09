"""VIA macros through the real USB path and the UNCHANGED OEM macro player."""
from pathlib import Path
import tempfile,unittest
import test_device_emulated as dev
from test_persistence_device import SavedBoard
from build import build
from via_proto import packet
PHYS=66
TABLE_RECORDS=19
FLAGS=0x401c04
LENGTHS=0x401c08


class MacroMixin:
    """Boot path: gate loader seeds the RAM table, OEM scanner fills lengths."""
    def boot(self):
        self.call('via_gate_loader');self.call(0x18e30,LENGTHS)
        self.table=self.sym['macro_table']
        return self
    def tick(self):
        """Scan-IRQ behaviour (stock 0x1b378): count the delay down, flag at zero."""
        flags=self.read(FLAGS,1)[0]
        if flags&2:
            d=int.from_bytes(self.read(FLAGS+1,2),'little')
            if d:
                d-=1;self.write(FLAGS+1,d.to_bytes(2,'little'))
                if d==0:self.write(FLAGS,bytes([flags|1]))
    def play(self,limit=400):
        start=len(self.reports)
        self.matrix(PHYS);self.matrix()
        for _ in range(limit):
            self.tick();self.matrix();self.drain()
            if not self.read(FLAGS,1)[0]&2 and len(self.reports)>start:break
        return [bytes(r) for r in self.reports[start:]]


class MacroBoard(MacroMixin,dev.USBBoard):
    def __init__(self,elf,image):
        super().__init__(elf,image);self.boot()


class SavedMacroBoard(MacroMixin,SavedBoard):
    def __init__(self,elf,image,nv=None):
        super().__init__(elf,image,nv);self.boot()


class MacroDevice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='flow-macro-')
        cls.out=Path(cls.temp.name)
        cls.image,cls.symbols,cls.manifest=build(cls.out)
        cls.elf=cls.out/'flow84-via.elf'
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def board(self):return MacroBoard(self.elf,self.image)
    def saved(self,nv=None):return SavedMacroBoard(self.elf,self.image,nv)
    def write(self,b,data,offset=0):
        for i in range(0,len(data),28):
            chunk=data[i:i+28]
            self.assertEqual(b.transact(packet(0x0f,(offset+i)>>8,(offset+i)&255,len(chunk),*chunk))[0],0x0f)
    def read(self,b,n,offset=0):
        out=b''
        for i in range(0,n,28):
            m=min(28,n-i);out+=b.transact(packet(0x0e,(offset+i)>>8,(offset+i)&255,m))[4:4+m]
        return out
    def bind(self,b,code):
        self.assertEqual(b.transact(packet(5,0,3,0,code>>8,code&255))[0],5)

    def test_count_size_roundtrip_reset_and_bounds(self):
        b=self.board()
        self.assertEqual(b.transact(packet(0x0c))[1],8)
        self.assertEqual(int.from_bytes(b.transact(packet(0x0d))[1:3],'big'),192)
        self.write(b,b'hello\0world\0')
        self.assertEqual(self.read(b,12),b'hello\0world\0')
        self.assertEqual(b.transact(packet(0x0e,0,168,28))[0],0x0e)               # VIA's last chunk overruns: clamped
        self.assertEqual(b.transact(packet(0x0e,0,168,28))[4:32],bytes(28))
        self.assertEqual(b.transact(packet(0x0f,0,190,5,1,2,3,4,5))[0],0x0f)      # clamped write: 2 bytes kept
        self.assertEqual(self.read(b,2,190),bytes([1,2]))
        self.assertEqual(b.transact(packet(0x0e,0x01,0,4))[0],0xff)               # offset beyond the buffer
        self.assertEqual(b.transact(packet(0x0e,0,0,29))[0],0xff)                 # > 28 bytes
        self.assertEqual(b.transact(packet(0x10))[0],0x10)
        self.assertEqual(self.read(b,12),bytes(12))

    def test_factory_table_seeded_and_user_edits_never_touch_it(self):
        b=self.board()
        rom=bytes(self.image[0x22b18:0x22b18+228])
        self.assertEqual(b.read(b.table,228),rom)
        self.assertEqual(b.read(LENGTHS,5),bytes([4,4,4,3,4]))
        self.write(b,b'some text here\0second\0')
        self.assertEqual(b.read(b.table,228),rom)
        self.assertEqual(b.read(LENGTHS,5),bytes([4,4,4,3,4]))

    def test_user_macro_types_text_through_stock_player(self):
        b=self.board();self.write(b,b'ab');self.bind(b,0x7700)
        reports=b.play()
        self.assertEqual([r[2] for r in reports],[4,5,0])
        self.assertEqual({r[0] for r in reports},{0})

    def test_shifted_text_and_second_macro_slot(self):
        b=self.board();self.write(b,b'x\0Hi\0');self.bind(b,0x7701)
        reports=b.play()
        self.assertEqual([(r[0],r[2]) for r in reports],[(2,0x0b),(0,0x0c),(0,0)])

    def test_modifier_down_key_tap_up_and_delay_actions(self):
        b=self.board();self.bind(b,0x7700)
        self.write(b,bytes([1,2,0xe0,1,1,0x06,1,4])+b'30|'+bytes([1,3,0xe0]))   # Ctrl down, C, wait, Ctrl up
        reports=b.play()
        self.assertEqual([(r[0],r[2]) for r in reports],[(1,0),(1,6),(0,0)])

    def test_factory_win_tab_macro_plays_from_ram_table(self):
        b=self.board()
        b.write(0x401c34+PHYS*4,bytes([9,4,0,0x55-9-4]))          # native type-9 value 4 on Caps
        reports=b.play()
        self.assertTrue(any(r[0]==8 and r[2]==0x2b for r in reports),[r.hex() for r in reports])

    def test_bad_buffer_keeps_previous_macros(self):
        b=self.board();self.write(b,b'ab');self.bind(b,0x7700)
        good=b.read(b.table+TABLE_RECORDS*12,48)
        self.write(b,bytes([1,1,3]))                               # unsupported keycode
        self.assertEqual(b.read(b.table+TABLE_RECORDS*12,48),good)
        self.assertEqual([r[2] for r in b.play()],[4,5,0])
        self.write(b,bytes(3)+b'a')                                # valid again: M3 = 'a'
        self.assertEqual(b.read(LENGTHS+5,8),bytes([2,2,2,3,2,2,2,2]))

    def test_reset_and_empty_macro_do_nothing_and_do_not_underflow(self):
        b=self.board();self.bind(b,0x7703)
        self.assertEqual(b.read(LENGTHS+5,8),bytes([2]*8))
        reports=b.play(limit=50)
        self.assertEqual([r[2] for r in reports if r[2]],[])
        self.assertEqual(b.read(FLAGS,1)[0]&2,0)

    def test_macro_keycodes_limited_to_eight_and_readback(self):
        b=self.board()
        for n in range(8):
            self.bind(b,0x7700+n)
            self.assertEqual(b.transact(packet(4,0,3,0))[4:6],bytes([0x77,n]))
        self.assertEqual(b.transact(packet(5,0,3,0,0x77,8))[0],0xff)
        self.assertEqual(b.transact(packet(5,0,3,0,0x7f,0))[0],0xff)

    def test_edit_while_player_busy_is_deferred_until_idle(self):
        b=self.board();self.write(b,b'ab')
        before=b.read(b.table+TABLE_RECORDS*12,24)
        b.write(FLAGS,bytes([2]))                                  # OEM player active
        self.write(b,b'ba')
        self.assertEqual(b.read(b.table+TABLE_RECORDS*12,24),before)
        b.write(FLAGS,bytes([0]));b.poll()
        self.assertNotEqual(b.read(b.table+TABLE_RECORDS*12,24),before)

    def test_macros_and_binding_survive_save_and_reload(self):
        b=self.saved();self.write(b,b'ab');self.bind(b,0x7700)
        self.assertEqual(b.transact(packet(7,0,1,1))[0],7)
        fresh=self.saved(b.nv)
        self.assertEqual(self.read(fresh,2),b'ab')
        self.assertEqual(fresh.transact(packet(4,0,3,0))[4:6],bytes([0x77,0]))
        self.assertEqual([r[2] for r in fresh.play()],[4,5,0])
        self.assertEqual(fresh.read(LENGTHS,13),bytes([4,4,4,3,4,4]+[2]*7))

    def test_macro_only_change_is_saved_and_clearing_persists(self):
        b=self.saved();self.write(b,b'ab');self.assertEqual(b.transact(packet(7,0,1,1))[0],7)
        n=len(b.ops);self.write(b,b'abc');self.assertEqual(b.transact(packet(7,0,1,1))[0],7)
        self.assertGreater(len(b.ops),n);self.assertNotIn('erase',[o[0] for o in b.ops])
        self.assertEqual(self.read(self.saved(b.nv),3),b'abc')
        self.assertEqual(b.transact(packet(0x10))[0],0x10)
        self.assertEqual(b.transact(packet(7,0,1,1))[0],7)
        self.assertEqual(self.read(self.saved(b.nv),3),bytes(3))

    def test_unchanged_macros_do_not_touch_flash_and_corrupt_blob_falls_back_safely(self):
        b=self.saved();self.write(b,b'ab');b.transact(packet(7,0,1,1));ops=list(b.ops)
        self.assertEqual(b.transact(packet(7,0,1,1))[0],7);self.assertEqual(b.ops,ops)
        bad=bytearray(b.nv);bad[28+8]^=0x40                        # damage the blob record
        fresh=self.saved(bytes(bad))
        self.assertEqual(self.read(fresh,2),bytes(2))
        self.assertEqual(fresh.read(LENGTHS,13)[5:],bytes([2]*8))


if __name__=='__main__':unittest.main()
