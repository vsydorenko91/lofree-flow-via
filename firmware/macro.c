#include "macro.h"
typedef struct {
    uint8_t mods, n, keys[3];
    uint8_t pend;          /* last tapped key awaiting its release, or 0 */
} mstate;
typedef struct {
    uint8_t *out;
    unsigned max, used, start;
    int bad;
} mctx;
static int basic(unsigned v){return v>=4&&v<=0xa4;}
static int slots(const mstate *s){return (s->mods?1:0)+s->n;}
static void emit(mctx *c,const mstate *s,unsigned delay)
{
    uint8_t *r;unsigned i,k=0;
    if(slots(s)>3||c->used>=c->max){c->bad=1;return;}
    if(delay>65535u)delay=65535u;
    if(c->used==c->start&&delay&&delay<2)delay=2;
    r=c->out?c->out+c->used*MACRO_RECORD:0;
    ++c->used;
    if(!r)return;
    for(i=0;i<MACRO_RECORD;++i)r[i]=0;
    r[0]=(uint8_t)delay;r[1]=(uint8_t)(delay>>8);
    if(s->mods){r[2]=1;r[3]=s->mods;++k;}
    for(i=0;i<s->n;++i){r[2+k*3]=2;r[3+k*3]=s->keys[i];++k;}
}
/* Extend the hold time of the record emitted last (explicit delay action). */
static void add_delay(mctx *c,mstate *s,unsigned ms)
{
    uint8_t *r;unsigned d;
    if(!ms)return;                         /* a zero delay would end the macro */
    if(c->used==c->start){mstate e={0,0,{0,0,0},0};emit(c,&e,ms);return;}
    if(!c->out)return;
    r=c->out+(c->used-1)*MACRO_RECORD;
    d=(unsigned)(r[0]|r[1]<<8)+ms;if(d>65535u)d=65535u;
    r[0]=(uint8_t)d;r[1]=(uint8_t)(d>>8);
    (void)s;
}
static void release_pending(mstate *s)
{
    unsigned i;
    if(!s->pend)return;
    for(i=0;i<s->n;++i)if(s->keys[i]==s->pend){s->keys[i]=s->keys[--s->n];break;}
    s->pend=0;
}
static void add_key(mstate *s,unsigned kc)
{
    unsigned i;
    for(i=0;i<s->n;++i)if(s->keys[i]==kc)return;
    if(s->n<3)s->keys[s->n++]=(uint8_t)kc;else s->n=4;      /* 4 = overflow */
}
static void del_key(mstate *s,unsigned kc)
{
    unsigned i;
    for(i=0;i<s->n;++i)if(s->keys[i]==kc){s->keys[i]=s->keys[--s->n];return;}
}
/* kc: 8-bit usage or QMK 16-bit; act: 1 tap, 2 down, 3 up. */
static void do_key(mctx *c,mstate *s,unsigned act,unsigned kc)
{
    unsigned mods=0,key=kc&255,m;uint8_t added;
    if(kc>=0x100){
        if(kc>0x1fffu){c->bad=1;return;}
        m=(kc>>8)&15;mods=(kc&0x1000u)?m<<4:m;
    }else if(kc>=0xe0&&kc<=0xe7){mods=1u<<(kc-0xe0);key=0;}
    else if(!basic(kc)){c->bad=1;return;}
    if(key&&!basic(key)){c->bad=1;return;}
    if(act==3){
        release_pending(s);
        s->mods&=(uint8_t)~mods;
        if(key)del_key(s,key);
        emit(c,s,MACRO_TAP_MS);return;
    }
    /* A new press first releases the previous tap, unless it is a different key. */
    if(s->pend&&(act!=1||s->pend==key||!key))
        {release_pending(s);emit(c,s,MACRO_TAP_MS);}
    else release_pending(s);
    added=(uint8_t)(mods&~s->mods);
    s->mods|=(uint8_t)mods;
    if(key)add_key(s,key);
    if(s->n>3){c->bad=1;return;}
    emit(c,s,MACRO_TAP_MS);
    if(act==1){
        s->mods&=(uint8_t)~added;
        if(key){s->pend=(uint8_t)key;}
        else emit(c,s,MACRO_TAP_MS);
    }
}
/* US layout, printable ASCII 0x20..0x7e: keyboard usage, bit 7 = with Shift. */
static const uint8_t ascii_map[95]={
    0x2c,0x9e,0xb4,0xa0,0xa1,0xa2,0xa4,0x34,0xa6,0xa7,0xa5,0xae,0x36,0x2d,0x37,0x38,
    0x27,0x1e,0x1f,0x20,0x21,0x22,0x23,0x24,0x25,0x26,0xb3,0x33,0xb6,0x2e,0xb7,0xb8,
    0x9f,0x84,0x85,0x86,0x87,0x88,0x89,0x8a,0x8b,0x8c,0x8d,0x8e,0x8f,0x90,0x91,0x92,
    0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0x9b,0x9c,0x9d,0x2f,0x31,0x30,0xa3,0xad,
    0x35,0x04,0x05,0x06,0x07,0x08,0x09,0x0a,0x0b,0x0c,0x0d,0x0e,0x0f,0x10,0x11,0x12,
    0x13,0x14,0x15,0x16,0x17,0x18,0x19,0x1a,0x1b,0x1c,0x1d,0xaf,0xb1,0xb0,0xb5};
