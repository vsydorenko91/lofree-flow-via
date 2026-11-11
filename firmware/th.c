/* OE915 v2.52 tap-hold engine (mod-tap and layer-tap). See docs/TAP_HOLD.md.
 *
 * Wrap every matrix_process call, redirect its matrix literal to th_shadow,
 * its activity call to th_activity, its key_event call to th_key_event, and
 * the keyboard sender's FIFO call to th_usb_submit. The stock macro tick stays.
 * Physical GPIO memory is never changed: IRQ scanning can continue normally.
 * Time values count processing passes, NOT milliseconds or GPIO row scans.
 */
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;

#include "th_runtime.h"
#define NKEYS 132
#define ROWS 6
#define COLS 22
#define MATRIX ((volatile u32 *)0x00401aecu)
#define REPORT ((volatile u8 *)0x00401a78u)
#define SNAPSHOT ((volatile u8 *)0x00401b7cu)
#define REPORT_FLAGS ((volatile u8 *)0x0040039cu)
#define DIRTY ((volatile u8 *)0x004003f0u)
#define KEYMAP ((volatile u8 *)(*(volatile u32 *)0x00400408u))
#define OPTIONS ((volatile u8 *)(*(volatile u32 *)0x00400414u))
#define MACRO_FLAGS (*(volatile u8 *)0x00401c04u)
#define STOCK_EVENT ((int (*)(unsigned, int))0x00019899u)
#define STOCK_MATRIX ((int (*)(void))0x0001beb5u)
#define STOCK_USB ((void (*)(const u8 *, unsigned))0x000121edu)

struct th_cfg th_runtime_cfg[TH_MAX];
#define CFG th_runtime_cfg

enum { IDLE, PENDING, HOLD };
enum { NO_TAP, DOWN, DOWN_SENT, UP };
enum { BAD_CONFIG = 1, QUEUE_FULL = 2, MACRO_WAIT = 4 };
struct th_key {
    u16 elapsed, queued;
    u8 decision, was_down, phase, tap_visible, bound;
    u8 mark;   /* th_defer_n when the key went down: later entries were pressed while it is held */
};

u32 th_shadow[ROWS];
u32 th_previous[ROWS];
u8 th_keys[NKEYS]; /* bit 0: held; bit 1: pinned selected Fn layer */
struct th_key th_state[TH_MAX];
u8 th_faults;
u8 th_enabled;
u8 th_context_seen, th_context, th_macro_was_active;
u8 th_checked, th_valid;
u16 th_passes;   /* matrix passes since boot (timing telemetry) */
u8 th_flavor;
/* Flavors 1 and 2 cannot decide at the first other key press, so keys pressed while a tap-hold
 * key is undecided (or while its tap is still on its way to the host) are held back and shown
 * to the stock scanner afterwards, one at a time and in the order they were pressed. An entry
 * whose key was released before its turn is still shown as a full press and release.
 * The queue holds 16 presses; with a timeout of at most 500 passes that is out of reach of
 * ten fingers. Beyond it a key is no longer held back, and a repeated key can lose a press. */
#define DEFER_MAX 16
#define DEFER_PASSES 8          /* give up waiting for a keyboard report after this many passes */
static u8 th_defer[DEFER_MAX];  /* matrix indices, oldest first */
static u8 th_defer_n;
static u16 th_defer_up;         /* bit j = entry j is physically released */
static u8 th_head_shown, th_head_wait;
static volatile u8 th_submit_seen;

static unsigned usb_mode(void)
{
    /* main's mode pointer is a .data literal, NOT the Win/Mac options pointer. */
    return *(volatile u8 *)(*(volatile u32 *)0x0001dc60u) == 1;
}

static unsigned column(unsigned idx)
{
    volatile unsigned n = idx;
    while (n >= COLS) n -= COLS;
    return n;
}

static unsigned row_index(unsigned idx)
{
    unsigned row = 0;
    while (idx >= COLS) { idx -= COLS; ++row; }
    return row;
}

static int bit(const u32 *matrix, unsigned idx)
{
    return (matrix[row_index(idx)] >> (column(idx))) & 1u;
}

