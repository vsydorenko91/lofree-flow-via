/* OE915 v2.52 VIA adapter. Requests are queued in the USB ISR and processed
 * ONLY in the normal main loop. Only the explicit Save button writes the dedicated journal. */
#include "protocol.h"
#include "th_runtime.h"
#include "dstore.h"
#include "macro.h"
#include "version.h"
_Static_assert(MACRO_COUNT==FLOW_VIA_MACRO_MAX,"macro count mismatch");
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
#define KEYMAP ((volatile u8 *)(*(volatile u32 *)0x400408u))
#define OPTIONS ((volatile u8 *)(*(volatile u32 *)0x400414u))
#define SETUP ((volatile u8 *)0x400258u)
#define OUT_STATE ((volatile u8 *)0x40109cu)
#define RX_BUF ((volatile u8 *)0x4010e4u)
#define USB ((volatile u8 *)0x804000u)
#define CONFIGURED (*(volatile u8 *)0x4003c4u)
#define USB_SUSPEND (*(volatile u8 *)0x4003b0u)
#define STOCK_ACK ((void (*)(void))0x18249u)
#define STOCK_LOADER ((int (*)(void))0x196cdu)
#define STOCK_EP3_SEND ((void (*)(const u8 *, unsigned))0x12255u)

extern int th_busy(void);
extern volatile u8 th_gate_state;
static u16 current_epoch(void){return *(volatile u16 *)((volatile u8 *)&th_gate_state+2);}
static const u32 rom[4]={0x22cc0,0x22ed0,0x230e0,0x232f0};
static flow_via protocol;
static u8 banks[528][4];
static u8 initialized;
static const ds_entry *cand;static unsigned cand_n;
static u8 saved,dirty,save_failed,saving;
/* Tap-hold timeout presets (processing passes), index 0 = default. Stored in light bits 5..6. */
static const u16 term_table[4]={200,120,320,500};
static u8 term_index;
/* Read-only RAM telemetry: no extra NVM writes, reset on power-up. */
static volatile u8 save_stage,backend_ok;
static volatile u16 save_attempts,erase_calls,program_calls,last_off,last_len;
/* OEM macro table (12-byte records) in RAM: records 0..18 are the factory
 * macros (seeded from ROM by the boot gate), user macros follow. */
#define MT_FACTORY 19u
#define MT_USER 96u
#define MACRO_BUF 192u
u8 macro_table[(MT_FACTORY+MT_USER)*MACRO_RECORD];
static u8 macro_buf[MACRO_BUF];
static u8 macro_dirty,macro_failed;
#define OEM_MACRO_FLAGS (*(volatile u8 *)0x401c04u)
#define OEM_MACRO_LENGTHS ((volatile u8 *)0x401c08u)
static u8 active_os;
static volatile u8 rx_head,rx_tail;
static u8 requests[4][32];
static u16 request_epoch[4];
static u16 seen_epoch,reply_epoch;
static u8 reply[32];
static u8 reply_ready;
u32 via_requests,via_rejected_packets,via_responses;

static unsigned layer_of(unsigned i)
{
    return i<132 ? 0 : i<264 ? 1 : i<396 ? 2 : 3;
}
static unsigned local_of(unsigned i)
{
    if(i>=396)return i-396;
    if(i>=264)return i-264;
    if(i>=132)return i-132;
    return i;
}
static int read_action(void *user,u16 i,int original,u8 out[4])
{
    unsigned n,layer=layer_of(i),slot=local_of(i);
    const volatile u8 *p;
    (void)user;
    if(i>=528)return 0;
    p=original ? (const volatile u8 *)rom[layer]+slot*4 : banks[i];
    for(n=0;n<4;++n)out[n]=p[n];
    return 1;
}
static u16 ds_factory(void *user,unsigned pos)
{
    u16 c;(void)user;
    return flow_via_get_factory_keycode(&protocol,(u16)pos,&c)?c:0;
}
/* Candidate profile = factory + a validated delta list (no 1056-byte image). */
static u16 cand_code(unsigned i)
{
    unsigned k;
    for(k=0;k<cand_n;++k)if(cand[k].pos==i)return cand[k].code;
    return ds_factory(0,i);
}
/* A pair of OS layers may use up to two physical MT positions. A position
 * shared by base/Fn must use the same MT code, so queued taps keep their action. */
