#!/usr/bin/env python3
"""Short-lived diagnostics for manual Save; close VIA before HIDAPI access."""
import argparse,hashlib,json,struct,time
from pathlib import Path
from via_device import Client,DeviceError
from via_proto import packet


def read_map(c):
    out=bytearray()
    for off in range(0,1056,28):
        n=min(28,1056-off)
        out.extend(c.exchange(packet(0x12,off>>8,off&255,n))[4:4+n])
    return bytes(out)

def status(c):return c.exchange(packet(8,0,2))[3:].split(b'\0')[0].decode()

def telemetry(c):
    r=c.exchange(packet(8,0,3))
    if r[:4]!=bytes([8,0,3,1]):raise DeviceError('unexpected Save telemetry ABI')
    fields=struct.unpack_from('>5H',r,5)
    return dict(zip(('attempts','erase_calls','program_calls','last_offset','last_length'),fields),
                stage=r[4],backend_ok=bool(r[15]),saving=bool(r[16]),passes=struct.unpack_from('>H',r,17)[0])

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['backup','restore','save','status','telemetry','dump','rate'])
    p.add_argument('--file',type=Path)
    a=p.parse_args()
    if a.command in ('backup','restore') and a.file is None:p.error('--file required')
    import hid
    c=Client(hid)
    try:
        if a.command=='backup':
            raw=read_map(c)
            value={'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),'keymap_hex':raw.hex()}
            with a.file.open('x') as f:f.write(json.dumps(value,indent=2)+'\n')
            print(json.dumps({k:v for k,v in value.items() if k!='keymap_hex'}))
        elif a.command=='restore':
            obj=json.loads(a.file.read_text());raw=bytes.fromhex(obj['keymap_hex'])
            if len(raw)!=1056 or hashlib.sha256(raw).hexdigest()!=obj['sha256']:raise DeviceError('invalid snapshot')
            for off in range(0,1056,28):
                chunk=raw[off:off+28]
                c.exchange(packet(0x13,off>>8,off&255,len(chunk),*chunk))
            if read_map(c)!=raw:raise DeviceError('restored map differs')
            print(json.dumps({'RAM_restored':True,'sha256':obj['sha256'],'flash_saved':False}))
        elif a.command=='save':
            before=status(c);raw=read_map(c);t=time.perf_counter()
            print(json.dumps({'before':before,'telemetry_before':telemetry(c)}),flush=True)
            c.exchange(packet(7,0,1,1),timeout_ms=15000)
            elapsed=time.perf_counter()-t
            if read_map(c)!=raw:raise DeviceError('Save changed RAM map')
            print(json.dumps({'before':before,'after':status(c),'save_roundtrip_seconds':elapsed,'map_sha256':hashlib.sha256(raw).hexdigest(),'telemetry_after':telemetry(c)}))
        elif a.command=='rate':
            # Matrix-processing passes per second (the unit of the tap-hold timeout).
            a0=telemetry(c)['passes'];t=time.perf_counter();time.sleep(3)
            n=(telemetry(c)['passes']-a0)&0xffff
            print(json.dumps({'passes_per_second':round(n/(time.perf_counter()-t),1),'note':'run once idle and once while holding a key'}))
        elif a.command=='telemetry':print(json.dumps(telemetry(c)))
        elif a.command=='dump':
            for slot in range(17):
                rec=c.exchange(packet(8,0,12,slot))[3:31]
                if rec==b'\xff'*28:print(f'slot {slot:2}: empty');break
                tag,n,flags=rec[0],rec[2],rec[3]
                if tag==0xc7:
                    ents=[(rec[8+i*4]|rec[9+i*4]<<8,rec[10+i*4]|rec[11+i*4]<<8) for i in range(n)]
                    print(f'slot {slot:2}: keys flags={flags:#x} level={rec[4]:#x} light={rec[5]:#x} entries={[(p_,hex(v)) for p_,v in ents]}')
                elif tag==0xc9:print(f'slot {slot:2}: macro flags={flags:#x} off={rec[4]|rec[5]<<8} data={bytes(rec[8:8+n])!r}')
                else:print(f'slot {slot:2}: unknown {rec.hex()}')
        else:
            v=struct.unpack('>I',c.exchange(packet(2,4))[2:6])[0]
            print(json.dumps({'status':status(c),'firmware':f'{v>>16}.{v>>8&255}.{v&255}','map_sha256':hashlib.sha256(read_map(c)).hexdigest()}))
    finally:c.close()

if __name__=='__main__':main()
