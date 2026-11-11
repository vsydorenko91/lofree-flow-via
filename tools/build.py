#!/usr/bin/env python3
"""Build the Lofree Flow84 VIA firmware image from stock + firmware/*.c. Never touches hardware."""
from pathlib import Path
import argparse,hashlib,json,struct,subprocess,sys
from elftools.elf.elffile import ELFFile
from capstone import Cs,CS_ARCH_ARM,CS_MODE_THUMB
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).resolve().parent))
import oe915
CODE_LIMIT=oe915.MAX_IMAGE_SIZE   # whole bootloader-accepted image; no code-flash NVM area is reserved
from emulator import bl
from oe915 import PAYLOAD_SHA
# Every call site of the OEM matrix scan; each is redirected through the gate.
MATRIX_CALLS=(0x17096,0x1906e,0x1b852,0x20232,0x206ce,0x20766,0x208be,
              0x20c3e,0x210f8,0x21a54,0x21b82,0x21d38,0x21fe0)
# Raw HID interface (usage page 0xff60, usage 0x61, 32-byte reports) that replaces the unused mouse interface.
RAW_DESCRIPTOR=bytes.fromhex('06 60 ff 09 61 a1 01 15 00 26 ff 00 75 08 95 20 09 62 81 02 95 20 09 63 91 02 c0')
# Stock functions whose only callers this build redirects; their bytes are reused for our code.
# name: (address, size, former call sites, the stock function they called)
HOLES={'.hole_a':(0x19710,0x108,(0x1b06a,)),
       '.hole_b':(0x1f99c,0x114,(0x1613e,0x1c462,0x1c914,0x1cc9a,0x20b5e))}
STOCK_MODE_SWITCH_CALL=bytes.fromhex('fef751fb')   # bl at 0x1b06a: Win/Mac switch loads the keymap from settings


def check_holes(stock,data):
    """A hole is safe only while nothing in the stock code can still reach it."""
    def target(d,a):
        h,l=struct.unpack_from('<HH',d,a)
        if h&0xf800==0xf000 and l&0xf800==0xf800:                    # bl
            off=((h&0x7ff)<<12)|((l&0x7ff)<<1);return a+4+off-(0x800000 if off&0x400000 else 0)
        if h&0xf800==0xe000:                                         # b
            off=(h&0x7ff)<<1;return a+4+off-(0x1000 if off&0x800 else 0)
        if h&0xf000==0xd000 and (h>>8)&15<14:                        # b<cond>
            off=(h&0xff)<<1;return a+4+off-(0x200 if off&0x100 else 0)
    for name,(lo,size,sites) in HOLES.items():
        hi=lo+size
        for a in sites:
            if target(stock,a)!=lo:raise ValueError(f'{name}: {a:#x} is not a stock call of {lo:#x}')
        for a in range(0x2000,len(stock)-3,2):
            if lo<=a<hi:continue
            t=target(data,a)
            if t is not None and lo<=t<hi:raise ValueError(f'{name}: stock code at {a:#x} still reaches {t:#x}')
        for a in range(0x2000,len(stock)-3,4):                       # function pointers and literals
            if lo<=a<hi:continue
            if lo<=(struct.unpack_from('<I',stock,a)[0]&~1)<hi:raise ValueError(f'{name}: pointer at {a:#x}')


# --flash-probe: spare flash words used by the hardware experiment in firmware/adapter.c.
PROBE_START=0x27d00


