# Recovery and rollback

**Flashing replaces the application and erases the saved profile.** Export your VIA
layout and write down macros first. A failed flash leaves the keyboard in the vendor
bootloader (`3554:f800`), from which the upload can simply be repeated.

## Cases

1. **Keyboard works.** The stock vendor interface (`05ac:024f`, usage page `0xff02`)
   accepts the stock enter-update command; the bootloader then takes a new image.
2. **Keyboard is already in the bootloader.** Start the flash directly; a failed
   transfer is not a success — retry from the still-reachable bootloader.
3. **Nothing enumerates** (failure before the application and USB start). There is
   **no hardware entry** to the bootloader, so there is no unbrick over USB for an image
   that uploaded correctly but does not start; see below. The risk is small because the
   bootloader is separate and always checks image size and CRC before it erases.

## How the bootloader decides (from its disassembly)

At reset the bootloader configures clocks, reads 8 bytes of the settings area (selector 1,
word `0x807C` = sector A bytes `0x1F0..0x1F7`) and does exactly one test:

* bytes `12 34 56 78 9A BC`, a kind byte `D1`, `D2` or `D3`, and a checksum byte
  (`0x55` minus the sum of the first seven) → stay in update mode (`3554:f800`);
* anything else → jump to the application at `0x2020`. The application is **not**
  checked at boot (no CRC, no signature), and no key, pin or other register is read.

The stock "enter update mode" command runs the function at `0x1B7D4`: it erases sector A,
programs this marker with kind `D1` and resets. So:

* an interrupted or corrupted upload is safe: the marker is still there, and the keyboard
  comes back in update mode;
* an image that uploads completely (CRC correct) but crashes before USB works cannot be
  replaced over USB, because only a running application can write the marker;
* any firmware, including a future full replacement, can enter update mode itself by
  writing these 8 bytes and resetting.

`tests/test_bootloader_bounds.py` runs the real bootloader from reset in the emulator for
both outcomes, with random values in every hardware register.

## Return to stock

* Web flasher: **Restore stock firmware**.
* CLI: `python tools/flash.py restore-stock`.

Both write the byte-identical vendor image `stock/CompxUpgradeBinFile.bin`
(SHA-256 `7c061d9c…c7de`) and verify the bootloader echo of every packet and the
final success state (136). The EEPROM-less settings area keeps whatever the stock
firmware last wrote.

## No bypass mode

Earlier builds had a "hold a key while plugging in" bypass to the stock behavior. It was removed: it
added little (a broken build is fixed by reflashing anyway), and holding a key by accident silently
disabled VIA. The stock update path is independent of the new code and keeps working, and the
bootloader is untouched: that, not an application feature, is what protects against a brick.

## Rehearsed on hardware

Stock restored from a patched image (4380/4380 echoes, states 5 → 136) and the patched
image flashed again from stock (4468/4468); keyboard enumeration and typing were
checked each time. Version 0.3.0 was flashed over 0.2.0 with the CLI (states 5 → 136), the
profile restored over Raw HID and saved.