static const u8 *future(unsigned i,u16 first,u8 count,const u8 (*a)[4])
{
    static u8 decoded[4];
    if(cand){
        (void)flow_via_decode_keycode(&protocol,i,cand_code(i),decoded);
        return decoded;
    }
    return i>=first && i<first+count ? a[i-first] : banks[i];
}
static int th_plan(unsigned os,u16 first,u8 count,const u8 (*a)[4],
                   struct th_cfg cfg[TH_MAX],u16 codes[TH_MAX])
{
    unsigned layer,slot,k,n=0,kind;volatile unsigned col;const u8 *p;u16 code;
    for(k=0;k<TH_MAX;++k){
        cfg[k].phys=255;cfg[k].tap_slot=19+k*22;cfg[k].hold_slot=20+k*22;
        cfg[k].reserved=0;cfg[k].term_passes=term_table[term_index];
        codes[k]=0;
    }
    for(layer=0;layer<2;++layer)for(slot=0;slot<132;++slot){
        p=future(os*264+layer*132+slot,first,count,a);
        kind=p[0]&127;
        if(kind!=FLOW_VIA_MT_KIND && kind!=FLOW_VIA_LT_KIND)continue;
        code=p[1]|(u16)p[2]<<8;
        col=slot;while(col>=22)col-=22;
        if(col>=15 || !(kind==FLOW_VIA_MT_KIND ? flow_via_is_mod_tap(code) : flow_via_is_layer_tap(code,os*2+layer)))return 0;
        for(k=0;k<n && cfg[k].phys!=slot;++k){}
        if(k<n){if(codes[k]!=code)return 0;}
        else{
            if(n==TH_MAX)return 0;
            cfg[n].phys=slot;cfg[n].reserved=kind==FLOW_VIA_LT_KIND;codes[n++]=code;
        }
    }
    return 1;
}
/* Rebuild OEM records + lengths from the raw buffer. Dry-run first so a bad
 * buffer can never damage the live table; skipped while the OEM player runs. */
