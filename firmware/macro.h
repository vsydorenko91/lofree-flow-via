#ifndef FLOW_MACRO_H
#define FLOW_MACRO_H
#include <stdint.h>
/* VIA/QMK macro bytecode -> OEM macro records (12 bytes: u16 delay, 3 x
 * {type, u16 value}, pad). A record is a report STATE held for `delay`;
 * a zero-delay record ends a macro (the OEM player releases everything).
 * Type 1 = modifier mask, type 2 = keyboard usage.
 *
 * Bytecode: macros are separated by 0x00. Inside a macro: printable ASCII is
 * typed (US layout; \n = Enter, \t = Tab); 0x01 0x01 kc = tap, 0x01 0x02 kc =
 * down, 0x01 0x03 kc = up, 0x01 0x04 digits '|' = delay in ms. 16-bit
 * variants 0x01 0x05/6/7 take two little-endian keycode bytes (basic key and
 * modifier+key forms only). An incomplete trailing action is ignored. */
#define MACRO_RECORD 12u
#define MACRO_COUNT 8u
#define MACRO_TAP_MS 10u
enum { MACRO_OK = 1, MACRO_BAD = 0 };
/* Dry run when out == 0. Writes up to max records and the per-macro record
 * counts (each >= 1, includes the terminator). Returns MACRO_BAD on an
 * unsupported key, more than three simultaneous slots or record overflow. */
int macro_compile(const uint8_t *buf, unsigned len, uint8_t *out, unsigned max,
                  uint8_t lengths[MACRO_COUNT], unsigned *used);
#endif
