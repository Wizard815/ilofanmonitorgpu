# jbod-fans (ESP32 firmware)

Drives three 4-pin PWM 140 mm fans that cool a JBOD's drive bays. fan-bridge
sends it a speed every few seconds; the ESP32 reads the fans' tach and an
optional exhaust probe, and falls back to safe speeds on its own.

## What happens when something breaks

These fans are the drives' only cooling, so every failure turns them **up**:

| Situation | Fans |
|---|---|
| Power on | 100 % for 2 s, then failsafe until the server checks in |
| Server sending normally | server's value, never below 20 % |
| Exhaust probe hotter than the server's value asks for | probe curve wins |
| No command for `ttl` s (server, container or Wi-Fi down) | probe curve (≥ 40 %) if fitted, else failsafe (default 80 %, set from the dashboard) |
| A fan stalls (asked ≥ 25 %, reads < 150 rpm for 5 s) | the other fans go to 100 % |

On-board LED: solid = server in charge, slow blink = failsafe/probe, fast blink = stalled fan.

## Wiring

```
 12 V 2 A brick ── DC jack ──┬──────────────── fan 1/2/3 pin 2 (+12 V)
                             └── buck 12→5 V ── ESP32 5V/VIN
 GND (common) ──────────────────────────────── fan pin 1, buck GND, ESP32 GND

 ESP32 GPIO25 ─────────────────── fan 1 pin 4 (PWM)
 ESP32 GPIO26 ─────────────────── fan 2 pin 4
 ESP32 GPIO27 ─────────────────── fan 3 pin 4

 3.3 V ─[10k]─┬─ ESP32 GPIO32 ─── fan 1 pin 3 (tach)
 3.3 V ─[10k]─┼─ ESP32 GPIO33 ─── fan 2 pin 3
 3.3 V ─[10k]─┴─ ESP32 GPIO35 ─── fan 3 pin 3

 DS18B20 (optional, taped in the JBOD's rear exhaust):
   red 3.3 V, black GND, yellow ─ GPIO4, with 4.7k from GPIO4 to 3.3 V
```

4-pin fan header, looking at the pins: 1 GND (black), 2 +12 V (yellow/red),
3 tach (green), 4 PWM (blue), with pin 1 at the end next to the key.

- **Pull tach lines up to 3.3 V only.** Pulling them up to 5 V or 12 V can kill the ESP32.
- **PWM straight from the GPIO** works for Noctua, Arctic and most current
  fans (they accept 3.3 V logic). If a fan ignores it, drive PWM through a
  2N7000 or an NPN as an open-drain stage, and set `PWM_INVERTED 1`.
- Power: three 140 mm fans draw 0.3–1 A at 12 V combined, depending on model. A 12 V 2 A brick
  is plenty. Keep mains out of the printed tray: use an external brick.

Everything fits on a 7 × 9 cm perfboard, which screws onto the shelf in
`hardware/jbod-hanger`.

## Flashing

**Prebuilt (no toolchain):** flash `firmware/jbod-fans-1.2.0-esp32.bin` at address
**0x0**, from https://espressif.github.io/esptool-js/ in Chrome/Edge or with
`esptool --chip esp32 write-flash 0x0 firmware/jbod-fans-1.2.0-esp32.bin`.
The full steps are in the [build guide](../docs/jbod/GUIDE.md#5-flash-the-esp32).

**First boot:** join the Wi-Fi network `jbod-fans-setup` (password `jbodfans`)
and fill in the page that opens (or go to `http://192.168.4.1/setup`): your
Wi-Fi, the API token (same as `JBOD_TOKEN`), a name and an update password.
Hold BOOT for 5 s to get back to setup later.

**Updates:** open `http://<ip>/update?token=<token>` and upload `jbod-fans-X.Y.Z-ota.bin`.

### Building from source

Rebuild the prebuilt images with `./build_release.sh`. It refuses to run while
a `secrets.h` exists, so your Wi-Fi details never end up in a shared file.

1. Optional: `cp jbod_fans/secrets.example.h jbod_fans/secrets.h` to pre-fill
   the setup page's defaults (handy for your own builds; never for a release).
2. Pick a build route:
   - **PlatformIO** (it fetches the libraries itself). Run these from `esp32/`:
     ```bash
     pio run -t upload
     ```
     ```bash
     pio device monitor
     ```
   - **Arduino IDE:** install the "esp32 by Espressif" board package and the
     **OneWire** and **DallasTemperature** libraries, open
     `jbod_fans/jbod_fans.ino`, board "ESP32 Dev Module", then upload.
     If you skip the probe, set `USE_EXHAUST_PROBE 0` and leave the libraries out.
3. Later updates over Wi-Fi: use the `/update` page, or
   ```bash
   pio run -e ota -t upload --upload-flags=--auth=YOUR_OTA_PASSWORD
   ```

Check it works (open `http://jbod-fans.local/` for the status page):

```bash
curl http://jbod-fans.local/status
```

```bash
curl -X POST -H "X-Token: change-me" "http://jbod-fans.local/set?pct=50&ttl=30"
```

Then run the self-test (or use **Self-test fans** on the dashboard) and read
`selftest` in the status after ~36 s. Every fan should say `ok`:

```bash
curl -X POST -H "X-Token: change-me" http://jbod-fans.local/selftest
```

The firmware builds with Arduino-ESP32 core 3.x (tested 3.3) and should also build on 2.x.

## API

| | |
|---|---|
| `GET /status` | `{mode, pct, cmd_pct, cmd_age_s, ttl_s, failsafe_pct, rpm[3], stalled[3], probe_c, server_temp_c, uptime_s, rssi, fw}` |
| `GET/POST /setup` | Wi-Fi, token, name, update password. Open on the setup network; needs `?token=` on your LAN. |
| `GET/POST /update` | Browser firmware upload (`-ota.bin`). Same access rule as `/setup`. |
| `POST /selftest` | header `X-Token`. ~36 s wiring check: all fans at 30/60/100 %, then each alone at 100 %. Results (rpm + a verdict per fan: ok, no tach, PWM ignored, PWM inverted, crossed wires) appear under `selftest` in `/status`. Also a button on the dashboard. |
| `POST /set?pct=N&ttl=S&fs=F&t=T` | header `X-Token`. pct 0–100, ttl 5–600 s, fs = failsafe 30–100 % (stored in flash), t = hottest drive (display only). Returns the status. |

Tune pins, the probe curve and the safety limits at the top of `jbod_fans.ino`.