static void macro_apply(void)
{
    u8 lengths[MACRO_COUNT];unsigned used,i;
    if(!macro_dirty)return;
    if(OEM_MACRO_FLAGS&2)return;                      /* player busy: retry later */
    macro_dirty=0;
    if(!macro_compile(macro_buf,MACRO_BUF,0,MT_USER,lengths,&used)){macro_failed=1;return;}
    macro_failed=0;
    macro_compile(macro_buf,MACRO_BUF,macro_table+MT_FACTORY*MACRO_RECORD,MT_USER,lengths,&used);
    for(i=0;i<MACRO_COUNT;++i)OEM_MACRO_LENGTHS[FLOW_VIA_MACRO_BASE-1+i]=lengths[i];
}
static int macro_rd(void *user,u16 off,u8 n,u8 *out)
{
    unsigned i;(void)user;
    for(i=0;i<n;++i)out[i]=macro_buf[off+i];
    return 1;
}
static int macro_wr(void *user,u16 off,u8 n,const u8 *in)
{
    unsigned i;(void)user;
    for(i=0;i<n;++i)macro_buf[off+i]=in[i];
    macro_dirty=1;dirty=1;macro_apply();
    return 1;
}
static int macro_rs(void *user)
{
    unsigned i;(void)user;
    for(i=0;i<MACRO_BUF;++i)macro_buf[i]=0;
    macro_dirty=1;dirty=1;macro_apply();
    return 1;
}
static const flow_via_macros macro_ops={MACRO_COUNT,MACRO_BUF,macro_rd,macro_wr,macro_rs};
static void copy_active(void);
static int commit(void *user,u16 first,u8 count,const u8 (*a)[4])
{
    unsigned j,n,i,slot;
    struct th_cfg cfg[TH_MAX];u16 codes[TH_MAX];
    (void)user;
    if(first>=528||count>15||first+count>528||th_busy()||saving)return 0;
    /* Check the complete future profile before publishing any entry. */
    for(j=0;j<count;++j){
        slot=local_of(first+j);
        if(slot==19||slot==20||slot==41||slot==42)
            for(n=0;n<4;++n)if(a[j][n]!=banks[first+j][n])return 0;
    }
    if(!th_plan(0,first,count,a,cfg,codes)||!th_plan(1,first,count,a,cfg,codes))return 0;
    /* main loop owns keymap dispatch and OS switching. IRQ only samples GPIO. */
    for(j=0;j<count;++j){
        i=first+j;
        for(n=0;n<4;++n)banks[i][n]=a[j][n];
    }
    copy_active();dirty=1;
    return 1;
}
static void action(unsigned slot,unsigned kind,unsigned value)
{
    unsigned layer;volatile u8 *p;
    for(layer=0;layer<2;++layer){
        p=KEYMAP+layer*0x210+slot*4;
        p[0]=kind;p[1]=value;p[2]=value>>8;p[3]=0x55-kind-p[1]-p[2];
    }
}
static void copy_active(void)
{
    unsigned i,n,first=active_os?264:0,mods;
    struct th_cfg cfg[TH_MAX];u16 codes[TH_MAX];
    for(i=0;i<264;++i)for(n=0;n<4;++n)KEYMAP[i*4+n]=banks[first+i][n];
    if(!th_plan(active_os,0,0,0,cfg,codes))return;
    for(i=0;i<TH_MAX;++i){
        th_runtime_cfg[i].phys=cfg[i].phys;th_runtime_cfg[i].tap_slot=cfg[i].tap_slot;
        th_runtime_cfg[i].hold_slot=cfg[i].hold_slot;th_runtime_cfg[i].reserved=cfg[i].reserved;
        th_runtime_cfg[i].term_passes=cfg[i].term_passes;   /* no struct copy: it would pull in memcpy */
        if(cfg[i].phys==255)continue;
        action(cfg[i].tap_slot,2,codes[i]&255);
        if(cfg[i].reserved)action(cfg[i].hold_slot,6,2);
        else{
            mods=(codes[i]>>8)&15;
            if(codes[i]&0x1000)mods<<=4;
            action(cfg[i].hold_slot,1,mods);
        }
    }
    th_checked=0;
}
static void load_rom(void)
{
    unsigned i,n;
    for(i=0;i<528;++i)
        for(n=0;n<4;++n)banks[i][n]=*((const volatile u8 *)rom[layer_of(i)]+local_of(i)*4+n);
}
/* VIA command 06 and the menu button: factory keymap in RAM only. Nothing is written
 * to flash until Save; refused while a key is held or a save is running. */
static int factory_reset(void *user)
{
    (void)user;
    if(th_busy()||saving)return 0;
    load_rom();copy_active();dirty=1;
    return 1;
}
/* Called before processing input: an OS transition must publish descriptors
 * before the engine resets held-key state for that transition. */
void via_th_sync(void)
{
    if(initialized && active_os!=(OPTIONS[9]!=0)){
        active_os=OPTIONS[9]!=0;copy_active();
    }
}
/* Backend: selector 1 "sector A" words [0x8000,0x8080), bytes [0,476) only.
 * OEM journal (0x8080..) and the update marker bytes 0x1f0..0x1f7 are never
 * addressed; erase restores any non-FF bytes of the tail 0x1e0..0x1ff. */
