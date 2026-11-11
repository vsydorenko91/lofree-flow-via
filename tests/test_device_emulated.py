"""Actual candidate ARM execution with banked USB CSR/FIFO model; no hardware."""
from pathlib import Path
import struct,sys,tempfile,unittest
HERE=Path(__file__).resolve().parents[1]
from build import build,RAW_DESCRIPTOR
from emulator import Board,PHYS,TABLES
from via_proto import packet
from unicorn import UC_HOOK_MEM_READ


class USBBoard(Board):
    def __init__(self,elf,image):
        super().__init__(elf,hold=(1,1))
        self.image=image
        self.write(0x2000,image[0x2000:])
        self.write(0x401c34,self.read(TABLES[0],0x420))
        self.write(0x4003c4,b'\x01')
        self.endpoint=0;self.csr=[0]*4;self.rx=[]
        self.tx=[[] for _ in range(4)];self.fifos=[[] for _ in range(4)]
        self.reports=self.tx[1]
        self.uc.hook_add(UC_HOOK_MEM_READ,self.on_read)
        self.write(0x4018d1,b'\x01')
        for _ in range(32):self.matrix()
        self.poll()

    def on_read(self,uc,access,address,size,value,data):
        if address==0x804020 and size==1:
            if not self.rx:raise AssertionError('EP0 read beyond supplied host data')
            uc.mem_write(address,bytes([self.rx.pop(0)]))
        elif address==0x804011 and size==1:
            uc.mem_write(address,bytes([self.csr[self.endpoint]]))

    def on_write(self,uc,access,address,size,value,data):
        self.writes.append((address,size,value))
        if address==0x80400e:self.endpoint=value
        elif 0x804020<=address<=0x80402c and (address-0x804020)%4==0:
            ep=(address-0x804020)//4
            self.fifos[ep].append(value&255)
        elif address==0x804011:
            ep=self.endpoint;self.csr[ep]=value
            if value==8:self.fifos[ep]=[]
            if (ep and value==1) or (not ep and value==10 and self.fifos[0]):
                self.tx[ep].append(bytes(self.fifos[ep]));self.fifos[ep]=[]

    def call(self,name,*args):
        if name=='th_matrix_process':name='th_gate_matrix'
        return super().call(name,*args)

    def send(self,ready=True):
        self.csr[1]=0
        return super().send(ready)

    def poll(self):self.call('via_gate_poll')

    def setup(self,raw):
        self.rx=list(raw);self.fifos[0]=[]
        self.w32(0x804014,0) # does not route other endpoint IRQs
        self.write(0x804016,struct.pack('<H',len(raw)))
        self.csr[0]=1;self.endpoint=0;self.write(0x80400e,b'\0')
        self.call(0x17f4c)
        self.assert_rx_empty()

    def assert_rx_empty(self):
        assert not self.rx,self.rx

    def output(self,payload,interface=2,report_id=0):
        setup=struct.pack('<BBHHH',0x21,9,0x0200|report_id,interface,len(payload))
        self.setup(setup)
        self.rx=list(payload);self.fifos[0]=[];self.csr[0]=1
        self.write(0x804016,struct.pack('<H',len(payload)))
        self.call(0x17f4c) # ORIGINAL EP0 receiver + original final routing
        self.assert_rx_empty()

    def receive(self):
        assert self.tx[3], 'no VIA response'
        result=self.tx[3].pop(0)
        self.csr[3]=0
        self.call(0x180f4) # ORIGINAL EP3 IN completion path
        return result

    def transact(self,payload):
        self.output(payload);self.poll();reply=self.receive()
        return reply

    def poison(self):
        self.write(0x24370,b'\x00\xde'*((len(self.image)-0x24370)//2))


class DeviceTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='flow-via-')
        cls.out=Path(cls.temp.name)
        cls.image,cls.symbols,cls.manifest=build(cls.out)
        cls.elf=cls.out/'flow84-via.elf'
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def board(self):return USBBoard(self.elf,self.image)

    def set_mt(self,b,layer=0,slot=66,code=0x2129):
        row,col=divmod(slot,22)
        self.assertEqual(b.transact(packet(5,layer,row,col,code>>8,code&255))[0],5)
        self.assertEqual(b.transact(packet(4,layer,row,col))[4:6],struct.pack('>H',code))
        # Timing remains processing passes; shorten only the emulator descriptor.
        for k in range(4):b.write(b.sym['th_runtime_cfg']+k*6+4,struct.pack('<H',4))

    def test_runtime_mt_short_tap_hold_and_remove(self):
        b=self.board();self.set_mt(b)
        b.matrix(PHYS);b.matrix();b.drain()
        self.assertEqual([0x29 in r[2:7] for r in b.reports],[True,False])
        self.assertNotIn(0x39,b.held_keys())
        for _ in range(4):b.matrix(PHYS)
        self.assertEqual(b.mods(),1);b.matrix();self.assertEqual(b.mods(),0)
        self.assertEqual(b.transact(packet(5,0,3,0,0,0x39))[0],5)
        b.matrix(PHYS);self.assertIn(0x39,b.held_keys());b.matrix()
        self.assertEqual(b.held_keys(),set())

    def test_runtime_mt_right_modifier_and_other_key_before_or_after_caps(self):
        for other in (45,67):
            b=self.board();self.set_mt(b,code=0x3129)
            b.matrix(PHYS);b.matrix(PHYS,other)
            self.assertEqual(b.mods(),0x10)
            b.matrix(other);self.assertEqual(b.mods(),0)
            b.matrix();self.assertEqual(b.held_keys(),set())

    def test_runtime_mt_on_base_preserves_ordinary_action_on_fn_layer(self):
        b=self.board();self.set_mt(b)
        self.assertEqual(b.transact(packet(5,1,3,0,0,5))[0],5)
        fn=next(i for i in range(132) if b.read(0x401c34+i*4,3)==bytes([6,2,0]))
        b.matrix(fn);b.matrix(fn,PHYS)
        self.assertIn(5,b.held_keys());self.assertEqual(b.mods(),0)
        b.matrix(fn);b.matrix();self.assertEqual(b.held_keys(),set())

    def test_runtime_mt_uses_current_physical_fn_for_same_scan_press(self):
        for base_mt in (True,False):
            b=self.board()
            self.set_mt(b,layer=0 if base_mt else 1)
            plain_layer=1 if base_mt else 0
            self.assertEqual(b.transact(packet(5,plain_layer,3,0,0,5))[0],5)
            fn=next(i for i in range(132) if b.read(0x401c34+i*4,3)==bytes([6,2,0]))
            b.matrix(fn,PHYS)
            if base_mt:
                self.assertIn(5,b.held_keys());self.assertEqual(b.mods(),0)
                b.matrix(PHYS);self.assertIn(5,b.held_keys())
            else:
                self.assertEqual(b.mods(),1);self.assertNotIn(5,b.held_keys())
            b.matrix();self.assertEqual(b.mods(),0);self.assertEqual(b.held_keys(),set())

    def set_lt(self,b,layer=0,slot=66,code=0x4129):
        row,col=divmod(slot,22)
        self.assertEqual(b.transact(packet(5,layer,row,col,code>>8,code&255))[0],5)
        self.assertEqual(b.transact(packet(4,layer,row,col))[4:6],struct.pack('>H',code))
        for k in range(4):b.write(b.sym['th_runtime_cfg']+k*6+4,struct.pack('<H',4))

    def test_runtime_lt_tap_sends_key_and_hold_selects_fn_layer(self):
        b=self.board();self.set_lt(b)
        b.matrix(PHYS);b.matrix();b.drain()
        self.assertEqual([0x29 in r[2:7] for r in b.reports],[True,False])
        self.assertNotIn(0x39,b.held_keys())
        # Fn layer entry for the 'A' position is 'B'; hold via another key press.
        self.assertEqual(b.transact(packet(5,1,3,1,0,5))[0],5)
        b.matrix(PHYS);b.matrix(PHYS,67)
        self.assertIn(5,b.held_keys());self.assertNotIn(4,b.held_keys());self.assertEqual(b.mods(),0)
        b.matrix(PHYS);b.matrix();self.assertEqual(b.held_keys(),set())
        # Long hold also activates the layer without another key.
        for _ in range(5):b.matrix(PHYS)
        b.matrix(PHYS,67);self.assertIn(5,b.held_keys())
        b.matrix(PHYS);b.matrix();self.assertEqual(b.held_keys(),set())
        # After release the base layer is back.
        b.matrix(67);self.assertIn(4,b.held_keys());b.matrix()

    def test_runtime_lt_modifier_press_does_not_force_hold_so_ctrl_space_chord_works(self):
        b=self.board();self.set_lt(b,slot=66,code=0x412c)               # LT(1,Space) on Caps position
        ctrl=next(i for i in range(132) if b.read(0x401c34+i*4,2)==bytes([1,1]) and i!=66)
        b.matrix(PHYS);b.matrix(PHYS,ctrl)                              # Space(LT) pending, then Ctrl pressed
        b.matrix(ctrl);b.drain();b.matrix(ctrl);b.drain()               # Space released while Ctrl still held
        spaces=[r for r in b.reports if 0x2c in r[2:7]]
        self.assertTrue(spaces and all(r[0]&1 for r in spaces),[r.hex() for r in b.reports])
        b.matrix();b.drain()
        self.assertEqual(b.held_keys(),set());self.assertEqual(b.mods(),0)

    def test_via_unsaved_toggle_click_is_acknowledged(self):
        b=self.board()
        for v in (1,0):
            r=b.transact(packet(7,0,13,v));self.assertEqual(r[0],7);self.assertEqual(r[2:4],bytes([13,v]))

    def test_runtime_lt_still_holds_on_ordinary_other_key(self):
        b=self.board();self.set_lt(b,slot=66,code=0x412c)
        self.assertEqual(b.transact(packet(5,1,3,1,0,5))[0],5)
        b.matrix(PHYS);b.matrix(PHYS,67);self.assertIn(5,b.held_keys());self.assertNotIn(0x2c,b.held_keys())
        b.matrix(PHYS);b.matrix()

    def test_runtime_lt_accepts_only_supported_layer_pairs_and_base_banks(self):
        b=self.board()
        for layer,code in ((0,0x4229),(0,0x4329),(0,0x4029),(1,0x4129),(1,0x4329),(2,0x4129),(2,0x4229),(3,0x4329),(0,0x4100),(0,0x41a5)):
            self.assertEqual(b.transact(packet(5,layer,3,0,code>>8,code&255))[0],0xff,hex(code))
        self.assertEqual(b.transact(packet(4,0,3,0))[4:6],bytes([0,0x39]))
        self.set_lt(b,layer=2,code=0x4329)

    def test_runtime_lt_shares_the_position_budget_with_mt_and_atomic_reject(self):
        b=self.board();self.set_lt(b,slot=66);self.set_mt(b,slot=67,code=0x224a)
        self.set_mt(b,slot=70,code=0x2428);self.set_mt(b,slot=71,code=0x282c)
        self.assertEqual(b.transact(packet(5,0,3,2,0x41,0x29))[0],0xff)
        self.assertEqual(b.transact(packet(4,0,3,2))[4:6],bytes([0,0x16]))
        # Same position cannot be LT in one layer and a different MT code in the other.
        self.assertEqual(b.transact(packet(5,1,3,0,0x21,0x29))[0],0xff)

    def test_copy_paste_cut_usages_are_accepted_and_reach_the_usb_report(self):
        for usage in (0x7b,0x7c,0x7d):
            b=self.board()
            self.assertEqual(b.transact(packet(5,0,3,0,0,usage))[0],5)
            self.assertEqual(b.transact(packet(4,0,3,0))[4:6],bytes([0,usage]))
            b.matrix(PHYS);b.drain();self.assertIn(usage,[r[2] for r in b.reports]);b.matrix();b.drain()
            self.assertEqual(b.held_keys(),set())

    def test_runtime_lt_reserved_slots_reject_and_remove_restores_plain_key(self):
        b=self.board()
        for slot in (19,20,41,42,63,64,85,86,15,21):
            r,c=divmod(slot,22)
            self.assertEqual(b.transact(packet(5,0,r,c,0x41,0x29))[0],0xff)
        self.set_lt(b)
        self.assertEqual(b.transact(packet(5,0,3,0,0,0x39))[0],5)
        b.matrix(PHYS);self.assertIn(0x39,b.held_keys());b.matrix()
        self.assertEqual(b.held_keys(),set())

    def test_usb_reconnect_rearms_startup_window_and_a_held_key_does_not_disable_the_firmware(self):
        b=self.board();state=b.sym['th_gate_state']
        epoch=struct.unpack('<H',b.read(state+2,2))[0]
        b.write(0x4003c4,b'\0');b.call('th_gate_scan',1);b.write(0x4003c4,b'\x01')
        self.assertEqual(b.read(state,1),b'\x00')
        self.assertEqual(struct.unpack('<H',b.read(state+2,2))[0],epoch+1)
        for _ in range(40):b.matrix(PHYS)        # a key held through the whole window
        self.assertEqual(b.read(state,1),b'\x01')
        b.matrix()

    def test_runtime_mt_capacity_and_cross_layer_conflict_reject_atomically(self):
        b=self.board();self.set_mt(b,slot=66);self.set_mt(b,slot=67,code=0x224a)
        self.set_mt(b,slot=70,code=0x2428);self.set_mt(b,slot=71,code=0x282c)      # four is the limit
        self.assertEqual(b.transact(packet(5,0,3,2,0x21,0x29))[0],0xff)
        self.assertEqual(b.transact(packet(4,0,3,2))[4:6],bytes([0,0x16]))
        self.assertEqual(b.transact(packet(5,1,3,0,0x22,0x29))[0],0xff)
        self.assertEqual(b.transact(packet(5,1,3,0,0x21,0x29))[0],5)
        # Bulk proposes a harmless change plus a fifth MT in one transaction.
        off=(66*2);old=b.transact(packet(0x12,off>>8,off&255,6))[4:10]
        self.assertEqual(b.transact(packet(0x13,off>>8,off&255,6,0x21,0x29,0x22,0x4a,0x21,0x29))[0],0xff)
        self.assertEqual(b.transact(packet(0x12,off>>8,off&255,6))[4:10],old)

    def test_runtime_four_tap_hold_keys_each_tap_and_hold_independently(self):
        keys=((66,0x2129,0x29,1),(67,0x222b,0x2b,2),(70,0x2428,0x28,4),(71,0x282c,0x2c,8))   # slot, MT code, tap, hold mods
        b=self.board()
        for slot,code,_,_ in keys:self.set_mt(b,slot=slot,code=code)
        for slot,_,tap,mods in keys:
            before=len(b.reports)
            b.matrix(slot);b.matrix();b.drain()
            self.assertEqual([tap in r[2:7] for r in b.reports[before:]],[True,False],hex(tap))
            for _ in range(5):b.matrix(slot)
            self.assertEqual(b.mods(),mods);b.matrix();b.drain()
            self.assertEqual((b.mods(),b.held_keys()),(0,set()))
        # All four held at once: every modifier is down, then everything releases cleanly.
        for _ in range(6):b.matrix(*(k[0] for k in keys))
        self.assertEqual(b.mods(),15);b.matrix();b.drain()
        self.assertEqual((b.mods(),b.held_keys(),b.faults()&2),(0,set(),0))   # bit 1 = tap queue overflow
        # Four quick taps in a row come out in order, one complete pulse each.
        before=len(b.reports)
        for slot,_,_,_ in keys:
            b.matrix(slot,submit=False);b.matrix(submit=False)
        b.drain(60)
        self.assertEqual([r[2] for r in b.reports[before:] if r[2]],[k[2] for k in keys])
        self.assertEqual(b.held_keys(),set())

    # ---- tap-hold flavors (custom value 18): 0 hold on press, 1 hold on nested tap, 2 timeout only ----
    TH,A,S=66,67,68                      # Caps position (MT: tap Esc 0x29, hold LCtrl), 'a' 0x04, 's' 0x16
    def flavored(self,flavor,term=30):
        b=self.board();self.set_mt(b)
        for k in range(4):b.write(b.sym['th_runtime_cfg']+k*6+4,struct.pack('<H',term))
        self.assertEqual(b.transact(packet(7,0,18,flavor))[0],7)
        self.assertEqual(b.transact(packet(8,0,18))[3],flavor)
        b.reports.clear()
        return b
    def typed(self,b):
        """Distinct keyboard report states in order: (modifiers, sorted keys)."""
        out=[]
        for r in b.reports:
            state=(r[0],tuple(sorted(k for k in r[2:8] if k)))
            if not out or out[-1]!=state:out.append(state)
        return out
    def idle(self,b):
        b.matrix();b.drain(40)
        self.assertEqual((b.mods(),b.held_keys(),b.faults()&2),(0,set(),0))
        self.assertEqual(b.transact(packet(8,0,18))[0],8)

    def test_flavor_setting_is_validated_and_refused_while_a_key_is_held(self):
        b=self.flavored(1)
        self.assertEqual(b.transact(packet(7,0,18,3))[0],0xff)
        b.matrix(self.A);self.assertEqual(b.transact(packet(7,0,18,2))[0],0xff);b.matrix();b.drain()
        self.assertEqual(b.transact(packet(8,0,18))[3],1)
        self.assertEqual(b.transact(packet(7,0,18,0))[0],7)

    def test_flavor_0_holds_at_the_first_other_key_press(self):
        b=self.flavored(0)
        b.matrix(self.TH);b.matrix(self.TH,self.A);b.matrix(self.A);self.idle(b)
        self.assertEqual(self.typed(b),[(1,(4,)),(0,(4,)),(0,())])

    def test_flavors_1_and_2_roll_gives_tap_then_the_other_key_in_order(self):
        for flavor in (1,2):
            b=self.flavored(flavor)
            b.matrix(self.TH);b.matrix(self.TH,self.A);b.matrix(self.A)     # TH released first
            for _ in range(12):b.matrix(self.A)
            self.idle(b)
            self.assertEqual(self.typed(b),[(0,(0x29,)),(0,()),(0,(4,)),(0,())],flavor)

    def test_flavor_1_nested_tap_of_another_key_makes_it_hold(self):
        b=self.flavored(1)
        b.matrix(self.TH);b.matrix(self.TH,self.A);b.matrix(self.TH,self.A)
        self.assertEqual(self.typed(b),[])                                  # nothing is sent while undecided
        b.matrix(self.TH)                                                  # 'a' released inside: hold
        for _ in range(12):b.matrix(self.TH)
        self.idle(b)
        self.assertEqual(self.typed(b),[(1,(4,)),(1,()),(0,())])

    def test_flavor_2_other_keys_do_not_decide_only_the_timeout_does(self):
        b=self.flavored(2)
        b.matrix(self.TH);b.matrix(self.TH,self.A);b.matrix(self.TH);b.matrix(self.TH,self.S)
        for _ in range(8):b.matrix(self.TH,self.S)
        self.assertEqual(self.typed(b),[])                                  # still inside the 30-pass timeout
        for _ in range(40):b.matrix(self.TH,self.S)
        self.idle(b)
        self.assertEqual(self.typed(b),[(1,(4,)),(1,()),(1,(0x16,)),(0,())])
        # A quick tap with a nested key tap is a tap, and the nested key still arrives, after it.
        b=self.flavored(2)
        b.matrix(self.TH);b.matrix(self.TH,self.A);b.matrix(self.TH);b.matrix();self.idle(b)
        self.assertEqual(self.typed(b),[(0,(0x29,)),(0,()),(0,(4,)),(0,())])

    def test_flavors_keep_the_order_of_several_held_back_keys(self):
        for flavor in (1,2):
            b=self.flavored(flavor)
            b.matrix(self.TH);b.matrix(self.TH,self.S);b.matrix(self.TH,self.S,self.A)   # s before a, although a has the lower index
            b.matrix(self.S,self.A)
            for _ in range(30):b.matrix(self.S,self.A)
            self.idle(b)
            self.assertEqual(self.typed(b),[(0,(0x29,)),(0,()),(0,(0x16,)),(0,(4,0x16)),(0,())],flavor)

    def test_flavor_1_key_pressed_before_the_tap_hold_key_does_not_count_as_nested(self):
        # First tap-hold tap is still on its way (keys are held back), then a second tap-hold key
        # goes down and the earlier key is released: that is not a tap "inside" the second one.
        b=self.flavored(1);self.set_mt(b,slot=70,code=0x222b)
        for k in range(4):b.write(b.sym['th_runtime_cfg']+k*6+4,struct.pack('<H',30))
        b.reports.clear()
        b.matrix(self.TH,submit=False);b.matrix(submit=False)              # tap queued, not yet sent
        b.matrix(self.A,submit=False);b.matrix(self.A,70,submit=False);b.matrix(70,submit=False)
        b.matrix(submit=False)                                             # second key released: its own tap
        self.idle(b)
        self.assertEqual([s for s in self.typed(b) if s[1]],[(0,(0x29,)),(0,(4,)),(0,(0x2b,))])
        self.assertTrue(all(m==0 for m,_ in self.typed(b)))

    def test_flavors_random_typing_never_loses_duplicates_or_sticks_a_key(self):
        import random
        plain={67:0x04,68:0x16,69:0x07}                    # a, s, d
        taps={self.TH:0x29,70:0x2b}                         # two mod-tap keys: Esc / Tab
        # Windows of 12 and 60 passes keep the 16-entry queue from overflowing at this (superhuman) rate.
        for flavor,term,seed in ((1,12,1),(1,60,2),(2,12,3),(2,60,4),(1,12,5),(2,60,6),(1,60,7),(2,12,8)):
            r=random.Random(seed)
            b=self.flavored(flavor,term);self.set_mt(b,slot=70,code=0x222b)
            for k in range(4):b.write(b.sym['th_runtime_cfg']+k*6+4,struct.pack('<H',term))
            b.reports.clear()
            down=set();presses={k:0 for k in list(plain)+list(taps)}
            for _ in range(400):
                key=r.choice(list(presses))
                if key in down:down.discard(key)
                else:down.add(key);presses[key]+=1
                for _ in range(r.choice((1,1,1,2,3,6,20))):b.matrix(*down)
            b.matrix();b.drain(200)
            self.assertEqual((b.mods(),b.held_keys(),b.faults()&2),(0,set(),0),(flavor,seed))
            edges={u:0 for u in list(plain.values())+list(taps.values())};last=set()
            for rep in b.reports:
                now={k for k in rep[2:8] if k}
                for u in now-last:edges[u]+=1
                last=now
            for slot,usage in plain.items():               # every ordinary press arrives exactly once
                self.assertEqual(edges[usage],presses[slot],(flavor,seed,hex(usage)))
            for slot,usage in taps.items():                # a tap-hold press is a tap or a hold, never both twice
                self.assertLessEqual(edges[usage],presses[slot],(flavor,seed,hex(usage)))

    def test_flavor_queue_overflow_degrades_without_stuck_keys(self):
        # 20 different keys pressed while the tap-hold key is undecided: the first 16 are held back,
        # the rest go straight through. Nothing sticks, and everything is released at the end.
        b=self.flavored(2,5000)
        keys=[22+i for i in range(12)]+[44+i for i in range(8)]
        down=[self.TH]
        b.matrix(*down)
        for k in keys:
            down.append(k);b.matrix(*down)
        seen=set()
        for rep in b.reports:seen|={k for k in rep[2:8] if k}
        self.assertTrue(seen)                                   # the overflow keys were not held back
        b.matrix();b.drain(300)
        self.assertEqual((b.mods(),b.held_keys(),b.faults()&2),(0,set(),0))
        self.assertEqual(b.transact(packet(7,0,18,0))[0],7)     # idle again: the setting can be changed

    def test_flavor_is_saved_with_the_profile_and_keeps_the_backlight_level(self):
        import test_persistence_device as pd
        b=pd.SavedBoard(self.elf,self.image)
        self.assertEqual(b.transact(packet(8,0,13))[3],0)
        b.transact(packet(7,0,6,3));self.assertEqual(b.transact(packet(7,0,18,2))[0],7)
        self.assertEqual(b.transact(packet(8,0,13))[3],1)
        self.assertEqual(b.transact(packet(7,0,1,1))[0],7);self.assertEqual(b.transact(packet(8,0,13))[3],0)
        fresh=pd.SavedBoard(self.elf,self.image,b.nv)
        self.assertEqual((fresh.transact(packet(8,0,18))[3],fresh.transact(packet(8,0,6))[3]),(2,3))
        self.assertEqual(fresh.transact(packet(8,0,13))[3],0)

    def test_runtime_mt_reserved_slots_and_gpio_holes_reject(self):
        b=self.board()
        for slot in (19,20,41,42,63,64,85,86,15,21):
            r,c=divmod(slot,22)
            self.assertEqual(b.transact(packet(5,0,r,c,0x21,0x29))[0],0xff)
        for slot in (19,20,41,42,63,64,85,86):
            r,c=divmod(slot,22)
            self.assertEqual(b.transact(packet(5,0,r,c,0,4))[0],0xff)

    def test_runtime_mt_pending_tap_blocks_remap_until_down_and_up_submit(self):
        b=self.board();self.set_mt(b)
        b.matrix(PHYS,submit=False);b.matrix(submit=False)
        self.assertEqual(b.transact(packet(5,0,3,0,0,0x39))[0],0xff)
        b.drain()
        self.assertEqual(b.transact(packet(5,0,3,0,0,0x39))[0],5)
        self.assertTrue(any(0x29 in r[2:7] for r in b.reports))

    def test_runtime_mt_rapid_repeats_queue_complete_usb_pulses(self):
        b=self.board();self.set_mt(b)
        for _ in range(3):
            b.matrix(PHYS,submit=False);b.matrix(submit=False)
        b.drain()
        self.assertEqual([0x29 in r[2:7] for r in b.reports],[True,False]*3)
        self.assertEqual(b.held_keys(),set());self.assertEqual(b.mods(),0)

    def test_runtime_two_mt_keys_serialize_queued_taps(self):
        b=self.board();self.set_mt(b);self.set_mt(b,slot=67,code=0x222b)
        for slot in (66,67):
            b.matrix(slot,submit=False);b.matrix(submit=False)
        b.drain()
        downs=[r[2] for r in b.reports if r[2]]
        self.assertEqual(downs,[0x29,0x2b]);self.assertEqual(b.mods(),0)
        self.assertEqual(b.held_keys(),set())

    def test_runtime_mt_banks_and_os_switch_require_release(self):
        b=self.board();self.set_mt(b)
        self.set_mt(b,layer=2,code=0x222b)
        for _ in range(4):b.matrix(PHYS)
        self.assertEqual(b.mods(),1)
        b.write(b.options+9,b'\x01');b.call('via_gate_loader')
        for _ in range(8):b.matrix(PHYS)
        self.assertEqual(b.mods(),0)
        b.matrix()
        for k in range(4):b.write(b.sym['th_runtime_cfg']+k*6+4,struct.pack('<H',4))
        for _ in range(4):b.matrix(PHYS)
        self.assertEqual(b.mods(),2);b.matrix();self.assertEqual(b.mods(),0)

    def test_real_get_descriptor_returns_unnumbered_raw_hid(self):
        b=self.board()
        b.setup(struct.pack('<BBHHH',0x81,6,0x2200,2,255))
        self.assertEqual(b.tx[0][-1],RAW_DESCRIPTOR)
        self.assertNotIn(b'\x85',RAW_DESCRIPTOR)

    def test_real_set_report_fifo_to_protocol_to_ep3(self):
        b=self.board()
        self.assertEqual(b.transact(packet(1)),packet(1,0,13))
        self.assertEqual(b.transact(packet(2,6)),packet(2,6,0,0,0,9))
        self.assertEqual(b.transact(packet(0x11)),packet(0x11,4))
        self.assertEqual(len(b.tx[1]),0)

    def test_opcode_0d_cannot_reach_stock_update_queue(self):
        b=self.board();b.write(0x40027c,b'\0'*4);b.write(0x40122c,b'\xa5'*17)
        self.assertEqual(b.transact(packet(0x0d)),packet(0x0d,0,0xc0))
        self.assertEqual(b.read(0x40027c,4),bytes(4))
        self.assertEqual(b.read(0x40122c,17),b'\xa5'*17)

    def test_legacy_update_entry_still_reaches_original_vendor_handler(self):
        b=self.board()
        raw=bytes([8,0x0d])+bytes(14)+bytes([0x40])
        b.output(raw,interface=1,report_id=8)
        self.assertEqual(b.read(0x40027c,1),b'\x01')
        self.assertEqual(b.read(0x40122c,17),raw)
        self.assertEqual(b.tx[3],[])

    def test_ram_write_changes_actual_stock_key_processing(self):
        b=self.board()
        self.assertEqual(b.transact(packet(4,0,3,1)),packet(4,0,3,1,0,4))
        self.assertEqual(b.transact(packet(5,0,3,1,0,5))[0],5)
        self.assertEqual(b.read(0x401c34+67*4,4),bytes.fromhex('02 05 00 4e'))
        b.matrix(67)
        self.assertIn(5,b.held_keys());self.assertNotIn(4,b.held_keys())
        b.matrix()
        self.assertEqual(b.held_keys(),set())

    def test_caps_is_factory_caps_lock_and_can_be_remapped_and_restored(self):
        for layer in (0,2):
            b=self.board()
            b.write(b.options+9,bytes([layer//2]));b.call('via_gate_loader')
            self.assertEqual(b.transact(packet(4,layer,3,0))[4:6],bytes.fromhex('00 39'))
            b.matrix(PHYS)
            self.assertIn(0x39,b.held_keys())
            self.assertEqual(b.mods(),0)
            for _ in range(210):b.matrix(PHYS)
            self.assertIn(0x39,b.held_keys());self.assertEqual(b.mods(),0)
            b.matrix()
            self.assertEqual(b.transact(packet(5,layer,3,0,0,0xe0))[0],5)
            b.matrix(PHYS);self.assertEqual(b.mods(),1)
            self.assertNotIn(0x39,b.held_keys());b.matrix()
            self.assertEqual(b.transact(packet(5,layer,3,0,0,0x39))[0],5)
            b.matrix(PHYS);self.assertIn(0x39,b.held_keys());b.matrix()
            self.assertEqual(b.held_keys(),set())

    def test_all_factory_banks_and_unused_slots_are_unmodified_in_rom(self):
        b=self.board()
        for at in TABLES:
            self.assertEqual(self.image[at:at+0x210],b.payload[at-0x2000:at-0x2000+0x210])
        self.assertLessEqual(self.manifest['sections'][0]['size'],0x140)   # gate may use 0x21c0..0x2300

    def test_mutation_rejected_while_any_real_key_is_active(self):
        b=self.board();b.matrix(68)
        self.assertEqual(b.transact(packet(5,0,3,0,0,0xe0))[0],0xff)
        b.matrix()
        self.assertEqual(b.transact(packet(5,0,3,0,0,0xe0))[0],5)

    def test_four_volatile_banks_survive_os_switch(self):
        b=self.board()
        b.transact(packet(5,0,3,1,0,5));b.transact(packet(5,2,3,1,0,6))
        self.assertEqual(b.read(0x401c34+67*4+1,1),b'\x05')
        b.write(b.options+9,b'\x01');b.call('via_gate_loader')
        self.assertEqual(b.read(0x401c34+67*4+1,1),b'\x06')
        b.write(b.options+9,b'\0');b.call('via_gate_loader')
        self.assertEqual(b.read(0x401c34+67*4+1,1),b'\x05')

    def test_endpoint_busy_preserves_response_until_host_ack(self):
        b=self.board();b.csr[3]=1
        b.output(packet(1));b.poll()
        self.assertEqual(b.tx[3],[])
        b.csr[3]=0;b.poll();self.assertEqual(b.receive(),packet(1,0,13))

    def test_bounded_isr_queue_preserves_request_order(self):
        b=self.board()
        for cmd in [packet(1),packet(0x11),packet(0x0d)]:b.output(cmd)
        for expected in [packet(1,0,13),packet(0x11,4),packet(0x0d,0,0xc0)]:
            b.poll();self.assertEqual(b.receive(),expected)

    def test_startup_window_with_broken_new_code_preserves_stock_restore_path(self):
        b=self.board();b.write(b.sym['th_gate_state'],b'\x00\x00');b.poison()
        b.call('via_gate_loader')
        b.call('via_gate_poll')
        raw=bytes([8,0x0d])+bytes(14)+bytes([0x40])
        b.output(raw,interface=1,report_id=8)
        self.assertEqual(b.read(0x40122c,17),raw)
        b.matrix(PHYS)
        self.assertIn(0x39,b.held_keys())
        self.assertEqual(b.mods(),0)

    def test_stale_reply_is_not_sent_after_usb_generation_changes(self):
        b=self.board();b.csr[3]=1
        b.output(packet(0x11));b.poll()
        self.assertEqual(b.tx[3],[])
        state=b.sym['th_gate_state']
        b.write(state+2,struct.pack('<H',1));b.csr[3]=0
        b.output(packet(1));b.poll()
        self.assertEqual(b.receive(),packet(1,0,13))

    def test_usb_reconnect_reloads_edited_banks_and_discards_old_queued_commands(self):
        b=self.board();b.transact(packet(5,0,3,1,0,5))
        b.output(packet(0x11))
        b.write(b.sym['th_gate_state']+2,struct.pack('<H',1))
        # Simulate stock reload while in bypass; reconnect must restore volatile edits.
        b.call(0x196cc)
        self.assertEqual(b.read(0x401c34+67*4+1,1),b'\x04')
        b.output(packet(1));b.poll()
        self.assertEqual(b.receive(),packet(1,0,13))
        self.assertEqual(b.read(0x401c34+67*4+1,1),b'\x05')

    def test_real_client_probe_and_roundtrip_against_firmware_usb_path(self):
        from via_device import Client
        b=self.board()
        class Device:
            def open_path(self,path):pass
            def get_report_descriptor(self):return RAW_DESCRIPTOR
            def close(self):pass
            def write(self,data):
                assert len(data)==33 and data[0]==0
                b.output(data[1:]);b.poll();return len(data)
            def read(self,n,timeout):return b.receive()
        class Hid:
            def enumerate(self,*ids):return [{'path':b'raw','usage_page':0xff60,'usage':0x61,'product_string':'Flow84@Lofree'}]
            def device(self):return Device()
        client=Client(Hid())
        self.assertEqual(client.probe()['protocol'],13)
        self.assertEqual(client.probe()['win_caps'],'0x39')
        result=client.roundtrip(0,3,1,5)
        self.assertEqual(result['before'],'0x4');self.assertEqual(result['temporary'],'0x5')
        self.assertEqual(b.read(0x401c34+67*4+1,1),b'\x04')

    def test_dead_stock_functions_hold_our_code_and_are_unreachable_from_stock(self):
        import build as builder,oe915
        from emulator import bl
        sizes={x['name']:x['size'] for x in self.manifest['sections']}
        owners={'.hole_a':'via_gate_loader','.hole_b':'via_gate_poll'}
        for name,(lo,size,sites) in builder.HOLES.items():
            self.assertTrue(0<sizes[name]<=size,name)
            for site in sites:      # every former caller now goes to our gate instead
                self.assertEqual(self.image[site:site+4],bl(site,self.symbols[owners[name]]),hex(site))
        for n,(lo,hi) in (('ds_pr',(0x19710,0x19818)),('th_busy',(0x19710,0x19818)),('ds_er',(0x1f99c,0x1fab0)),('physical_fn',(0x1f99c,0x1fab0))):
            self.assertTrue(lo<=(self.symbols[n]&~1)<hi,n)
        # A reference from stock code into a hole must fail the build.
        bad=bytearray(self.image);bad[0x1613e:0x16142]=bl(0x1613e,0x1f99c)
        with self.assertRaises(ValueError):builder.check_holes(oe915.load_stock(),bad)

    def test_only_pointing_interface_descriptor_changes(self):
        b=self.board()
        for at,n in [(0x227cc,77),(0x22819,106)]:
            self.assertEqual(self.image[at:at+n],b.payload[at-0x2000:at-0x2000+n])
        self.assertLessEqual(self.manifest['sections'][0]['size'],0x140)
        self.assertLess(int(self.manifest['bss_end'],16),0x406000)

if __name__=='__main__':unittest.main()