static void setbit(u32 *matrix, unsigned idx, int value)
{
    u32 mask = 1u << (column(idx));
    if (value) matrix[row_index(idx)] |= mask;
    else matrix[row_index(idx)] &= ~mask;
}

static unsigned type_at(unsigned idx, unsigned layer)
{
    return KEYMAP[idx * 4 + layer * 0x210] & 0x7f;
}

static unsigned value_at(unsigned idx, unsigned layer)
{
    volatile u8 *p = KEYMAP + idx * 4 + layer * 0x210;
    return p[1] | (unsigned)p[2] << 8;
}

static unsigned inverted(unsigned idx)
{
    return ((KEYMAP[idx * 4] | KEYMAP[idx * 4 + 0x210]) & 0x80) && OPTIONS[0xb];
}

/* Runtime-only markers: kind 10 = mod-tap, kind 11 = layer-tap (hold = Fn). */
static unsigned bound_kind(unsigned t){return t == 10 || t == 11;}
IN_HOLE_B static unsigned physical_fn(const u32 *physical)
{
    unsigned i;
    for(i=0;i<NKEYS;++i)
        if(type_at(i,0)==6 && bit(physical,i))return 1;
    return 0;
}

static unsigned usage(unsigned idx, unsigned layer)
{
    unsigned type = type_at(idx, layer), value = value_at(idx, layer);
    return type == 2 ? value : (type == 7 ? value >> 8 : 0);
}

static int owner(unsigned idx, unsigned key)
{
    unsigned i;
    for (i = 0; i < NKEYS; ++i)
        if (i != idx && (th_keys[i] & 1) && usage(i, th_keys[i] >> 1) == key)
            return 1;
    return 0;
}

int th_config_valid(void)
{
    unsigned k, j, layer, which, slot, type, value;
    u8 used[NKEYS];
    for (j = 0; j < NKEYS; ++j) used[j] = 0;
    for (k = 0; k < TH_MAX; ++k) {
        if (CFG[k].phys == 0xff) continue;
        if (CFG[k].phys >= NKEYS || column(CFG[k].phys) >= 15 ||
            CFG[k].reserved > 1 || !CFG[k].term_passes) return 0;
        for (j = 0; j < k; ++j)
            if (CFG[j].phys == CFG[k].phys) return 0;
        for (which = 0; which < 2; ++which) {
            slot = which ? CFG[k].hold_slot : CFG[k].tap_slot;
            if (slot >= NKEYS || column(slot) < 15 || used[slot]) return 0;
            used[slot] = 1;
            type = type_at(slot, 0); value = value_at(slot, 0);
            if (!which && (type != 2 || !value || value > 0xa4)) return 0;
            if (which && !((type == 1 && value && value <= 255 && !CFG[k].reserved) ||
                           (type == 6 && value == 2 && CFG[k].reserved))) return 0;
            for (layer = 0; layer < 2; ++layer)
                if (type_at(slot, layer) != type || value_at(slot, layer) != value ||
                    (KEYMAP[slot * 4 + layer * 0x210] & 0x80)) return 0;
        }
    }
    return 1;
}

/* Replacement for matrix_process's activity query. */
int th_activity(void)
{
    unsigned row;
    u32 any = 0;
    for (row = 0; row < ROWS; ++row) any |= th_shadow[row];
    return any != 0;
}

/* Called only by stock matrix_process; never call this recursively. */
int th_key_event(unsigned idx, int pressed)
{
    unsigned fn, layer, type, key;
    int result;
    if (!th_enabled || idx >= NKEYS) return STOCK_EVENT(idx, pressed);
    fn = REPORT[0] != 0;
    layer = fn ^ inverted(idx);
    if (!pressed && (th_keys[idx] & 1)) layer = th_keys[idx] >> 1;
    type = type_at(idx, layer);
    key = usage(idx, layer);
    /* Fn is already in the prepass. Avoid stock act_fn's destructive clear.
     * Held ordinary keys retain their chosen action until release instead. */
    if (!pressed) th_keys[idx] = 0;
    if (type == 6 || (key && owner(idx, key))) result = 1;
    else {
        REPORT[0] = layer ^ inverted(idx);
        result = STOCK_EVENT(idx, pressed);
        REPORT[0] = fn;
    }
    if (pressed && result) th_keys[idx] = 1 | (layer << 1);
    return result;
}

