# Recovery and rollback

**Flashing replaces the application and erases the saved profile.** Export your VIA
layout and write down macros first. A failed flash leaves the keyboard in the vendor
bootloader (`3554:f800`), from which the upload can simply be repeated.

## Cases

1. **Keyboard works.** The stock vendor interface (`05ac:024f`, usage page `0xff02`)
   accepts the stock enter-update command; the bootloader then takes a new image.
2. **Keyboard is already in the bootloader.** Start the flash directly; a failed
   transfer is not a success — retry from the still-reachable bootloader.
3. **Nothing enumerates** (failure before the application and USB start). A hardware
   entry to the ROM/bootloader after such a failure has **not** been confirmed, so
   there is no guaranteed unbrick. The risk is small because the bootloader is
   separate and always checks image size and CRC before it erases.

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
checked each time.
