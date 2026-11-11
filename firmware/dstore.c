#include "dstore.h"
#define TAG_KEY 0xc7u
#define TAG_BLOB 0xc9u
#define F_SNAP 1u
#define F_MORE 2u
#define F_FIRST 4u
#define F_BLOBRST 8u
static uint16_t crc16(const uint8_t *r)
{
    uint16_t c=0xffffu;unsigned i,k;
    for(i=0;i<DS_REC;++i){
        if(i==6||i==7)continue;
        c^=(uint16_t)r[i]<<8;
        for(k=0;k<8;++k)c=c&0x8000u?(uint16_t)((c<<1)^0x1021u):(uint16_t)(c<<1);
    }
    return c;
}
static int blank(const uint8_t *r){unsigned i;for(i=0;i<DS_REC;++i)if(r[i]!=0xff)return 0;return 1;}
static int valid(const uint8_t *r)
{
    unsigned i;
    if(crc16(r)!=(uint16_t)(r[6]|r[7]<<8))return 0;
    if(r[0]==TAG_KEY){
        if(r[2]>DS_PER||(r[5]&&!(r[5]&0x80)))return 0;
        for(i=0;i<r[2];++i)if((unsigned)(r[8+i*4]|r[9+i*4]<<8)>=DS_POSITIONS)return 0;
        return 1;
    }
    if(r[0]==TAG_BLOB)return r[2]<=DS_CHUNK&&(unsigned)(r[4]|r[5]<<8)+r[2]<=DS_BLOB_MAX;
    return 0;
}
static int find(const ds_entry *e,unsigned n,unsigned pos)
{
    unsigned i;
    for(i=0;i<n;++i)if(e[i].pos==pos)return (int)i;
    return -1;
}
static void clear_blob(ds_state *st)
{
    unsigned i;
    for(i=0;i<DS_BLOB_MAX;++i)st->blob[i]=0;
    st->blob_len=0;
}
static void apply(ds_state *st,const uint8_t *r)
{
    unsigned i,off,pos;int k;
    if(r[3]&F_SNAP){st->count=0;st->level=DS_NO_LEVEL;st->light=0;clear_blob(st);}
    if(r[0]==TAG_BLOB){
        if(r[3]&F_BLOBRST)clear_blob(st);
        off=(unsigned)(r[4]|r[5]<<8);
        for(i=0;i<r[2];++i)st->blob[off+i]=r[8+i];
        if(off+r[2]>st->blob_len)st->blob_len=off+r[2];
        return;
    }
    if(r[4]!=DS_NO_LEVEL)st->level=r[4];
    if(r[5])st->light=r[5];
    for(i=0;i<r[2];++i){
        pos=(unsigned)(r[8+i*4]|r[9+i*4]<<8);
        k=find(st->entries,st->count,pos);
        if(k<0){k=(int)st->count++;st->entries[k].pos=(uint16_t)pos;}   /* <= 85 by construction */
        st->entries[k].code=(uint16_t)(r[10+i*4]|r[11+i*4]<<8);
    }
}
int ds_load(const ds_backend *b,ds_state *st)
{
    uint8_t r[DS_REC];unsigned s,g=0,ok=0,saves=0;uint32_t mask=0;
    st->count=0;st->level=DS_NO_LEVEL;st->light=0;clear_blob(st);st->next=DS_SLOTS;
    /* Pass 1: find complete saves (first-flag ... record without "more"). */
    for(s=0;s<DS_SLOTS;++s){
        if(!b->read(b->user,(uint16_t)(s*DS_REC),DS_REC,r))return DS_IO;
        if(blank(r)){st->next=s;break;}
        if(!valid(r)){ok=0;continue;}
        if(r[3]&F_FIRST){g=s;ok=1;}
        if(!(r[3]&F_MORE)){
            if(ok){unsigned k;for(k=g;k<=s;++k)mask|=1u<<k;++saves;}
            ok=0;
        }
    }
    /* Pass 2: apply committed saves in slot order. */
    for(s=0;s<DS_SLOTS;++s){
        if(!(mask>>s&1u))continue;
        if(!b->read(b->user,(uint16_t)(s*DS_REC),DS_REC,r))return DS_IO;
        if(valid(r))apply(st,r);
    }
    return (int)saves;
}
/* ceil divisions without a libgcc call (n is small). */
static unsigned cdiv5(unsigned n){return ((n+4u)*52429u)>>18;}
static unsigned cdiv20(unsigned n){return ((n+19u)*52429u)>>20;}
static unsigned kslots(unsigned n,uint8_t level,uint8_t light)
{
    return n?cdiv5(n):((level!=DS_NO_LEVEL||light)?1u:0u);
}
static unsigned bslots(unsigned blen,int write)
{
    return write?(blen?cdiv20(blen):1u):0u;
}
static int put(const ds_backend *b,unsigned slot,uint8_t *r)
{
    uint8_t v[DS_REC];unsigned i,k;
    r[1]=(uint8_t)slot;
    k=crc16(r);r[6]=(uint8_t)k;r[7]=(uint8_t)(k>>8);
    if(!b->program(b->user,(uint16_t)(slot*DS_REC),DS_REC,r))return DS_IO;
    if(!b->read(b->user,(uint16_t)(slot*DS_REC),DS_REC,v))return DS_IO;
    for(i=0;i<DS_REC;++i)if(v[i]!=r[i])return DS_IO;
    return DS_OK;
}
/* One whole save at `slot`: key records for ops, then blob chunks. */
static int emit_save(const ds_backend *b,unsigned slot,unsigned flags0,
                     const ds_entry *ops,unsigned nops,uint8_t level,uint8_t light,
                     const uint8_t *blob,unsigned blen,int write_blob)
{
    unsigned ks=kslots(nops,level,light),total=ks+bslots(blen,write_blob),i,j,n,pos=0;
    uint8_t r[DS_REC];int rc;
    for(i=0;i<total;++i,++slot){
        for(j=0;j<DS_REC;++j)r[j]=0;
        r[3]=(uint8_t)((i?0u:F_FIRST|flags0)|(i+1<total?F_MORE:0u));
        if(i<ks){
            r[0]=TAG_KEY;r[4]=i?DS_NO_LEVEL:level;r[5]=i?0:light;
            for(n=0;n<DS_PER&&pos<nops;++n,++pos){
                r[8+n*4]=(uint8_t)ops[pos].pos;r[9+n*4]=(uint8_t)(ops[pos].pos>>8);
                r[10+n*4]=(uint8_t)ops[pos].code;r[11+n*4]=(uint8_t)(ops[pos].code>>8);
            }
            r[2]=(uint8_t)n;
        }else{
            unsigned c=i-ks,off=c*DS_CHUNK,len=blen>off?blen-off:0;
            if(len>DS_CHUNK)len=DS_CHUNK;
            r[0]=TAG_BLOB;r[2]=(uint8_t)len;r[4]=(uint8_t)off;r[5]=(uint8_t)(off>>8);
            if(c==0)r[3]|=F_BLOBRST;
            for(n=0;n<len;++n)r[8+n]=blob[off+n];
        }
        rc=put(b,slot,r);
        if(rc!=DS_OK)return rc;
    }
    return DS_OK;
}
int ds_save(const ds_backend *b,const ds_entry *want,unsigned want_count,
            uint8_t level,uint8_t light,const uint8_t *blob,unsigned blob_len,
            ds_state *st)
{
    ds_entry ops[DS_MAX_ENTRIES];unsigned nops=0,i,same_blob=1,need;int rc,k;
    uint8_t lv,lt;
    if(want_count>DS_MAX_ENTRIES||blob_len>DS_BLOB_MAX)return DS_FULL;
    rc=ds_load(b,st);
    if(rc<0)return DS_IO;
    /* Differences against the stored state (reverted keys get their factory code). */
    for(i=0;i<want_count;++i){
        k=find(st->entries,st->count,want[i].pos);
        if(k<0||st->entries[k].code!=want[i].code){if(nops<DS_MAX_ENTRIES)ops[nops]=want[i];++nops;}
    }
    for(i=0;i<st->count;++i)
        if(find(want,want_count,st->entries[i].pos)<0){
            uint16_t fc=b->factory(b->user,st->entries[i].pos);
            if(st->entries[i].code==fc)continue;          /* already stored as factory */
            if(nops<DS_MAX_ENTRIES){ops[nops].pos=st->entries[i].pos;ops[nops].code=fc;}
            ++nops;
        }
    if(blob_len!=st->blob_len)same_blob=0;
    else for(i=0;i<blob_len;++i)if(blob[i]!=st->blob[i])same_blob=0;
    lv=level==st->level?DS_NO_LEVEL:level;
    lt=light==st->light?0:light;
    need=kslots(nops,lv,lt)+bslots(blob_len,!same_blob);
    if(!need)return DS_UNCHANGED;
    if(nops<=DS_MAX_ENTRIES&&need<=DS_SLOTS-st->next)
        return emit_save(b,st->next,0,ops,nops,lv,lt,blob,blob_len,!same_blob);
    /* Log full: compact into a snapshot of the whole wanted state. */
    need=kslots(want_count,level,light)+bslots(blob_len,blob_len!=0);
    if(need>DS_SLOTS)return DS_FULL;
    if(!b->erase(b->user))return DS_IO;
    return emit_save(b,0,F_SNAP,want,want_count,level,light,blob,blob_len,blob_len!=0);
}
