#ifndef FLOW_VIA_PROTOCOL_H
#define FLOW_VIA_PROTOCOL_H
#include <stdint.h>
#include <stddef.h>
#define FLOW_VIA_REPORT_SIZE 32u
#define FLOW_VIA_ENTRIES 528u
#define FLOW_VIA_MAX_BATCH 15u
/* Runtime-only engine marker, not a stock OE915 action. Value is raw QMK MT.
 * Adapter must consume this marker and enforce engine capacity/idle guards;
 * never pass kind10 to the stock dispatcher as if it executed tap-hold. */
#define FLOW_VIA_MT_KIND 10u
/* Runtime-only layer-tap marker: raw QMK LT(layer,kc) = 0x4000|layer<<8|kc.
 * Only LT(1,kc) on a Win base entry and LT(3,kc) on a Mac base entry (hold = Fn). */
#define FLOW_VIA_LT_KIND 11u
/* Syntax check only: nonempty modifier nibble plus right flag; ordinary Keyboard-page tap.
 * No allocation, key state changes, or adapter availability checks. */
int flow_via_is_mod_tap(uint16_t code);
/* bank: 0 Win base, 1 Win Fn, 2 Mac base, 3 Mac Fn (VIA layer). */
int flow_via_is_layer_tap(uint16_t code, unsigned bank);
/* Flattened offsets: layer * 132 + row * 22 + column. Wire entries are
 * OE915's four bytes, including checksum and Fn-lock bit. All callbacks run
 * synchronously; serialize with scanner/OS-bank changes in the integration. */
typedef int (*flow_via_read)(void *, uint16_t, int original, uint8_t out[4]);
/* A failed commit MUST leave every entry unchanged. Success commits the entire
 * contiguous batch. Reject inactive OS banks here, or in validate. Never flash. */
typedef int (*flow_via_commit)(void *, uint16_t first, uint8_t count, const uint8_t (*entries)[4]);
/* Optional guard: reject occupied tap-hold slots, keys currently held, etc.
 * Called for every candidate before committing any part of the request. */
typedef int (*flow_via_validate)(void *, uint16_t, const uint8_t entry[4]);
/* Optional atomic RAM-only factory reset. Must preserve runtime reservations;
 * callback is responsible for availability/transaction checks. No callback =>
 * command 06 is rejected. It must not call the stock reset/EEPROM handler. */
typedef int (*flow_via_reset)(void *);
/* VIA macro buffer. M0..M(count-1) are keycodes 0x7700.. and map to OEM macro
 * type-9 values FLOW_VIA_MACRO_BASE.. (OEM ids are 1-based; 1..5 are factory). */
#define FLOW_VIA_MACRO_BASE 6u
#define FLOW_VIA_MACRO_MAX 8u
typedef struct {
    uint8_t count;
    uint16_t size;
    int (*read)(void *, uint16_t off, uint8_t n, uint8_t *out);
    int (*write)(void *, uint16_t off, uint8_t n, const uint8_t *data);
    int (*reset)(void *);
} flow_via_macros;
typedef struct {
    void *user;
    const flow_via_macros *macros;
    flow_via_read read;
    flow_via_commit commit;
    flow_via_validate validate;
    flow_via_reset reset;
    uint32_t started;
    uint8_t indication;
    uint8_t ready;
} flow_via;
/* This codec targets the SHA-pinned OE915 v2.52 ROM tables: ten factory actions
 * have fixed custom codes, matching the definition JSON order. Init rejects
 * unknown opaque factory actions. The enclosing builder authenticates the ROM.
 * Factory reads of all four banks must be available; live reads may reject
 * inactive banks. No heap or libc calls. */
int flow_via_init(flow_via *, void *, flow_via_read, flow_via_commit,
                  flow_via_validate, flow_via_reset, uint32_t now_ms);
/* Exactly 32 bytes excluding HID report ID. Returns 0 for malformed length or
 * uninitialized state (no response); otherwise 1, including command-FF errors.
 * Request and reply may alias. Other overlapping buffers are unsupported. */
int flow_via_get_keycode(flow_via *, uint16_t index, uint16_t *out);
int flow_via_get_factory_keycode(flow_via *, uint16_t index, uint16_t *out);
int flow_via_decode_keycode(flow_via *, uint16_t index, uint16_t code, uint8_t out[4]);
int flow_via_handle(flow_via *, const uint8_t *, size_t, uint8_t reply[32], uint32_t now_ms);
#endif