static void s1_read(unsigned word,unsigned n,u8 *out)
{((void (*)(unsigned,unsigned,unsigned,u8 *))0x1a76du)(1,word,n,out);}
static int ds_rd(void *user,u16 off,u16 n,u8 *out)
{
    (void)user;
    if(!n || (off&3) || (n&3) || off>DS_SLOTS*DS_REC || n>DS_SLOTS*DS_REC-off)return 0;
    s1_read(0x8000u+(off>>2),n,out);
    return 1;
}
static int ds_er(void *user)
{
    u8 tail[32];unsigned i,keep=0;int ok;
    (void)user;
    if(!saving)return 0;
    s1_read(0x8078u,32,tail);
    for(i=0;i<32;++i)keep|=tail[i]!=0xff;
    save_stage=2;last_off=0;last_len=512;++erase_calls;
    ok=((int (*)(unsigned,unsigned,unsigned))0x19661u)(1,0x8000u,512);
    if(ok && keep)ok=((int (*)(unsigned,unsigned,unsigned,const u8 *))0x1b9a5u)(1,0x8078u,32,tail);
    backend_ok=ok!=0;save_stage=ok?3:4;
    return ok;
}
static int ds_pr(void *user,u16 off,u16 n,const u8 *in)
{
    int ok;
    (void)user;
    if(!saving || !n || n>32 || (off&3) || (n&3) || off>=DS_SLOTS*DS_REC || n>DS_SLOTS*DS_REC-off)return 0;
    save_stage=5;last_off=off;last_len=n;++program_calls;
    ok=((int (*)(unsigned,unsigned,unsigned,const u8 *))0x1b9a5u)(1,0x8000u+(off>>2),n,in);
    backend_ok=ok!=0;save_stage=ok?6:7;
    return ok;
}
static const ds_backend backend={0,ds_rd,ds_er,ds_pr,ds_factory};
#define LEVEL_RAM ((volatile u8 *)0x4018c2u)
static void set_level(unsigned v)
{
    *LEVEL_RAM=v;*(volatile u8 *)0x401218u=v;*(volatile u8 *)0x4018c3u=1;
}
#define LIGHT_ST ((volatile u8 *)0x4018a8u)
/* "Unsaved" = keys/macros edited, or lighting/typing settings differ from the saved (or boot) state. */
static u8 base_level,base_light;
static u8 cur_level(void){return *LEVEL_RAM>4?4:*LEVEL_RAM;}
static u8 cur_light(void){return 0x80|term_index<<5|(LIGHT_ST[0x12]&3)<<3|(LIGHT_ST[0x32]&7);}
static int unsaved(void){return dirty||cur_level()!=base_level||cur_light()!=base_light;}
static void set_color(unsigned v){LIGHT_ST[0x32]=v;*(volatile u8 *)0x401219u=v;*(volatile u8 *)0x4018c3u=1;}
static void set_mode(unsigned v)
{
    /* Same steps as the stock mode key (0x1ada8): mode, effect byte from the stock table
     * (steady 0x30, breathing 0x31, off 0xf0) and the "restart effect" flag at +0x1f. */
    LIGHT_ST[0x12]=v;LIGHT_ST[0x11]=*((const u8 *)0x2296cu+v);LIGHT_ST[0x1f]=1;
    *(volatile u8 *)0x40121au=v;*(volatile u8 *)0x4018c3u=1;
}
static int profile_valid(const ds_entry *list,unsigned count)
{
    unsigned i,n,slot;u8 e[4];int ok=1;
    struct th_cfg cfg[TH_MAX];u16 codes[TH_MAX];
    cand=list;cand_n=count;
    for(i=0;i<528&&ok;++i){
        if(!flow_via_decode_keycode(&protocol,i,cand_code(i),e)){ok=0;break;}
        slot=local_of(i);
        if(slot==19||slot==20||slot==41||slot==42)
            for(n=0;n<4;++n)if(e[n]!=*((const volatile u8 *)rom[layer_of(i)]+slot*4+n))ok=0;
    }
    if(ok)ok=th_plan(0,0,0,0,cfg,codes)&&th_plan(1,0,0,0,cfg,codes);
    cand=0;
    return ok;
}
static void apply_list(const ds_entry *list,unsigned count)
{
    unsigned k,n;u8 e[4];
    for(k=0;k<count;++k){
        (void)flow_via_decode_keycode(&protocol,list[k].pos,list[k].code,e);
        for(n=0;n<4;++n)banks[list[k].pos][n]=e[n];
    }
    copy_active();
}
static int save_profile(void)
{
    ds_entry want[DS_MAX_ENTRIES];ds_state st;
    unsigned n=0,blen;u16 i,code;int result;u8 level,light;
    if(saving || th_busy())return 0;
    ++save_attempts;save_stage=1;
    for(i=0;i<528;++i){
        if(!flow_via_get_keycode(&protocol,i,&code))return 0;
        if(code==ds_factory(0,i))continue;
        if(n==DS_MAX_ENTRIES){save_failed=1;save_stage=9;return 0;}   /* too many changed keys */
        want[n].pos=i;want[n++].code=code;
    }
    level=cur_level();light=cur_light();
    for(blen=MACRO_BUF;blen&&!macro_buf[blen-1];--blen){}
    saving=1;
    result=ds_save(&backend,want,n,level,light,macro_buf,blen,&st);
    saving=0;save_failed=result<1;
    if(!save_failed){saved=1;dirty=0;base_level=level;base_light=light;save_stage=8;}
    else if(save_stage!=4 && save_stage!=7)save_stage=9;
    return !save_failed;
}

