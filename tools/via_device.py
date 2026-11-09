#!/usr/bin/env python3
"""Real VIA Raw HID client. Never opens the stock vendor/update interface.

inspect: descriptors only. probe: read-only VIA queries. roundtrip: explicit
RAM-only mutation followed by restoration in finally; no persistent writes.
"""
import argparse,json,struct,time
from oe915 import NORMAL_IDS
RAW_USAGE_PAGE,RAW_USAGE=0xff60,0x61
RAW_DESCRIPTOR=bytes.fromhex('06 60 ff 09 61 a1 01 15 00 26 ff 00 75 08 95 20 09 62 81 02 95 20 09 63 91 02 c0')
from via_proto import packet
class DeviceError(RuntimeError):pass

def locate(hid):
    found={}
    for d in hid.enumerate(*NORMAL_IDS):
        if d.get('usage_page')==RAW_USAGE_PAGE and d.get('usage')==RAW_USAGE and d.get('product_string')=='Flow84@Lofree':
            found[d['path']]=d
    if len(found)!=1:raise DeviceError(f'expected one Flow84 Raw HID interface, found {len(found)}')
    return next(iter(found.values()))

class Client:
    def __init__(self,hid):
        self.device=hid.device();self.descriptor_verified=False
        try:
            self.device.open_path(locate(hid)['path'])
            if bytes(self.device.get_report_descriptor())!=RAW_DESCRIPTOR:
                raise DeviceError('Raw HID descriptor mismatch; no command sent')
            self.descriptor_verified=True
        except BaseException:self.device.close();raise
    def close(self):self.device.close()
    def exchange(self,request,timeout_ms=2000):
        if not self.descriptor_verified or not isinstance(request,bytes) or len(request)!=32:
            raise DeviceError('exact 32-byte request and verified Raw HID interface required')
        # The prefix belongs to HIDAPI, not the unnumbered 32-byte payload.
        wire=b'\0'+request
        if self.device.write(wire)!=33:raise DeviceError('short Raw HID write')
        deadline=time.monotonic()+timeout_ms/1000
        while True:
            remaining=max(1,int((deadline-time.monotonic())*1000))
            result=bytes(self.device.read(64,remaining))
            if len(result)==32 and result[:2]==b'\x16\x01':
                if time.monotonic()>=deadline:raise DeviceError('timeout during UI refresh events')
                continue
            break
        if len(result)!=32:raise DeviceError(f'expected32-byte response, got {len(result)}')
        if result[0]==0xff:raise DeviceError('firmware rejected unsupported/reserved/busy mutation')
        if result[0]!=request[0]:raise DeviceError('response command mismatch')
        return result
    def get(self,layer,row,col):
        return struct.unpack_from('>H',self.exchange(packet(4,layer,row,col)),4)[0]
    def set(self,layer,row,col,code):
        self.exchange(packet(5,layer,row,col,code>>8,code&255))
    def probe(self):
        version=struct.unpack_from('>H',self.exchange(packet(1)),1)[0]
        dictionary=struct.unpack_from('>I',self.exchange(packet(2,6)),2)[0]
        layers=self.exchange(packet(0x11))[1]
        if (version,dictionary,layers)!=(13,9,4):raise DeviceError('unexpected protocol/dictionary/layer count')
        return {'protocol':version,'keycodes_version':dictionary,'layers':layers,
                'win_A':hex(self.get(0,3,1)),'mac_A':hex(self.get(2,3,1)),
                'win_caps':hex(self.get(0,3,0)),'mac_caps':hex(self.get(2,3,0)),
                'macro_buffer_bytes':struct.unpack_from('>H',self.exchange(packet(0x0d)),1)[0],
                'transport':'real Raw HID; no stock vendor commands sent'}
    def roundtrip(self,layer,row,col,code):
        original=self.get(layer,row,col)
        mutation_attempted=False
        try:
            mutation_attempted=True
            self.set(layer,row,col,code)
            readback=self.get(layer,row,col)
            if readback!=code:raise DeviceError('RAM write did not read back')
            return {'before':hex(original),'temporary':hex(readback),'position':[layer,row,col]}
        finally:
            if mutation_attempted:
                self.set(layer,row,col,original)
                if self.get(layer,row,col)!=original:raise DeviceError('RAM restoration did not read back')


def integer(s):return int(s,0)
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['inspect','probe','roundtrip'])
    p.add_argument('--layer',type=int,default=0);p.add_argument('--row',type=int,default=3);p.add_argument('--col',type=int,default=1)
    p.add_argument('--keycode',type=integer,default=5)
    a=p.parse_args()
    if not (0<=a.layer<4 and 0<=a.row<6 and 0<=a.col<22 and 0<=a.keycode<=65535):p.error('position/keycode out of range')
    import hid
    c=Client(hid)
    try:
        result={'descriptor_verified':True,'commands_sent':False} if a.command=='inspect' else c.probe()
        if a.command=='roundtrip':
            result['roundtrip']=c.roundtrip(a.layer,a.row,a.col,a.keycode)
            result['roundtrip']['restored']=True
        print(json.dumps(result,indent=2))
    finally:c.close()

if __name__=='__main__':main()
