#ifndef FLOW_TH_RUNTIME_H
#define FLOW_TH_RUNTIME_H
#define TH_MAX 2
struct th_cfg {
    unsigned char phys, tap_slot, hold_slot, reserved;   /* reserved: 1 = layer-tap, 0 = mod-tap */
    unsigned short term_passes;
};
extern struct th_cfg th_runtime_cfg[TH_MAX];
extern unsigned char th_checked;
extern unsigned short th_passes;
/* The adapter publishes validated descriptors and virtual actions only at idle. */
void via_th_sync(void);
#endif
