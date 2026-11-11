"""The opt-in --flash-probe image: what its experiment does to the flash, checked in the emulator.

This cannot show how the real flash controller behaves; it pins down that the experiment programs
only the two intended spare words, restores register 0x803024, and is absent from normal images."""
from pathlib import Path
import struct,tempfile,unittest
from unicorn.arm_const import UC_ARM_REG_R0,UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3,UC_ARM_REG_LR,UC_ARM_REG_PC
import test_persistence_device as pd
from build import build,PROBE_START
import oe915
from via_proto import packet
PROTECT=0x803024
PATTERN=bytes([0x5a,0xa5,0x3c,0xc3])


class ProbeBoard(pd.SavedBoard):
    """Selector-0 programming modelled as the hypothesis under test: it works only while 0x803024 is 0."""
    def __init__(self,*a,protected=True,**k):
        self.code_programs=[];self.protected=protected
        super().__init__(*a,**k)
        self.w32(PROTECT,0xe0)                               # what the stock main() leaves there
    def on_code(self,uc,address,size,data):
        if address==0x1b9a4 and uc.reg_read(UC_ARM_REG_R0)==0:
            word,n,buf=[uc.reg_read(r) for r in (UC_ARM_REG_R1,UC_ARM_REG_R2,UC_ARM_REG_R3)]
            reg=struct.unpack('<I',self.read(PROTECT,4))[0]
            self.code_programs.append((word*4,self.read(buf,n),reg))
            ok=not self.protected or reg==0
            if ok:self.write(word*4,self.read(buf,n))
            uc.reg_write(UC_ARM_REG_R0,int(ok));uc.reg_write(UC_ARM_REG_PC,uc.reg_read(UC_ARM_REG_LR))
            return
        return super().on_code(uc,address,size,data)


class FlashProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.out=Path(cls.temp.name)
        cls.image,cls.symbols,cls.manifest=build(cls.out/'probe',flash_probe=True)
        cls.normal,_,cls.normal_manifest=build(cls.out/'normal')
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def board(self,image,sub,**k):
        b=ProbeBoard(self.out/sub/'flow84-via.elf',image,**k)
        b.write(PROBE_START,b'\xff'*0x100)                  # the erased tail of the update window
        return b

    def test_probe_programs_only_its_two_spare_words_and_restores_the_register(self):
        for slot in (0,15):
            b=self.board(self.image,'probe')
            r=b.transact(packet(8,0,17,slot))
            at=PROBE_START+slot*8
            self.assertEqual(b.code_programs,[(at,PATTERN,0xe0),(at+4,PATTERN,0)])
            self.assertEqual((r[0],r[3],r[4],r[5],r[6:10]),(8,1,0xe0,0,b'\xff'*4))        # refused as is
            self.assertEqual((r[10],r[11],r[12:16],r[16]),(0,1,PATTERN,0xe0))            # works when cleared
            self.assertEqual(struct.unpack('<I',b.read(PROTECT,4))[0],0xe0)
            self.assertEqual(b.ops,[])                                                   # settings area untouched
            self.assertTrue(all(PROBE_START<=a and a+4<=oe915.MAX_IMAGE_SIZE for a,_,_ in b.code_programs))
        b=self.board(self.image,'probe',protected=False)
        r=b.transact(packet(8,0,17,3))
        self.assertEqual((r[5],r[6:10],r[11],r[12:16]),(1,PATTERN,1,PATTERN))

    def test_probe_is_refused_while_a_key_is_held(self):
        b=self.board(self.image,'probe');b.matrix(68)
        self.assertEqual(b.transact(packet(8,0,17,0))[0],0xff);self.assertEqual(b.code_programs,[])

    def test_normal_image_has_no_probe_and_probe_image_cannot_be_published(self):
        b=self.board(self.normal,'normal')
        b.transact(packet(8,0,17,0))
        self.assertEqual(b.code_programs,[])
        self.assertEqual((self.normal_manifest['flash_probe'],self.manifest['flash_probe']),(False,True))
        self.assertLessEqual(len(self.image),PROBE_START)
