#!/bin/sh
# Build the generic firmware and merge it into one image that flashes at 0x0
# (browser flashers, esptool). Output: esp32/firmware/jbod-fans-<ver>-esp32.bin
set -e
cd "$(dirname "$0")"
[ -f jbod_fans/secrets.h ] && { echo "move jbod_fans/secrets.h away first: a release must not contain your Wi-Fi"; exit 1; }
pio run -e esp32dev
VER=$(sed -n 's/#define FW_VERSION "\(.*\)"/\1/p' jbod_fans/jbod_fans.ino)
B=.pio/build/esp32dev
BOOT_APP0=$(ls ~/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin)
ESPTOOL=$(ls ~/.platformio/packages/tool-esptool/esptool 2>/dev/null || command -v esptool.py || command -v esptool)
mkdir -p firmware
OUT=firmware/jbod-fans-$VER-esp32.bin
"$ESPTOOL" --chip esp32 merge-bin -o "$OUT" --flash-mode dio --flash-freq 40m --flash-size 4MB \
  0x1000 $B/bootloader.bin 0x8000 $B/partitions.bin 0xe000 "$BOOT_APP0" 0x10000 $B/firmware.bin
cp $B/firmware.bin firmware/jbod-fans-$VER-ota.bin     # for over-the-air updates
( cd firmware && sha256sum jbod-fans-$VER-*.bin > SHA256SUMS-$VER.txt )
ls -la firmware