static int ascii(unsigned ch,unsigned *shift)
{
    unsigned v;
    *shift=0;
    if(ch=='\n')return 0x28;
    if(ch=='\t')return 0x2b;
    if(ch<0x20||ch>0x7e)return 0;
    v=ascii_map[ch-0x20];
    *shift=v&0x80u?2:0;
    return (int)(v&0x7fu);
}
static void type_char(mctx *c,mstate *s,unsigned ch)
{
    unsigned shift,key=(unsigned)ascii(ch,&shift);
    if(!key){c->bad=1;return;}
    do_key(c,s,1,shift?key|0x200u:key);
}
static void finish(mctx *c,mstate *s)
{
    mstate e={0,0,{0,0,0},0};
    if(s->pend||s->n||s->mods){
        s->pend=0;s->n=0;s->mods=0;
        emit(c,&e,MACRO_TAP_MS);
    }
    if(c->used==c->start)emit(c,&e,2);              /* never start with a terminator: it hangs the player */
    emit(c,&e,0);                                   /* terminator */
}
int macro_compile(const uint8_t *buf,unsigned len,uint8_t *out,unsigned max,
                  uint8_t lengths[MACRO_COUNT],unsigned *used)
{
    mctx c;mstate s;unsigned i=0,m=0,delay,act,kc;
    c.out=out;c.max=max;c.used=0;c.start=0;c.bad=0;
    s.mods=s.n=s.pend=0;s.keys[0]=s.keys[1]=s.keys[2]=0;
    for(m=0;m<MACRO_COUNT;++m){
        c.start=c.used;s.mods=s.n=s.pend=0;
        while(i<len&&buf[i]){
            unsigned b=buf[i++];
            if(b!=1){type_char(&c,&s,b);continue;}
            if(i>=len)break;
            act=buf[i++];
            if(act==4){
                unsigned j=i;delay=0;
                while(j<len&&buf[j]>='0'&&buf[j]<='9'){delay=delay*10+(buf[j]-'0');if(delay>65535u)delay=65535u;++j;}
                if(j>=len||buf[j]!='|'){i=len;break;}   /* incomplete */
                i=j+1;add_delay(&c,&s,delay);continue;
            }
            if(act>=1&&act<=3){if(i>=len)break;kc=buf[i++];do_key(&c,&s,act,kc);continue;}
            if(act>=5&&act<=7){if(i+1>=len)break;kc=buf[i]|(unsigned)buf[i+1]<<8;i+=2;do_key(&c,&s,act-4,kc);continue;}
            c.bad=1;
        }
        finish(&c,&s);
        lengths[m]=(uint8_t)(c.used-c.start);
        if(c.bad)return MACRO_BAD;
        if(i<len&&buf[i]==0)++i;
    }
    if(used)*used=c.used;
    return MACRO_OK;
}
