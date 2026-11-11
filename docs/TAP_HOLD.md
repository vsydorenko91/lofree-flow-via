# Mod-tap and layer-tap

| Keycode | Meaning |
|---|---|
| `MT(mods, kc)` `0x2000..0x3fff` | tap = `kc` (a normal keyboard usage), hold = the modifiers |
| `LT(1, kc)` on a Windows base layer, `LT(3, kc)` on a Mac base layer (`0x4000 | layer<<8 | kc`) | tap = `kc`, hold = the Fn layer of the same pair |

Limits: at most **four** such positions per Windows/Mac pair (shared between MT and
LT). A position that is mod-/layer-tap on base and Fn must use the same code. An
invalid request (fifth key, empty modifier mask, consumer tap, LT on the Fn layer or
with an empty key, other layers) is rejected before RAM changes.

## Behavior

* Hold is chosen when the timeout expires, or earlier according to the setting
  **Flow84 tab → Typing → Tap-hold becomes hold**:
  * *When another key is pressed* (default, the behavior of 0.1/0.2): best for keys such as
    Caps Lock that are not part of normal typing.
  * *When another key is pressed and released* while the key is down: a quick roll
    (`key down, next key down, key up`) stays a tap, so it suits keys you type through.
  * *Only after the timeout*: other keys never decide.

  In the last two modes the keys pressed while a tap-hold key is undecided are held back
  and sent right after the decision, in the order they were pressed (so text typed in a roll
  comes out in order, at the cost of that short delay). Up to 16 presses are held back.
* In the default mode hold is chosen when another key is pressed while the key is pending. For **layer-tap, pressing a modifier does not count** as "another
  key" — `Ctrl+Space` with `Space` on LT still sends the chord (this was a real bug:
  the language switch stopped working).
* The tap is emitted when the key is released. This is inherent to tap-hold and gives
  a small typing delay; put these on rarely typed keys (Caps Lock, a modifier cluster)
  rather than on letters or Space if the delay bothers you.
* Timeout is a VIA setting (Flow84 tab → Typing): Normal 200 ms, Short 120 ms, Long 320 ms, Very
  long 500 ms. The engine counts matrix passes; they run at about 1000 per second (measured
  with `tools/device_check.py rate`), so one pass is about 1 ms.
* The pending key is never lost: taps queue until both USB reports (down and up)
  were really submitted; macros own the report while they play; leaving USB mode or
  switching OS discards queued taps.

## Implementation notes

The engine (`firmware/th.c`) wraps every stock matrix-scan call (13 call sites),
keeps a private shadow matrix and synthesizes presses on eight otherwise unused matrix
positions (columns 19 and 20 of rows 0..3, empty in every ROM keymap) that appear in the
keymap as the tap key and the hold action. Rows 0 and 1 were used on hardware since 0.1; rows 2
and 3 (the third and fourth tap-hold key) are verified in the emulator only so far. These slots cannot be remapped from VIA. Edits are
applied only when physical and synthetic input are idle. The current Fn state is
evaluated before choosing a layer, including Fn+key in the same scan.
