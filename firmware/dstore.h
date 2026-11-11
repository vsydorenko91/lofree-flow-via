#ifndef FLOW_DSTORE_H
#define FLOW_DSTORE_H
#include <stdint.h>
/* Compact append-only delta store for selector-1 "sector A" (hardware proven:
 * OEM erase/program/read wrappers, 28-byte programs, OEM journal and update
 * marker untouched). Area = 17 records x 28 bytes at byte offsets 0..475; the
 * update marker bytes 0x1f0..0x1f7 are never addressed.
 *
 * It stores DIFFERENCES from the factory profile as a list of {position, VIA
 * keycode}, plus two settings bytes and one opaque blob (the macro buffer).
 * No 1056-byte profile image is ever needed in RAM.
 *
 * Record (28 B), crc16 LE @6 over bytes 0..5 and 8..27:
 *   key record  tag 0xC7: seq, n(0..5), flags, level (0xFF none), light
 *               (0 none, else 0x80|mode<<3|color), then n x {u16 pos, u16 code}.
 *   blob record tag 0xC9: seq, n(0..20), flags, offset u16 LE @4, data @8
 *               (records written by 0.2.0 carry at most 16 bytes and load unchanged).
 * flags: bit0 snapshot (reset everything), bit1 "more records follow in this
 * save", bit2 "first record of a save", bit3 "blob reset" (clear the blob).
 * A save is applied only if all its records are valid and the last has bit1
 * clear, so a power cut can never leave a half-applied state. An all-FF slot
 * ends the log; a bad-CRC slot (torn write) voids its save. */
#define DS_POSITIONS 528u
#define DS_SLOTS 17u
#define DS_REC 28u
#define DS_PER 5u
#define DS_CHUNK 20u
#define DS_MAX_ENTRIES (DS_SLOTS*DS_PER)
#define DS_BLOB_MAX 192u
#define DS_NO_LEVEL 0xffu
typedef struct { uint16_t pos, code; } ds_entry;
typedef struct {
    void *user;
    int (*read)(void *, uint16_t off, uint16_t n, uint8_t *out);
    int (*erase)(void *);                       /* whole area */
    int (*program)(void *, uint16_t off, uint16_t n, const uint8_t *data);
    uint16_t (*factory)(void *, unsigned pos);  /* factory VIA keycode */
} ds_backend;
typedef struct {
    ds_entry entries[DS_MAX_ENTRIES];
    unsigned count;                             /* entries differing from factory */
    uint8_t level, light;
    uint8_t blob[DS_BLOB_MAX];
    unsigned blob_len;
    unsigned next;                              /* first free slot */
} ds_state;
enum { DS_IO = -1, DS_FULL = -2, DS_OK = 1, DS_UNCHANGED = 2 };
/* Replay the log into *st. Returns the number of valid saves (0 = nothing
 * stored) or DS_IO. */
int ds_load(const ds_backend *, ds_state *st);
/* Persist `want` (full current difference list from factory, unique
 * positions) + settings + blob. scratch receives the stored state. Appends
 * only what changed; compacts (erase + snapshot) when the log is full and
 * refuses BEFORE erasing when the snapshot cannot fit (DS_FULL). Identical
 * state => DS_UNCHANGED with no flash access. */
int ds_save(const ds_backend *, const ds_entry *want, unsigned want_count,
            uint8_t level, uint8_t light, const uint8_t *blob, unsigned blob_len,
            ds_state *scratch);
#endif
