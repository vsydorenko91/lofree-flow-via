"""Actual ARM USB Save/load with a strict selector-1 sector-A NOR model."""
from pathlib import Path
import struct,tempfile,unittest
from unicorn import UC_HOOK_CODE
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_LR,UC_ARM_REG_PC,UC_ARM_REG_SP
import test_device_emulated as dev
from build import build
import oe915
from via_proto import packet
BASE=oe915.MAX_IMAGE_SIZE
A_WORD=0x8000;A_BYTES=476          # only 17*28 bytes of sector A are addressable


class SavedBoard(dev.USBBoard):
    """Intercepts the OEM selector-1 read/erase/program wrappers for sector A."""
    def __init__(self,elf,image,nv=None,boot_key=None):
        self.nv=bytearray(b'\xff'*512 if nv is None else nv)
        self.ops=[];self.fail_at=None;self.nv_reads=0
        self.min_sp=0x4074f0;self.boot_key=boot_key;self.warming=True
        super().__init__(elf,image)
        self.warming=False
    def matrix(self,*indices,submit=True):
        if self.warming and self.boot_key is not None:indices=(self.boot_key,)
        return super().matrix(*indices,submit=submit)
    def on_code(self,uc,address,size,data):
        if uc.reg_read(UC_ARM_REG_SP)==0x40f000:uc.reg_write(UC_ARM_REG_SP,0x4074f0)
        self.min_sp=min(self.min_sp,uc.reg_read(UC_ARM_REG_SP))
        if address in (0x1a76c,0x19660,0x1b9a4):
            sel,word,n,buf=[uc.reg_read(r) for r in (UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)]
            if sel==1 and A_WORD<=word<A_WORD+0x80:
                off=(word-A_WORD)*4
                if address==0x1a76c:
                    assert off+n<=A_BYTES and n%4==0,(off,n)
                    self.nv_reads+=1;self.write(buf,bytes(self.nv[off:off+n]))
                    uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
                else:
                    erase=address==0x19660
                    if erase:assert off==0 and n==512,(off,n)
                    else:assert n<=32 and n%4==0 and off+n<=A_BYTES,(off,n)   # never touches update marker
                    self.ops.append(('erase' if erase else 'program',off,n))
                    ok=self.fail_at!=len(self.ops)
                    if ok:
                        raw=b'\xff'*512 if erase else self.read(buf,n)
                        if erase:self.nv[:]=raw
                        else:
                            assert all((x&y)==y for x,y in zip(self.nv[off:off+n],raw)),'non-NOR program'
                            self.nv[off:off+n]=raw
                    uc.reg_write(UC_ARM_REG_R0,int(ok));uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
        return super().on_code(uc,address,size,data)


