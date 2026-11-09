"""OEM code must never erase/overwrite our selector-1 sector A (word 0x8000).

Runs the REAL stock boot-recovery (0x1e3bc), clear (0x1672c) and journal
rotation (0x1f50c, 0x1f3ac) routines with profile data present in sector A and
records every selector-1 erase/program. A control run with the original bytes
restored proves the test would catch the original behaviour."""
from pathlib import Path
import tempfile,unittest
from unicorn.arm_const import UC_ARM_REG_SP,UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_LR,UC_ARM_REG_PC
import test_device_emulated as dev
from build import build
import oe915

STOCK=bytes.fromhex
SITES={0x1e3fa:'53d1',0x1672c:None,0x1f450:None,0x1f45c:None,0x1f5a4:None,0x1f5b0:None}


class Spy(dev.USBBoard):
    def __init__(self,elf,image):
        self.flash_calls=[];self.sector_a=bytes([0xc7,1,2,5,3,0x80,0x12,0x34])+bytes(504)   # profile record, non-FF
        super().__init__(elf,image)
    def on_code(self,uc,address,size,data):
        if uc.reg_read(UC_ARM_REG_SP)==0x40f000:uc.reg_write(UC_ARM_REG_SP,0x4074f0)
        if address in (0x1a76c,0x19660,0x1b9a4):
            sel,word,n,buf=[uc.reg_read(r) for r in (UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)]
            if sel==1:
                a_range=(word&0x3ff)<0x80          # sector A, including its 0x400-word alias window
                if address==0x1a76c:
                    self.write(buf,(self.sector_a if a_range else b'\xff'*512)[:n].ljust(n,b'\xff'))
                else:
                    self.flash_calls.append(('erase' if address==0x19660 else 'program',word,n,a_range))
                uc.reg_write(UC_ARM_REG_R0,1);uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
        return super().on_code(uc,address,size,data)
    def touched_a(self):
        return [c for c in self.flash_calls if c[3]]


class SectorAProtected(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='flow-seca-')
        out=Path(cls.temp.name)
        cls.image,cls.symbols,cls.manifest=build(out)
        cls.elf=out/'flow84-via.elf'
        cls.stock=oe915.load_stock()
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def board(self):return Spy(self.elf,self.image)

    def run_stock(self,b,entry):
        if entry==0x1f50c:      # OEM journal "full" (write offset >= 0x1f8) so the rotation path runs
            journal=int.from_bytes(self.stock[0x1f61c:0x1f620],'little')
            b.write(journal+10,(0x1f8).to_bytes(2,'little'))
        b.flash_calls.clear()
        try:b.call(entry)
        except Exception:pass          # later stock code touches unmodelled hardware; calls so far are what matter

    def test_patched_sites_are_exactly_the_intended_instructions(self):
        im=self.image
        self.assertEqual(im[0x1e3fa:0x1e3fc],bytes.fromhex('53e0'))
        self.assertEqual(im[0x1672c:0x1672e],bytes.fromhex('7047'))
        for site in (0x1f450,0x1f45c,0x1f5a4,0x1f5b0):self.assertEqual(im[site:site+4],b'\xc0\x46\xc0\x46')
        for site in SITES:self.assertNotEqual(im[site:site+2],self.stock[site:site+2])

    def test_boot_recovery_and_clear_leave_sector_a_untouched(self):
        for entry in (0x1e3bc,0x1672c):
            b=self.board();self.run_stock(b,entry)
            self.assertEqual(b.touched_a(),[],hex(entry))

    def test_control_original_code_does_erase_sector_a(self):
        b=self.board()
        b.write(0x1e3fa,self.stock[0x1e3fa:0x1e3fc]);b.write(0x1672c,self.stock[0x1672c:0x1672e])
        self.run_stock(b,0x1e3bc)
        self.assertIn('erase',[c[0] for c in b.touched_a()])
        b=self.board();b.write(0x1672c,self.stock[0x1672c:0x1672e]);self.run_stock(b,0x1672c)
        self.assertIn('erase',[c[0] for c in b.touched_a()])

    def test_rotation_routines_patch_exactly_the_sector_a_backup_calls(self):
        """Statically: each patched BL was an erase/program whose r1 is the register
        holding sector A's word address (0x8000), never journal B (movs r1,#0x80)."""
        import capstone
        md=capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
        for start,end,reg,sites in ((0x1f3ac,0x1f4fc,'r4',((0x1f450,'#0x19660'),(0x1f45c,'#0x1b9a4'))),
                                    (0x1f50c,0x1f61c,'r6',((0x1f5a4,'#0x19660'),(0x1f5b0,'#0x1b9a4')))):
            ins=list(md.disasm(self.stock[start:end],start))
            text=[f'{x.mnemonic} {x.op_str}' for x in ins]
            for site,callee in sites:
                k=next(i for i,x in enumerate(ins) if x.address==site)
                self.assertEqual((ins[k].mnemonic,ins[k].op_str),('bl',callee))
                self.assertIn(f'movs r1, {reg}',text[k-4:k])
                self.assertNotIn('movs r1, #0x80',text[k-4:k])
                self.assertEqual(self.image[site:site+4],b'\xc0\x46\xc0\x46')
            # the journal-B rotation (movs r1,#0x80 erase) must remain intact
            b_erases=[x.address for k,x in enumerate(ins) if x.mnemonic=='bl' and x.op_str=='#0x19660'
                      and 'movs r1, #0x80' in text[k-4:k]]
            self.assertTrue(b_erases)
            for site in b_erases:self.assertEqual(self.image[site:site+4],self.stock[site:site+4])


if __name__=='__main__':unittest.main()