/* Explicit button: 07 channel0 value1 payload1. Generic09 (sent by VIA
 * automatically after custom changes) acknowledges but NEVER saves. */
static int profile_command(const u8 q[32],u8 r[32])
{
    unsigned i;const char *status;
    for(i=0;i<32;++i)r[i]=q[i];
    if(q[1]!=0){r[0]=0xff;return 1;}
    if(q[0]==9)return 1;
    if(q[2]==6){
        /* White key backlight level 0..4, RAM only (no flash). OEM LED loop
         * 0x1ea34 reads 0x4018c2 every refresh; 0x401218 mirrors the
         * level for the OEM settings journal, so a later Fn press stays
         * consistent. The dirty flag 0x4018c3 has no readers, set as OEM does. */
        volatile u8 *const level=LEVEL_RAM;
        if(q[0]==7){
            if(q[3]>4){r[0]=0xff;return 1;}
            set_level(q[3]);
        }else r[3]=*level>4?4:*level;
        return 1;
    }
    if(q[2]==10 || q[2]==11){
        /* Side lighting, RAM only. Settings struct 0x4018a8: color index +0x32 (0..7:
         * red, green, yellow, turquoise, cyan, blue, purple, white), mode +0x12 (0 steady,
         * 1 breathing, 2 off). */
        unsigned c=q[2]==10;
        if(q[0]==7){
            if(q[3]>(c?7u:2u)){r[0]=0xff;return 1;}
            if(c)set_color(q[3]);else set_mode(q[3]);
        }else r[3]=c?LIGHT_ST[0x32]:(LIGHT_ST[0x12]>2?2:LIGHT_ST[0x12]);
        return 1;
    }
    if(q[2]==15){if(q[0]==8)r[3]=FLOW_VERSION_MINOR;return 1;}   /* firmware version (read-only) */
    if(q[2]==14){
        /* Tap-hold timeout preset 0..3 (applies to the active and future plans). */
        if(q[0]==7){
            if(q[3]>3){r[0]=0xff;return 1;}
            term_index=q[3];
            for(i=0;i<TH_MAX;++i)th_runtime_cfg[i].term_passes=term_table[term_index];
        }else r[3]=term_index;
        return 1;
    }
    if(q[0]==8 && q[2]==12){
        /* Read-only: raw 28-byte journal record `q[3]` (proof of what is stored). */
        if(q[3]>=DS_SLOTS || !ds_rd(0,(u16)(q[3]*DS_REC),DS_REC,r+3)){r[0]=0xff;}
        return 1;
    }
    if(q[2]==16){
        /* Button: factory keymap and empty macros, RAM only (Save makes it permanent). */
        if(q[0]==7 && (q[3]!=1 || !factory_reset(0) || !macro_rs(0)))r[0]=0xff;
        return 1;
    }
    if(q[2]==13){if(q[0]==8)r[3]=unsaved();return 1;}   /* VIA toggle: unsaved changes; a click is acknowledged, no effect */
    if(q[0]==7){
        if(q[2]!=1 || q[3]!=1 || !save_profile())r[0]=0xff;
        return 1;
    }
    if(q[2]==1){r[3]=0;return 1;}
    if(q[2]==3){
        /* ABI1: stage, attempts, erases, programs, last byte offset/length,
         * backend success and saving flag. Integers are big endian. */
        const u16 fields[5]={save_attempts,erase_calls,program_calls,last_off,last_len};
        for(i=3;i<32;++i)r[i]=0;
        r[3]=1;r[4]=save_stage;
        for(i=0;i<5;++i){r[5+i*2]=fields[i]>>8;r[6+i*2]=fields[i];}
        r[15]=backend_ok;r[16]=saving;r[17]=th_passes>>8;r[18]=th_passes;
        return 1;
    }
    if(q[2]!=2){r[0]=0xff;return 1;}
    status=save_failed?"Save failed":unsaved()?"Unsaved changes":saved?"Saved":"No saved profile";
    for(i=0;i<28 && status[i];++i)r[3+i]=status[i];
    r[3+i]=0;return 1;
}
static int initialize(void)
{
    if(initialized)return 1;
    if(th_busy())return 0;
    load_rom();
    active_os=OPTIONS[9]!=0;
    if(!flow_via_init(&protocol,0,read_action,commit,0,factory_reset,0))return 0;
    protocol.macros=&macro_ops;macro_dirty=1;macro_apply();
    {
        ds_state st;unsigned k;
        if(ds_load(&backend,&st)>0 && profile_valid(st.entries,st.count)){
            if(st.light&0x80)term_index=st.light>>5&3;
            apply_list(st.entries,st.count);saved=1;
            if(st.level<=4)set_level(st.level);
            if(st.light&0x80){set_color(st.light&7);if((st.light>>3&3)<=2)set_mode(st.light>>3&3);}
            for(k=0;k<MACRO_BUF;++k)macro_buf[k]=k<st.blob_len?st.blob[k]:0;
            macro_dirty=1;macro_apply();
        }else copy_active();
    }
    base_level=cur_level();base_light=cur_light();
    initialized=1;
    return 1;
}