static void repair_modifiers(void)
{
    unsigned idx, mods = 0, type, opts;
    /* The stock macro player owns report state while active. */
    if (MACRO_FLAGS & 2) return;
    for (idx = 0; idx < NKEYS; ++idx) {
        if (!(th_keys[idx] & 1)) continue;
        type = type_at(idx, th_keys[idx] >> 1);
        if (type == 1 || type == 7) mods |= value_at(idx, th_keys[idx] >> 1) & 255;
    }
    opts = OPTIONS[6];
    if (opts & 3) mods &= 0xbb;
    if (opts & 4) mods &= 0xdd;
    if (opts & 0x18) mods &= 0x77;
    if (REPORT[0x1e] != mods) {
        REPORT[0x1e] = mods;
        REPORT_FLAGS[0] = REPORT_FLAGS[1] = DIRTY[0] = 1;
    }
    /* matrix_process already packed the snapshot; fix its modifier and sum. */
    SNAPSHOT[0] = REPORT[0x1e];
    mods = 0;
    for (idx = 0; idx < 30; ++idx) mods += SNAPSHOT[idx];
    SNAPSHOT[30] = 0x55 - mods;
}

static void replay_held_keys(void)
{
    unsigned i;
    for (i = 0; i < NKEYS; ++i) th_keys[i] = 0;
    for (i = 0; i < ROWS; ++i) ((volatile u32 *)0x401b1cu)[i] = 0;
    for (i = 0x1d; i < 0x3c; ++i) REPORT[i] = 0;
    REPORT_FLAGS[0] = REPORT_FLAGS[1] = 1;
    for (i = 0; i < 4; ++i) DIRTY[i] = 1;
}

static void context_changed(const u32 *physical)
{
    unsigned k, row;
    replay_held_keys();
    th_defer_n = th_defer_up = th_head_shown = 0;
    th_checked = 0;
    for (row = 0; row < ROWS; ++row) th_previous[row] = physical[row];
    for (k = 0; k < TH_MAX; ++k) {
        struct th_key *st = th_state + k;
        st->elapsed = st->queued = 0;
        st->decision = IDLE;
        st->phase = NO_TAP;
        st->tap_visible = 0;
        st->mark = 0;
        /* A key spanning a transport/OS switch must be released before reuse. */
        st->was_down = CFG[k].phys < NKEYS ? bit(physical, CFG[k].phys) : 0;
        st->bound = st->was_down && bound_kind(type_at(CFG[k].phys, physical_fn(physical) ^ inverted(CFG[k].phys)));
    }
}

extern volatile u8 th_gate_state;
static u16 th_seen_epoch;
static u8 th_epoch_seen;

