#include "version.h"
#include "protocol.h"

static const uint8_t consumer_code[10] = {0xa8,0xa9,0xaa,0xab,0xac,0xae,0xb2,0xb4,0xbd,0xbe};
static const uint16_t consumer_usage[10] = {0xe2,0xe9,0xea,0xb5,0xb6,0xcd,0x192,0x221,0x6f,0x70};
/* Fixed semantic order for authenticated OE915 v2.52: custom0..8 are type5,
 * custom9 is type9/value4 (Win+Tab). No dynamic registry or RAM sorting. */
#define FACTORY_COUNT 10u
static const uint8_t factory_value[9] = {0,1,2,4,7,15,16,24,25};
static unsigned bank(uint16_t i) { return i < 132 ? 0 : i < 264 ? 1 : i < 396 ? 2 : 3; }
static uint16_t word(const uint8_t *p) { return (uint16_t)((uint16_t)p[0] << 8 | p[1]); }
static void putword(uint8_t *p, uint16_t v) { p[0]=(uint8_t)(v>>8); p[1]=(uint8_t)v; }
static int valid(const uint8_t *p) { return (uint8_t)(p[0]+p[1]+p[2]+p[3]) == 0x55; }
static uint16_t value(const uint8_t *p) { return (uint16_t)(p[1] | (uint16_t)p[2]<<8); }
static int usage(uint16_t v) { return v>=4 && v<=0xa4; }
int flow_via_is_mod_tap(uint16_t code) {
    return code>=0x2000 && code<=0x3fff && ((code>>8)&15)!=0 && usage(code&255);
}
int flow_via_is_layer_tap(uint16_t code, unsigned bank) {
    return (code&0xf000)==0x4000 && !(bank&1) && ((code>>8)&15)==bank+1u && usage(code&255);
}
static unsigned mods(uint16_t v) {
    if (v && v<=15) return v;
    if (v && v<=0xf0 && !(v&15)) return 0x10 | (v>>4);
    return 0;
}
static int standard(const uint8_t *a, unsigned layer, uint16_t *code) {
    unsigned k=a[0]&0x7f, n; uint16_t v=value(a);
    if (!k && !v) *code=0;
    else if (k==2 && usage(v)) *code=v;
    else if (k==FLOW_VIA_MT_KIND && flow_via_is_mod_tap(v)) *code=v;
    else if (k==FLOW_VIA_LT_KIND && flow_via_is_layer_tap(v,layer)) *code=v;
    else if (k==9 && v>=FLOW_VIA_MACRO_BASE && v<FLOW_VIA_MACRO_BASE+FLOW_VIA_MACRO_MAX) *code=(uint16_t)(0x7700+v-FLOW_VIA_MACRO_BASE);
    else if (k==1) {
        if (v && v<=0x80 && !(v&(v-1))) {
            n=0; while (v>1) { v>>=1; ++n; } *code=(uint16_t)(0xe0+n);
        } else { n=mods(v); if (!n) return 0; *code=(uint16_t)(n<<8); }
    } else if (k==3) {
        for(n=0;n<10;++n) if(v==consumer_usage[n]) { *code=consumer_code[n]; return 1; }
        return 0;
    } else if(k==6 && v==2) *code=layer<2 ? 0x5221 : 0x5223;
    else if(k==7 && usage(v>>8)) {
        n=mods(v&255); if(!n) return 0; *code=(uint16_t)((n<<8)|(v>>8));
    } else return 0;
    return 1;
}
static unsigned factory_index(const uint8_t *a) {
    unsigned n,k=a[0]&127; uint16_t v=value(a);
    if(k==9 && v==4) return 9;
    if(k==5) for(n=0;n<9;++n) if(v==factory_value[n]) return n;
    return FACTORY_COUNT;
}
static int encode(const uint8_t *a,unsigned layer,uint16_t *code) {
    unsigned n; if (!valid(a)) return 0;
    if(standard(a,layer,code)) return 1;
    n=factory_index(a); if(n==FACTORY_COUNT) return 0;
    *code=(uint16_t)(0x7e00+n); return 1;
}
int flow_via_decode_keycode(flow_via *s,uint16_t i,uint16_t code,uint8_t *a) {
    uint8_t original[4]; unsigned k,n,m; uint16_t v;
    if(!s->read(s->user,i,1,original) || !valid(original)) return 0;
    if(code>=0x7e00 && code<0x7e00+FACTORY_COUNT) {
        n=code-0x7e00;
        if(factory_index(original)!=n) return 0;
        for(n=0;n<4;++n) a[n]=original[n];
        return 1;
    }
    if(!code) { k=0; v=0; }
    else if(usage(code)) { k=2; v=code; }
    else if(flow_via_is_mod_tap(code)) { k=FLOW_VIA_MT_KIND; v=code; }
    else if(flow_via_is_layer_tap(code,bank(i))) { k=FLOW_VIA_LT_KIND; v=code; }
    else if(code>=0x7700 && code<0x7700+FLOW_VIA_MACRO_MAX) { k=9; v=(uint16_t)(FLOW_VIA_MACRO_BASE+code-0x7700); }
    else if(code>=0xe0 && code<=0xe7) { k=1; v=(uint16_t)(1u<<(code-0xe0)); }
    else if(code==(bank(i)<2 ? 0x5221 : 0x5223)) { k=6; v=2; }
    else {
        for(n=0;n<10;++n) if(code==consumer_code[n]) break;
        if(n<10) { k=3; v=consumer_usage[n]; }
        else if(code>=0x100 && code<=0x1fff) {
            m=(code>>8)&15; if(!m) return 0;
            v=(uint16_t)((code&0x1000) ? m<<4 : m);
            if(!(code&255)) k=1;
            else if(usage(code&255) && v<=15) { k=7; v=(uint16_t)(v|((code&255)<<8)); }
            else return 0;
        } else return 0;
    }
    a[0]=(uint8_t)(k|(original[0]&0x80)); a[1]=(uint8_t)v; a[2]=(uint8_t)(v>>8);
    a[3]=(uint8_t)(0x55-a[0]-a[1]-a[2]); return 1;
}
int flow_via_init(flow_via *s,void *user,flow_via_read read,flow_via_commit commit,
                  flow_via_validate validate,flow_via_reset reset,uint32_t now) {
    uint16_t i,c; uint8_t a[4];
    if(!s) return 0;
    s->ready=0; s->indication=0;
    s->user=user; s->macros=0; s->read=read; s->commit=commit; s->validate=validate; s->reset=reset; s->started=now;
    if(!read || !commit) return 0;
    for(i=0;i<FLOW_VIA_ENTRIES;++i) {
        if(!read(user,i,1,a) || !encode(a,bank(i),&c)) return 0;
    }
    s->ready=1; return 1;
}
int flow_via_get_keycode(flow_via *s,uint16_t i,uint16_t *code) {
    uint8_t a[4]; return s->read(s->user,i,0,a) && encode(a,bank(i),code);
}
int flow_via_get_factory_keycode(flow_via *s,uint16_t i,uint16_t *code) {
    uint8_t a[4]; return s->read(s->user,i,1,a) && encode(a,bank(i),code);
}
static int allowed(flow_via *s,uint16_t i,const uint8_t *a) {
    return !s->validate || s->validate(s->user,i,a);
}
static int macro_cmd(flow_via *s,const uint8_t *q,uint8_t *r) {
    const flow_via_macros *m=s->macros; uint16_t off; unsigned n,i;
    if(!m) return 0;
    if(q[0]==0x10) return m->reset && m->reset(s->user);
    off=word(q+1); n=q[3];
    if(n>28 || off>m->size) return 0;
    /* VIA transfers fixed 28-byte chunks, so the last one may overrun the
     * buffer: like QMK, clamp it (reads return zeros past the end). */
    if(n>(unsigned)m->size-off) n=(unsigned)m->size-off;
    if(q[0]==0x0e) {
        for(i=4;i<32;++i) r[i]=0;
        return !n || (m->read && m->read(s->user,off,(uint8_t)n,r+4));
    }
    return !n || (m->write && m->write(s->user,off,(uint8_t)n,q+4));
}
static int bulk(flow_via *s,const uint8_t *q,uint8_t *r) {
    uint16_t off=word(q+1),end,first,last,i,c,p; unsigned n=q[3],j,count;
    uint8_t a[FLOW_VIA_MAX_BATCH][4], bytes[30];
    if(n>28 || off>1056 || n>1056-off) return 0;
    end=(uint16_t)(off+n);
    /* Python reference normalizes the enclosing word even on an empty odd
     * offset write. Keep that behavior, with the same guard callbacks. */
    first=off>>1; last=(uint16_t)((end+1)>>1); count=last-first;
    for(i=first;i<last;++i) {
        if(!flow_via_get_keycode(s,i,&c)) return 0;
        j=(unsigned)(i-first)*2; putword(bytes+j,c);
    }
    p=off&1;
    if(q[0]==0x12) { for(j=0;j<n;++j) r[4+j]=bytes[p+j]; return 1; }
    for(j=0;j<n;++j) bytes[p+j]=q[4+j];
    for(j=0;j<count;++j) {
        i=(uint16_t)(first+j);
        if(!flow_via_decode_keycode(s,i,word(bytes+j*2),a[j]) || !allowed(s,i,a[j])) return 0;
    }
    return !count || s->commit(s->user,first,(uint8_t)count,a);
}
int flow_via_handle(flow_via *s,const uint8_t *q,size_t len,uint8_t r[32],uint32_t now) {
    uint8_t request[32],a[1][4]; uint16_t i,c; uint32_t v; unsigned n; int ok=1;
    if(!s || !s->ready || !q || !r || len!=32) return 0;
    for(n=0;n<32;++n) { request[n]=q[n]; r[n]=q[n]; } q=request;
    switch(q[0]) {
    case 1: putword(r+1,13); break;
    case 2:
        if(q[1]==3) {
            if(q[2]>=6) {ok=0; break;}
            for(n=0;n<(6u-q[2])*3;++n) r[3+n]=0;
            break;
        }
        if(q[1]==1) v=now-s->started;
        else if(q[1]==2) v=0;
        else if(q[1]==4) v=FLOW_FW_VERSION;
        else if(q[1]==6) v=9;
        else {ok=0;break;}
        r[2]=(uint8_t)(v>>24); r[3]=(uint8_t)(v>>16); r[4]=(uint8_t)(v>>8); r[5]=(uint8_t)v; break;
    case 3:
        if(q[1]==2) ok=!(q[2]|q[3]|q[4]|q[5]);
        else if(q[1]==5) s->indication=q[2];
        else ok=0;
        break;
    case 4: case 5:
        if(q[1]>=4 || q[2]>=6 || q[3]>=22) {ok=0;break;}
        i=(uint16_t)((q[1]*6u+q[2])*22u+q[3]);
        if(q[0]==4) {ok=flow_via_get_keycode(s,i,&c); if(ok) putword(r+4,c);}
        else {ok=flow_via_decode_keycode(s,i,word(q+4),a[0]) && allowed(s,i,a[0]); if(ok) ok=s->commit(s->user,i,1,a);}
        break;
    case 6: ok=s->reset && s->reset(s->user); break;
    case 0x0c: r[1]=s->macros?s->macros->count:0; break;
    case 0x0d: if(s->macros) putword(r+1,s->macros->size); else r[1]=r[2]=0; break;
    case 0x0e: case 0x0f: case 0x10: ok=macro_cmd(s,q,r); break;
    case 0x11: r[1]=4; break;
    case 0x12: case 0x13: ok=bulk(s,q,r); break;
    default: ok=0; break;
    }
    if(!ok) {for(n=0;n<32;++n) r[n]=q[n]; r[0]=0xff;}
    return 1;
}