/* Hook at 0x17fa8, after the original EP0 FIFO read, in the USB ISR.
 * Keep stock ACK and following LED/vendor dispatch unchanged. Only the exact
 * unnumbered output request for IF2 is consumed as VIA. Never forward bytes. */
void via_ep0_complete(void)
{
    unsigned i,next;
    if(SETUP[0]==0x21&&SETUP[1]==9&&SETUP[2]==0&&SETUP[3]==2&&
       SETUP[4]==2&&SETUP[5]==0&&SETUP[6]==32&&SETUP[7]==0&&
       OUT_STATE[6]==0&&OUT_STATE[7]==2&&OUT_STATE[4]==32&&OUT_STATE[5]==0&&
       OUT_STATE[1]==32&&OUT_STATE[2]==0){
        next=(rx_head+1u)&3u;
        if(next!=rx_tail){
            for(i=0;i<32;++i)requests[rx_head][i]=RX_BUF[i];
            request_epoch[rx_head]=current_epoch();
            rx_head=next;
        }else ++via_rejected_packets;
    }
    STOCK_ACK();
}


/* Replaces only the USB mode's stock mouse sender. EP3 becomes Raw HID. */
void via_poll(void)
{
    unsigned tail;
    u16 epoch=current_epoch();
    u8 old_endpoint,busy;
    if(!CONFIGURED||USB_SUSPEND||th_gate_state!=1)return;
    if(!initialize())return;
    if(epoch!=seen_epoch || (OPTIONS[9]!=0)!=active_os){
        active_os=OPTIONS[9]!=0;
        copy_active();
        seen_epoch=epoch;
    }
    macro_apply();
    if(reply_ready&&reply_epoch!=epoch)reply_ready=0;
    while(!reply_ready&&rx_tail!=rx_head){
        tail=rx_tail;
        if(request_epoch[tail]==epoch){
            reply_epoch=epoch;
            if(requests[tail][0]>=7 && requests[tail][0]<=9)reply_ready=profile_command(requests[tail],reply);
            else if(flow_via_handle(&protocol,requests[tail],32,reply,0))reply_ready=1;
            ++via_requests;
        }
        rx_tail=(tail+1u)&3u;
    }
    if(!reply_ready)return;
    if(current_epoch()!=epoch){reply_ready=0;return;}
    old_endpoint=USB[0xe];USB[0xe]=3;
    busy=USB[0x11]&1u;
    USB[0xe]=old_endpoint;
    if(busy)return;
    /* One reply per host request. No unsolicited packets compete for EP3. */
    STOCK_EP3_SEND(reply,32);
    reply_ready=0;
    ++via_responses;
}

/* Called for an OS bank reload after init. Before the gate is active a tiny
 * original-padding gate calls STOCK_LOADER directly, avoiding this code. */
int via_load_current(void)
{
    if(!initialized)return STOCK_LOADER();
    active_os=OPTIONS[9]!=0;
    copy_active();
    return 1;
}