int th_matrix_process(void)
{
    unsigned row, k, other, is_down, context, stable, j, another_tap, tapped, gate;
    u8 released[TH_MAX];
    u16 age[TH_MAX];
    u32 physical[ROWS], changed[ROWS], pressed[ROWS];
    int result;
    unsigned fn;
    via_th_sync();
    ++th_passes;
    for (row = 0; row < ROWS; ++row) physical[row] = th_shadow[row] = MATRIX[row];
    fn = physical_fn(physical);
    {
        u16 epoch = *(volatile u16 *)((volatile u8 *)&th_gate_state + 2);
        if (th_epoch_seen && epoch != th_seen_epoch) context_changed(physical);
        th_seen_epoch = epoch;
        th_epoch_seen = 1;
    }
    context = (*(volatile u8 *)(*(volatile u32 *)0x1dc60u) << 1) |
              (OPTIONS[9] != 0);
    if (th_context_seen && context != th_context) context_changed(physical);
    th_context_seen = 1;
    th_context = context;
    if (th_macro_was_active && !(MACRO_FLAGS & 2)) replay_held_keys();
    th_macro_was_active = (MACRO_FLAGS & 2) != 0;
    if (!th_checked) { th_valid = th_config_valid(); th_checked = 1; }
    th_enabled = usb_mode() && th_valid;
    if (!th_enabled) {
        if (usb_mode()) th_faults |= BAD_CONFIG;
        /* Fail closed to the stock input path. No NV or device writes here. */
        return STOCK_MATRIX();
    }
    for (row = 0; row < ROWS; ++row) {
        changed[row] = physical[row] ^ th_previous[row];
        pressed[row] = physical[row] & ~th_previous[row];
    }
    for (k = 0; k < TH_MAX; ++k)
        if (CFG[k].phys != 0xff) setbit(changed, CFG[k].phys, 0);
    stable = 1;
    for (row = 0; row < ROWS; ++row) if (changed[row]) stable = 0;
    /* Held-back keys that were let go in this pass; `tapped` = 1 + index of the newest one. */
    tapped = 0;
    for (j = 0; j < th_defer_n; ++j)
        if (!((th_defer_up >> j) & 1u) && !bit(physical, th_defer[j])) {
            th_defer_up |= (u16)(1u << j);
            if (j || !th_head_shown) tapped = j + 1;
        }
    for (k = 0; k < TH_MAX; ++k) {
        released[k] = CFG[k].phys != 0xff && th_state[k].bound && th_state[k].was_down &&
                      !bit(physical, CFG[k].phys);
        age[k] = th_state[k].elapsed;
    }
    for (k = 0; k < TH_MAX; ++k) {
        const struct th_cfg *cfg = CFG + k;
        struct th_key *st = th_state + k;
        if (cfg->phys == 0xff) continue;
        is_down = bit(physical, cfg->phys);
        other = 0;
        for (row = 0; row < ROWS; ++row) {
            u32 fresh = pressed[row];
            if (row == row_index(cfg->phys)) fresh &= ~(1u << column(cfg->phys));
            if (!fresh) continue;
            /* A layer-tap is not decided by a modifier press: Ctrl+Space (Space = LT)
             * must stay a tap-with-modifier, not turn into Fn+Ctrl. */
            if (cfg->reserved) {
                unsigned c;
                for (c = 0; c < COLS; ++c) {
                    unsigned idx = row * COLS + c;
                    if ((fresh >> c) & 1u && idx < NKEYS &&
                        type_at(idx, fn ^ inverted(idx)) != 1) other = 1;
                }
                continue;
            }
            other = 1;
        }
        if (is_down && !st->was_down)
            st->bound = bound_kind(type_at(cfg->phys, fn ^ inverted(cfg->phys)));
        if (!st->bound) { st->was_down = is_down; goto overlay; }
        if (is_down && !st->was_down) {
            st->decision = PENDING;
            st->elapsed = 0;
            st->mark = th_defer_n;
        }
        if (st->decision == PENDING && is_down) {
            if (th_flavor == TH_HOLD_ON_TAP) {
                /* Another key, or a tap-hold key pressed after this one, went down and up again. */
                other = tapped > st->mark;
                for (j = 0; j < TH_MAX; ++j)
                    if (j != k && released[j] && age[j] < st->elapsed) other = 1;
            } else if (th_flavor == TH_HOLD_ON_TIMEOUT) other = 0;
            if (st->elapsed < 65535) ++st->elapsed;
            if (other || st->elapsed >= cfg->term_passes) st->decision = HOLD;
        }
        if (!is_down && st->was_down) {
            if (st->decision == PENDING) {
                if (st->queued == 65535) {
                    th_faults |= QUEUE_FULL;
                    /* Retain the release for retry after a queued tap drains. */
                    goto overlay;
                }
                ++st->queued;
            }
            st->decision = IDLE;
        }
        st->was_down = is_down;
    overlay:
        if (st->bound)
        setbit(th_shadow, cfg->phys, 0);
        setbit(th_shadow, cfg->hold_slot, st->decision == HOLD && is_down);
        if (MACRO_FLAGS & 2) {
            th_faults |= MACRO_WAIT;
            /* Keep queued taps until the player gives report ownership back. */
            setbit(th_shadow, cfg->tap_slot, 0);
            st->tap_visible = 0;
            continue;
        }
        another_tap = 0;
        for (j = 0; j < TH_MAX; ++j)
            if (j != k && th_state[j].phase != NO_TAP) another_tap = 1;
        /* Serialize tap pulses and reserve a boot-keyboard slot. Otherwise a
         * tap inserted before later-row releases may land only in NKRO and
         * never be observed by our endpoint-1 submit hook. Wait for a stable
         * ordinary matrix and fewer than five existing keys instead. */
        /* ...and after every key that was pressed before this one and is still held back. */
        if (st->phase == NO_TAP && st->queued && stable && !another_tap && !st->mark &&
            REPORT[0x1d] < 5 && !owner(cfg->tap_slot, usage(cfg->tap_slot, 0))) {
            st->phase = DOWN;
        }
        if (st->phase == DOWN_SENT) st->phase = UP;
        st->tap_visible = st->phase == DOWN || st->phase == DOWN_SENT;
        setbit(th_shadow, cfg->tap_slot, st->tap_visible);
    }
    if (th_flavor != TH_HOLD_ON_PRESS) {
        /* The oldest held-back key waits for every tap-hold key pressed before it (mark 0)
         * that is still undecided or whose tap has not gone out yet. */
        gate = 0;
        for (k = 0; k < TH_MAX; ++k)
            if (CFG[k].phys != 0xff && !th_state[k].mark &&
                (th_state[k].decision == PENDING || th_state[k].queued ||
                 th_state[k].phase != NO_TAP)) gate = 1;
        if (gate || th_defer_n)
            for (j = 0; j < NKEYS && th_defer_n < DEFER_MAX; ++j) {
                if (!bit(pressed, j)) continue;
                for (k = 0; k < TH_MAX; ++k)
                    if (CFG[k].phys == j && th_state[k].bound) break;
                if (k == TH_MAX) th_defer[th_defer_n++] = (u8)j;
            }
        if (th_head_shown) {
            /* The oldest entry is visible; drop it once its press went out (it then follows the
             * physical key again, or is released if the key is already up). */
            if (th_submit_seen || ++th_head_wait >= DEFER_PASSES) {
                for (j = 1; j < th_defer_n; ++j) th_defer[j - 1] = th_defer[j];
                --th_defer_n;
                th_defer_up >>= 1;
                th_head_shown = 0;
                for (k = 0; k < TH_MAX; ++k) if (th_state[k].mark) --th_state[k].mark;
            }
        } else if (th_defer_n && !gate) {
            th_head_shown = 1;
            th_head_wait = 0;
            th_submit_seen = 0;
        }
        for (j = 0; j < th_defer_n; ++j) setbit(th_shadow, th_defer[j], 0);
        if (th_head_shown) setbit(th_shadow, th_defer[0], 1);
    }
    for (row = 0; row < ROWS; ++row) th_previous[row] = physical[row];
    result = STOCK_MATRIX(); /* includes the UNMODIFIED stock macro_player_tick */
    repair_modifiers();
    /* No physical matrix writes, and no synthetic Fn state is left in it. */
    return result;
}