class PersistenceDevice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.out=Path(cls.temp.name)
        cls.image,cls.symbols,cls.manifest=build(cls.out)
        cls.elf=cls.out/'flow84-via.elf'
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def board(self,nv=None,boot_key=None):return SavedBoard(self.elf,self.image,nv,boot_key)
    def status(self,b):return b.transact(packet(8,0,2))[3:].split(b'\0')[0]
    def change(self,b,code=5):self.assertEqual(b.transact(packet(5,0,3,1,0,code))[0],5)
    def save(self,b):return b.transact(packet(7,0,1,1))

    def test_backlight_level_is_ram_only_and_validated(self):
        b=self.board()
        for v in (0,3,4):
            self.assertEqual(b.transact(packet(7,0,6,v))[0],7)
            self.assertEqual(b.transact(packet(8,0,6))[3],v)
            self.assertEqual(b.read(0x4018c2,1)[0],v)
            self.assertEqual(b.read(0x401218,1)[0],v)
        self.assertEqual(b.transact(packet(7,0,6,5))[0],0xff)
        self.assertEqual(b.read(0x4018c2,1)[0],4)
        self.assertEqual(b.ops,[])

    def test_lighting_color_and_mode_are_ram_only_and_validated(self):
        b=self.board()
        for cid,hi,addr,native in ((10,7,0x4018da,{0:0,7:7}),(11,1,0x4018ba,{0:0,1:2})):
            for v in (0,hi):
                self.assertEqual(b.transact(packet(7,0,cid,v))[0],7)
                self.assertEqual(b.transact(packet(8,0,cid))[3],v)
                self.assertEqual(b.read(addr,1)[0],native[v])
            self.assertEqual(b.transact(packet(7,0,cid,hi+1))[0],0xff)
        self.assertEqual(b.ops,[])

    def test_tap_hold_timeout_preset_is_validated_applied_and_persisted(self):
        b=self.board()
        term=lambda bd,k:struct.unpack('<H',bd.read(bd.sym['th_runtime_cfg']+k*6+4,2))[0]
        self.assertEqual(b.transact(packet(8,0,14))[3],0)
        for v,passes in ((1,120),(2,320),(3,500),(0,200)):
            self.assertEqual(b.transact(packet(7,0,14,v))[0],7)
            self.assertEqual(b.transact(packet(8,0,14))[3],v)
            self.assertEqual([term(b,0),term(b,1)],[passes,passes])
        self.assertEqual(b.transact(packet(7,0,14,4))[0],0xff)
        self.assertEqual(b.ops,[])
        self.assertEqual(b.transact(packet(7,0,14,3))[0],7)
        self.assertEqual(self.save(b)[0],7)
        fresh=self.board(b.nv)
        self.assertEqual(fresh.transact(packet(8,0,14))[3],3)
        self.assertEqual([term(fresh,0),term(fresh,1)],[500,500])

    def test_edits_queries_and_generic_save_never_write_flash(self):
        b=self.board();self.assertEqual(self.status(b),b'No saved profile')
        self.change(b);self.assertEqual(self.status(b),b'Unsaved changes')
        b.transact(packet(9,0));b.transact(packet(8,0,1));b.transact(packet(1))
        self.assertEqual(b.ops,[])
        for q in [packet(7,0,1,0),packet(7,1,1,1),packet(7,0,2,1)]:
            self.assertEqual(b.transact(q)[0],0xff)
        self.assertEqual(b.ops,[])

    def test_explicit_save_reloads_map_mt_and_level_after_full_bss_reset(self):
        b=self.board();self.change(b)
        self.assertEqual(b.transact(packet(5,2,3,0,0x21,0x29))[0],5)
        self.assertEqual(b.transact(packet(7,0,6,2))[0],7)
        self.assertEqual(b.transact(packet(7,0,10,5))[0],7);self.assertEqual(b.transact(packet(7,0,11,1))[0],7)
        b.output(packet(7,0,1,1));self.assertEqual(b.ops,[])   # queued in ISR, no flash yet
        b.poll();self.assertEqual(b.receive()[0],7)
        self.assertEqual(self.status(b),b'Saved')
        self.assertEqual([o[0] for o in b.ops],['program'])
        self.assertGreaterEqual(b.min_sp,0x406000)
        fresh=self.board(b.nv)
        self.assertEqual(fresh.transact(packet(4,0,3,1))[4:6],b'\0\x05')
        self.assertEqual(fresh.transact(packet(4,2,3,0))[4:6],b'\x21\x29')
        self.assertEqual(fresh.transact(packet(8,0,6))[3],2)
        self.assertEqual(fresh.read(0x4018c2,1)[0],2)
        self.assertEqual(fresh.transact(packet(8,0,10))[3],5);self.assertEqual(fresh.transact(packet(8,0,11))[3],1)
        self.assertEqual(fresh.read(0x4018da,1)[0],5);self.assertEqual(fresh.read(0x4018ba,1)[0],2)
        fresh.matrix(67);self.assertIn(5,fresh.held_keys());fresh.matrix()
        fresh.write(fresh.options+9,b'\x01');fresh.call('via_gate_loader');fresh.matrix()
        fresh.matrix(66);fresh.matrix();fresh.drain()
        self.assertTrue(any(0x29 in r[2:7] for r in fresh.reports))
        self.assertEqual(self.status(fresh),b'Saved');self.assertEqual(fresh.ops,[])

    def test_layer_tap_survives_save_and_reload(self):
        b=self.board()
        self.assertEqual(b.transact(packet(5,0,3,0,0x41,0x29))[0],5)
        self.assertEqual(self.save(b)[0],7)
        fresh=self.board(b.nv)
        self.assertEqual(fresh.transact(packet(4,0,3,0))[4:6],b'\x41\x29')
        fresh.matrix(66);fresh.matrix(66,67);fresh.matrix();fresh.drain()
        self.assertEqual(fresh.ops,[])

    def test_journal_dump_and_dirty_indicator(self):
        b=self.board()
        self.assertEqual(b.transact(packet(8,0,13))[3],0)
        self.change(b);self.assertEqual(b.transact(packet(8,0,13))[3],1)
        self.assertEqual(b.transact(packet(8,0,12,0))[3:31],b'\xff'*28)
        self.save(b);self.assertEqual(b.transact(packet(8,0,13))[3],0)
        rec=b.transact(packet(8,0,12,0))[3:31]
        self.assertEqual(rec[0],0xc7);self.assertEqual(bytes(b.nv[:28]),bytes(rec))
        self.assertEqual(b.transact(packet(8,0,12,17))[0],0xff)

    def test_unchanged_save_does_not_erase_or_program(self):
        b=self.board();self.change(b);self.assertEqual(self.save(b)[0],7)
        before=list(b.ops);self.assertEqual(self.save(b)[0],7);self.assertEqual(b.ops,before)

    def test_incremental_saves_append_and_never_erase_until_full(self):
        b=self.board()
        for k in range(1,5):
            self.change(b,4+k);self.assertEqual(self.save(b)[0],7)
        self.assertNotIn('erase',[o[0] for o in b.ops])
        self.assertEqual(self.board(b.nv).transact(packet(4,0,3,1))[4:6],b'\0\x08')

    def test_save_failure_reports_and_keeps_previous_profile(self):
        b=self.board();self.change(b,5);self.save(b);self.change(b,6)
        b.fail_at=len(b.ops)+1
        self.assertEqual(self.save(b)[0],0xff);self.assertEqual(self.status(b),b'Save failed')
        self.assertEqual(self.board(b.nv).transact(packet(4,0,3,1))[4:6],b'\0\x05')
        b.fail_at=None;self.assertEqual(self.save(b)[0],7);self.assertEqual(self.status(b),b'Saved')
        self.assertEqual(self.board(b.nv).transact(packet(4,0,3,1))[4:6],b'\0\x06')

    def test_power_cut_inside_a_save_never_corrupts_boot(self):
        b=self.board();self.change(b,5);self.save(b)
        keep=bytes(b.nv);self.change(b,6);self.save(b);full=bytes(b.nv)
        record=bytearray(keep)
        for cut in range(0,28):                       # torn second record at every byte
            torn=bytearray(keep);torn[28:28+cut]=full[28:28+cut]
            fresh=self.board(torn)
            self.assertIn(fresh.transact(packet(4,0,3,1))[4:6],(b'\0\x05',b'\0\x06'))
        broken=bytearray(keep);broken[28:56]=bytes([0xc7])*28   # garbage record, invalid CRC
        self.assertEqual(self.board(broken).transact(packet(4,0,3,1))[4:6],b'\0\x05')

    def test_invalid_stored_profile_falls_back_to_factory(self):
        b=self.board();self.change(b,5);self.save(b)
        bad=bytearray(b.nv)
        bad[8:12]=struct.pack('<HH',19,4)               # protected factory slot change
        import zlib
        crc=0xffff
        for i,byte in enumerate(bad[:28]):
            if i in (6,7):continue
            crc^=byte<<8
            for _ in range(8):crc=((crc<<1)^0x1021)&0xffff if crc&0x8000 else (crc<<1)&0xffff
        bad[6]=crc&255;bad[7]=crc>>8
        fresh=self.board(bad)
        self.assertEqual(fresh.transact(packet(4,0,3,1))[4:6],b'\0\x04')   # factory A, not the saved 5
        self.assertEqual(fresh.transact(packet(4,0,3,0))[4:6],b'\0\x39')   # factory Caps
        self.assertEqual(fresh.ops,[])

    def test_held_input_and_pending_tap_reject_save_before_flash(self):
        b=self.board();self.change(b);b.matrix(68)
        self.assertEqual(self.save(b)[0],0xff);self.assertEqual(b.ops,[]);b.matrix()
        b.transact(packet(5,0,3,0,0x21,0x29));b.matrix(66,submit=False);b.matrix(submit=False)
        self.assertEqual(self.save(b)[0],0xff);self.assertEqual(b.ops,[])
        b.drain();self.assertEqual(self.save(b)[0],7)

    def test_boot_bypass_never_loads_saved_map(self):
        b=self.board();self.change(b);self.save(b)
        bypass=self.board(b.nv,boot_key=66)
        self.assertEqual(bypass.read(bypass.sym['th_gate_state'],1),b'\x02')
        self.assertEqual(bypass.read(0x401c34+67*4+1,1),b'\x04')
        self.assertEqual(bypass.nv_reads,0);self.assertEqual(bypass.ops,[])

    def test_idle_after_edit_and_save_emits_no_unsolicited_usb_packets(self):
        b=self.board();self.change(b)
        for _ in range(20):b.poll()
        self.assertEqual(b.tx[3],[]);self.assertEqual(b.ops,[])
        self.assertEqual(self.status(b),b'Unsaved changes')
        self.save(b);before=list(b.ops)
        for _ in range(20):b.poll()
        self.assertEqual(b.tx[3],[])
        self.assertEqual(self.status(b),b'Saved');self.assertEqual(b.ops,before)

    def test_edit_then_busy_in_preserves_only_requested_reply(self):
        b=self.board();self.change(b)
        b.csr[3]=1;b.poll()
        b.output(packet(1));b.poll()
        b.csr[3]=0;b.poll()
        self.assertEqual(b.receive(),packet(1,0,13))
        b.poll();self.assertEqual(b.tx[3],[])

    def test_builder_keeps_code_within_bootloader_image_limit(self):
        self.assertLessEqual(len(self.image),BASE)
        for sec in self.manifest['sections']:
            if sec['name']!='.bss':self.assertLessEqual(int(sec['address'],16)+sec['size'],BASE)
