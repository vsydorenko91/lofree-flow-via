"""web/profile-core.js (the flasher's backup/restore) driven against the emulated firmware."""
from pathlib import Path
import json,shutil,subprocess,tempfile,unittest
import test_persistence_device as pd
from build import build
from via_proto import packet
ROOT=Path(__file__).resolve().parents[1]
NODE=shutil.which('node')
MT_A,MT_B=0x2204,0x2105      # mod-tap codes
SETTINGS=((6,3),(10,5),(11,1),(14,2))


@unittest.skipUnless(NODE,'node is not installed')
class WebProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.out=Path(cls.temp.name)
        cls.image,cls.symbols,cls.manifest=build(cls.out)
        cls.elf=cls.out/'flow84-via.elf'
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def board(self,nv=None):return pd.SavedBoard(self.elf,self.image,nv)

    def run_js(self,b,*args,hook=None):
        """Run the bridge; every request goes through the real USB path of the emulated keyboard."""
        p=subprocess.Popen([NODE,str(ROOT/'web/test-profile-bridge.mjs'),*args],stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        try:
            n=0
            for line in p.stdout:
                kind,_,rest=line.strip().partition(' ')
                if kind=='Q':
                    q=bytes.fromhex(rest);n+=1
                    if hook:hook(n,q)
                    p.stdin.write(b.transact(q).hex()+'\n');p.stdin.flush()
                else:return kind,rest
            self.fail('bridge ended without a result')
        finally:
            p.kill();p.wait();p.stdout.close();p.stdin.close()

    def set_key(self,b,layer,row,col,code):
        self.assertEqual(b.transact(packet(5,layer,row,col,code>>8,code&255))[0],5)
    def keymap(self,b):
        return b''.join(b.transact(packet(0x12,off>>8,off&255,28))[4:32] for off in range(0,1036,28))+\
               b.transact(packet(0x12,1036>>8,1036&255,20))[4:24]
    def macros(self,b):
        return b''.join(b.transact(packet(0x0e,0,off,min(28,192-off)))[4:4+min(28,192-off)] for off in range(0,192,28))
    def state(self,b):
        return self.keymap(b),self.macros(b),tuple(b.transact(packet(8,0,i))[3] for i,_ in SETTINGS)

    def source(self):
        b=self.board()
        self.set_key(b,0,3,1,5);self.set_key(b,1,2,4,0x7700);self.set_key(b,2,3,2,0x29)
        self.set_key(b,0,3,0,MT_A);self.set_key(b,0,4,0,MT_B)
        text=b'Hi, Flow84!\0\x01\x01\x04second'
        self.assertEqual(b.transact(packet(0x0f,0,0,len(text),*text))[0],0x0f)
        for i,v in SETTINGS:self.assertEqual(b.transact(packet(7,0,i,v))[0],7)
        return b

    def backup(self,b):
        kind,rest=self.run_js(b,'backup');self.assertEqual(kind,'OK',rest)
        return json.loads(rest)
    def restore(self,b,profile,hook=None):
        f=self.out/'profile.json';f.write_text(json.dumps(profile))
        return self.run_js(b,'restore',str(f),hook=hook)

    def test_backup_reads_the_live_profile_without_touching_flash(self):
        b=self.source();want=self.state(b)
        p=self.backup(b)
        self.assertEqual((p['format'],p['version'],p['firmware']),('flow84-via-profile',1,'0.3.0'))
        self.assertEqual((bytes.fromhex(p['keymap']),bytes.fromhex(p['macros'])),want[:2])
        self.assertEqual(p['settings'],{'backlight':3,'sideColor':5,'sideEffect':1,'tapTerm':2})
        self.assertEqual(b.ops,[]);self.assertEqual(self.state(b),want)

    def test_restore_on_a_freshly_flashed_keyboard_survives_a_power_cycle(self):
        src=self.source();p=self.backup(src);want=self.state(src)
        b=self.board()
        self.assertEqual(self.restore(b,p)[0],'OK')
        self.assertEqual(self.state(b),want)
        self.assertEqual(b.transact(packet(8,0,13))[3],0)
        self.assertTrue(b.ops and all(o[0]=='program' for o in b.ops))
        self.assertEqual(self.state(self.board(b.nv)),want)

    def test_restore_replaces_a_different_profile_with_other_tap_hold_keys(self):
        src=self.source();p=self.backup(src);want=self.state(src)
        b=self.board()
        self.set_key(b,0,2,0,MT_A);self.set_key(b,0,5,0,MT_B);self.set_key(b,3,3,3,7)   # both tap-hold slots taken elsewhere
        b.transact(packet(0x0f,0,0,3,*b'old'));self.assertEqual(b.transact(packet(7,0,1,1))[0],7)
        self.assertEqual(self.restore(b,p)[0],'OK')
        self.assertEqual(self.state(self.board(b.nv)),want)

    def test_failed_restore_never_writes_flash(self):
        src=self.source();p=self.backup(src)
        b=self.board();self.set_key(b,0,3,3,9);self.assertEqual(b.transact(packet(7,0,1,1))[0],7)
        stored=bytes(b.nv);ops=list(b.ops)
        # A key is held from the first keymap write on: the firmware refuses every edit.
        hold=lambda n,q:b.matrix(68) if q[0] in (5,0x13) else None
        kind,rest=self.restore(b,p,hook=hold)
        self.assertEqual(kind,'ERR');self.assertIn('could not be restored',rest)
        self.assertEqual((bytes(b.nv),b.ops),(stored,ops))
        for bad in ({},{**p,'keymap':p['keymap'][:-2]},{**p,'macros':'zz'},{**p,'settings':{'backlight':999}},{**p,'version':2}):
            fresh=self.board()
            self.assertEqual(self.restore(fresh,bad)[0],'ERR');self.assertEqual(fresh.ops,[])
