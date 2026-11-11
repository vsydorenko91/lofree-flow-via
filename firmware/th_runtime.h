#ifndef FLOW_TH_RUNTIME_H
#define FLOW_TH_RUNTIME_H
/* Two stock functions lost their only callers to this patch (tools/build.py checks it):
 * the Win/Mac keymap loader at 0x19710 and the mouse report sender at 0x1f99c. Their
 * bytes hold our code instead. Use only for code that runs after the gate is active. */
#define IN_HOLE_A __attribute__((section(".hole_a"),noinline))
#define IN_HOLE_B __attribute__((section(".hole_b"),noinline))
#define TH_MAX 4
struct th_cfg {
    unsigned char phys, tap_slot, hold_slot, reserved;   /* reserved: 1 = layer-tap, 0 = mod-tap */
    unsigned short term_passes;
};
extern struct th_cfg th_runtime_cfg[TH_MAX];
/* When does a tap-hold key become "hold" (apart from the timeout)?
 * 0: as soon as another key is pressed; 1: when another key is pressed and released while it
 * is down; 2: never, only the timeout. Change it only while th_busy() is 0. */
enum { TH_HOLD_ON_PRESS, TH_HOLD_ON_TAP, TH_HOLD_ON_TIMEOUT, TH_FLAVORS };
extern unsigned char th_flavor;
extern unsigned char th_checked;
extern unsigned short th_passes;
/* The adapter publishes validated descriptors and virtual actions only at idle. */
void via_th_sync(void);
#endif