def build(out,term=200,*,lto=True,flash_probe=False):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    stock=oe915.load_stock();payload=stock[0x2000:]
    if hashlib.sha256(payload).hexdigest()!=PAYLOAD_SHA:raise ValueError('wrong firmware payload')
    elf_path=out/'flow84-via.elf'
    linker_path=out/'device-limit.ld'
    linker_path.write_text(f'FLOW_MAX_IMAGE_SIZE = {oe915.MAX_IMAGE_SIZE};\nFLOW_MAX_CODE_SIZE = {CODE_LIMIT};\n'
                           +(HERE/'firmware/device.ld').read_text())
    cmd=[sys.executable,'-m','ziglang','cc','-target','thumb-freestanding-eabi','-mcpu=arm7tdmi','-mthumb',
         '-Oz','-fno-unwind-tables','-fno-asynchronous-unwind-tables','-g','-Wall','-Wextra','-Werror','-ffreestanding','-fno-builtin','-fno-stack-protector','-nostdlib',
         '-Wl,-T,'+str(linker_path),'-Wl,-e,th_gate_matrix','-Wl,--no-gc-sections',
         
         '-I'+str(HERE/'firmware'),str(HERE/'firmware/th.c'),str(HERE/'firmware/gate.S'),
         str(HERE/'firmware/protocol.c'),str(HERE/'firmware/adapter.c'),str(HERE/'firmware/dstore.c'),str(HERE/'firmware/macro.c'),'-o',str(elf_path)]
    if lto:cmd[4:4]=["-flto"]
    if flash_probe:cmd[4:4]=['-DFLOW_FLASH_PROBE']
    subprocess.run(cmd,check=True)
    with elf_path.open('rb') as f:
        e=ELFFile(f)
        symbols={s.name:s['st_value'] for s in e.get_section_by_name('.symtab').iter_symbols()}
        sections=[(s.name,s['sh_addr'],s['sh_size'],None if s['sh_type']=='SHT_NOBITS' else s.data())
                  for s in e.iter_sections() if s['sh_flags']&2]
    data=bytearray(stock);changes=[]
    def patch(at,before,after,reason):
        if data[at:at+len(before)]!=before or len(before)!=len(after):raise ValueError(f'patch mismatch {at:#x}')
        data[at:at+len(before)]=after
        changes.append({'address':hex(at),'size':len(after),'before':before.hex(),'after':after.hex(),'reason':reason})
    for name,a,n,raw in sections:
        if raw is None:continue
        if a+n>CODE_LIMIT:raise ValueError('allocated flash section exceeds image safety limit')
        if name=='.th_gate':patch(a,bytes(n),raw,'hook gates in original zero padding')
        elif name in HOLES:
            if a!=HOLES[name][0] or n>HOLES[name][1]:raise ValueError(f'{name} does not fit its stock hole')
            if n:patch(a,bytes(stock[a:a+n]),raw,f'our code in the dead stock function at {a:#x}')
        else:
            if a<len(stock):raise ValueError(f'unexpected section {name}')
            if len(data)<a:data.extend(bytes(a-len(data)))
            if len(data)!=a:raise ValueError('overlapping appended section')
            data.extend(raw)
    hooks=[(a,0x1beb4,'th_gate_matrix') for a in MATRIX_CALLS]
    # Exact observed complete EP0 OUT -> ACK path; subsequent stock vendor routing remains intact.
    hooks += [(0x1bfce,0x19898,'th_gate_event'),(0x1f84c,0x121ec,'th_gate_usb'),
              (0x1b3e2,0x1b488,'th_gate_scan'),(0x17fa8,0x18248,'via_gate_ep0'),
              (0x1aa66,0x196cc,'via_gate_loader')]
    for a in (0x1613e,0x1c462,0x1c914,0x1cc9a,0x20b5e):hooks.append((a,0x1f99c,'via_gate_poll'))
    dis=Cs(CS_ARCH_ARM,CS_MODE_THUMB)
    sites=[i.address for i in dis.disasm(payload[0x1beb4-0x2000:0x1c144-0x2000],0x1beb4)
           if i.mnemonic=='bl' and i.op_str=='#0x166f0']
    if len(sites)!=1:raise ValueError('ambiguous activity call')
    hooks.append((sites[0],0x166f0,'th_gate_activity'))
    for a,old,target in hooks:patch(a,bl(a,old),bl(a,symbols[target]),target)
    patch(0x1b06a,STOCK_MODE_SWITCH_CALL,bl(0x1b06a,symbols['via_gate_loader']),'RAM bank loader on OS switch')
    # Selector-1 sector A is the OEM journal's BACKUP buffer: stock code erases it at
    # every boot and during rotation, which wiped our profile on power cycle.
    # Remove those uses (the only loss is the OEM journal's power-cut protection).
    patch(0x1e3fa,bytes.fromhex('53d1'),bytes.fromhex('53e0'),'OEM boot: never run sector-A backup recovery/erase (bne -> b)')
    patch(0x1672c,stock[0x1672c:0x1672e],bytes.fromhex('7047'),'OEM: never erase sector A (function returns at once)')
    for site,callee in ((0x1f450,0x19660),(0x1f45c,0x1b9a4),(0x1f5a4,0x19660),(0x1f5b0,0x1b9a4)):
        patch(site,bl(site,callee),b'\xc0\x46'*2,f'OEM journal rotation: no sector-A backup call {callee:#x}')
    # The stock mouse sender (0x1f99c) is never called any more: all five call sites go to via_gate_poll.
    patch(0x1c14c,struct.pack('<I',0x401aec),struct.pack('<I',symbols['th_shadow']),'private matrix')
    end=(max(a+n for name,a,n,raw in sections if raw is None)+3)&~3
    patch(0x21b4,struct.pack('<I',0x45ec),struct.pack('<I',end-0x4007d8),'initialize all new BSS')
    for site,old in ((0x1d304,0x22b18),(0x1ee3c,0x22b18),(0x1a024,0x237ee)):
        patch(site,struct.pack('<I',old),struct.pack('<I',symbols['macro_table']),'OEM macro table literal -> RAM table')
    # Replace the unused stock pointing interface with a dedicated Raw HID interface.
    config=bytearray(stock[0x22778:0x22778+84]);off=0;interface=None
    while off<len(config):
        size,kind=config[off:off+2]
        if not size:raise ValueError('bad descriptor')
        if kind==4:
            interface=config[off+2]
            if interface==2:config[off+6]=0;config[off+7]=0
        elif kind==0x21 and interface==2:config[off+7:off+9]=struct.pack('<H',len(RAW_DESCRIPTOR))
        elif kind==5 and interface==2:config[off+4:off+6]=struct.pack('<H',32)
        off+=size
    patch(0x22778,stock[0x22778:0x22778+84],config,'IF2 Raw HID, EP3 max packet 32')
    patch(0x22883,stock[0x22883:0x22883+87],RAW_DESCRIPTOR+bytes(87-len(RAW_DESCRIPTOR)),'unnumbered 32-byte input/output Raw HID')
    patch(0x228f4,struct.pack('<I',87),struct.pack('<I',len(RAW_DESCRIPTOR)),'IF2 report length table')
    # Endpoint2 uses CSR+0x10 value4 for32B. The same value is live in r5 at EP3 init.
    patch(0x18322,struct.pack('<H',0x7402),struct.pack('<H',0x7405),'EP3 buffer config: strb r5,#0x10 (value4)')
    check_holes(stock,data)
    if flash_probe and len(data)>PROBE_START:raise ValueError('image overlaps the flash-probe words')
    oe915.validate_image_size(data)
    data=bytearray(oe915.seal(data))
    list(oe915.upgrade_reports(bytes(data)))
    image=out/'flow84-via.bin';image.write_bytes(data)
    manifest={'image':image.name,'sha256':hashlib.sha256(data).hexdigest(),'stock_sha256':oe915.STOCK_SHA,
              'old_size':len(stock),'new_size':len(data),'flash_probe':bool(flash_probe),'bss_end':hex(end),'lto':bool(lto),
              'sections':[{'name':name,'address':hex(a),'size':n} for name,a,n,raw in sections],
              'symbols':symbols,'changes':changes,'raw_descriptor':RAW_DESCRIPTOR.hex()}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return bytes(data),symbols,manifest

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,default=HERE/'out');p.add_argument('--no-lto',action='store_true')
    p.add_argument('--flash-probe',action='store_true',help='experimental image for the code-flash write experiment; goes to out/probe unless --out is given')
    a=p.parse_args()
    if a.flash_probe and a.out==HERE/'out':a.out=HERE/'out/probe'
    _,_,m=build(a.out,lto=not a.no_lto,flash_probe=a.flash_probe)
    print(json.dumps({k:m[k] for k in ('sha256','new_size','bss_end')},indent=2))