/* Hook the actual keyboard FIFO submit, NOT the staging/dirty-bit update.
 * This acknowledges submission to the controller, not a USB host ACK.
 * Stock endpoint readiness continues to serialize down and up submissions. */
void th_usb_submit(const u8 *report, unsigned length)
{
    unsigned k, i, found, key;
    STOCK_USB(report, length);
    if (length == 8) th_submit_seen = 1;
    if (!th_enabled || length != 8 || (MACRO_FLAGS & 2)) return;
    for (k = 0; k < TH_MAX; ++k) {
        struct th_key *st = th_state + k;
        if (CFG[k].phys == 0xff) continue;
        key = usage(CFG[k].tap_slot, 0);
        found = 0;
        for (i = 2; i < 7; ++i) if (report[i] == key) found = 1;
        if (st->phase == DOWN && st->tap_visible && found) st->phase = DOWN_SENT;
        else if (st->phase == UP && !st->tap_visible && !found) {
            if (st->queued) --st->queued;
            st->phase = NO_TAP;
        }
    }
}

/* RAM remaps require both physical and synthetic input to be idle. */
IN_HOLE_A int th_busy(void)
{
    unsigned i;
    for (i = 0; i < ROWS; ++i) if (MATRIX[i]) return 1;
    for (i = 0; i < NKEYS; ++i) if (th_keys[i] & 1) return 1;
    if (th_defer_n) return 1;
    for (i = 0; i < TH_MAX; ++i)
        if (th_state[i].was_down || th_state[i].decision ||
            th_state[i].queued || th_state[i].phase) return 1;
    return 0;
}
