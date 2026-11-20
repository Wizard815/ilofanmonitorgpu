# ilofanmonitorgpu (fan-bridge)

Drives HPE iLO chassis fans from GPU temperatures. iLO cannot see the
temperature of add-in GPUs like the AMD Instinct MI50/MI60, so on its own
it never spins the fans up for them. fan-bridge reads the GPU sensors every
second and raises iLO's global minimum fan speed when the GPUs get busy or
hot, then hands control back to iLO once they cool down.

Tested on iLO 5 (v3.14, HPE ML350 Gen10) with two MI50s (gfx906, ROCm).

## Features

- **1 s polling straight from sysfs** (`/sys/class/drm/card*/device/hwmon`):
  edge, junction and HBM temperature, power and busy %. `rocm-smi` is the
  fallback.
- **One curve, one floor.** The control temperature is the hottest of
  junction and HBM + `mem_offset_c` across all GPUs. The curve maps it to a
  fan minimum.
- **Fast takeover:** GPU sensors are read every `poll_sec` (0.5 s; sysfs
  reads take ~1 ms). iLO writes run on their own thread, so a slow iLO
  (0.3-2 s per PATCH) never delays the next read, and only the newest value
  is sent. Load (`load_power_w` / `load_busy_pct`) jumps the minimum to
  `load_floor_pct` before the heat arrives, and while the temperature climbs
  the curve is read `predict_sec` ahead along the trend. The dashboard shows
  detection-to-applied time for the last raise.
- **Clean handoff:** fan-bridge learns what iLO runs the fans at on its own
  (or use `ilo_baseline_pct`). It only takes control when it needs more than
  that. Coming down, it holds for `hold_sec` (the timer waits while temps are
  still rising), steps `step_down_pct` every `step_down_sec`, and hands back
  to iLO as soon as the next step would be at or below iLO's own speed.
- **Presets:** Quiet, Balanced, Performance, Max cooling, plus your own named
  presets (curve, load minimum, look-ahead and step-down timing). One click
  to switch; editing any of those values shows "Custom".
- **iLO readback:** actual per-fan speeds, the current minimum and ambient
  temperature are read from iLO every `ilo_poll_sec` (and right after each
  change), so you can see whether the minimum is actually doing anything.
- **Live dashboard** at `:5000`: status, per-fan bars, 15-minute temperature
  and fan history, per-GPU sensors, a draggable curve that shows where you
  are on it right now, and settings that save as you edit them. Works on a
  phone, follows the OS light/dark setting.
- **Perf on demand (optional):** `power_dpm_force_performance_level` = `high`
  while the GPUs are in use, `auto` after `perf_idle_sec` idle. Needs the
  writable sysfs mounts below.
- **MCLK step (optional, off by default):** at container start, with the GPUs
  idle, raise the top memory clock state of the listed MI50s
  (`smcPPTable/FreqTableUclk/2` and `/3`, via [UPP](https://github.com/sibradzic/upp)).
  Backs up each pp_table first and restores it on any failure. Nothing is
  flashed: a reboot loads the stock VBIOS table.
- **Safety:** threads restart themselves after a crash; if GPU sensors vanish
  for `failsafe_after_polls` polls it sets `failsafe_fan_pct`; on start it
  reads the minimum iLO already has and holds/steps it down normally; settings
  are validated and written atomically; the Docker healthcheck fails if the
  loop stops polling. Without iLO credentials it runs in dry-run mode.

## How the fan control works

iLO 5 exposes one writable fan setting, the global minimum fan speed (the same
request iLO's own Administration -> Fan Settings dialog sends):

```
PATCH /redfish/v1/Chassis/1/Thermal
{"Oem":{"Hpe":{"FanPercentMinimum": <0-100>}}}
```

It raises the floor of every chassis fan at once; iLO's own thermal logic
still runs on top and can go higher. Per-fan control is not writable on this
firmware, and the host OS (Unraid included) has no access to the chassis fans
at all: they hang off the system board and only iLO drives them. A minimum
below what iLO is already running has no effect, which is why the dashboard
shows the actual fan speeds next to it.

## Setup

1. **Create an iLO user** for the script (not Administrator) with the
   *Login* and *Configure iLO Settings* privileges.
2. **Credentials:**
   ```bash
   mkdir -p secrets data
   cp ilo_creds.json.example secrets/ilo_creds.json   # then edit it
   ```
3. **Edit `docker-compose.yml`:** set `ILO_HOST`. Optionally add the writable
   GPU mounts (perf on demand, MCLK step) and the MCLK settings.
4. **Build and run:** `docker compose up -d --build`
5. **Dashboard:** `http://<host>:5000`

## Settings

All tuning lives in `data/settings.json` and in the dashboard; the loop
re-reads it every cycle. Main keys:

| Key | Default | Meaning |
|---|---|---|
| `thresholds` | 6-point curve | `[temp_c, fan_pct]` points on control temp |
| `predict_sec` | 3 | look ahead along a rising temperature (0 = off) |
| `ilo_baseline_pct` | 0 | what iLO runs on its own (0 = learn it) |
| `load_power_w` / `load_busy_pct` | 100 / 50 | what counts as "under load" |
| `load_floor_pct` | 40 | minimum while under load (0 = off) |
| `hold_sec` | 60 | hold before stepping down |
| `step_down_sec` / `step_down_pct` | 15 / 5 | how fast it steps down |
| `min_fan_pct` | 0 | idle minimum (0 = iLO fully in control) |
| `quantize_pct` | 5 | round targets up to this step |
| `poll_sec` / `ilo_poll_sec` | 0.5 / 5 | GPU and iLO read intervals |
| `mem_offset_c` | 11 | HBM counts as this much hotter (null = ignore HBM) |
| `failsafe_fan_pct` / `failsafe_after_polls` | 60 / 5 | minimum when sensors vanish |
| `perf_control` | false | perf on demand on/off |
| `perf_idle_sec` | 600 | idle time before `auto` |
| `perf_busy_pct` / `perf_power_w` | 20 / 80 | what counts as "in use" |

Settings from the old trigger/cooldown/release version are dropped on first
load; a customised 7-point curve is replaced by the default 6-point one.

API: `GET /api/state`, `GET /api/history?since=<ts>`,
`POST /api/settings` (any subset of the keys above, JSON),
`POST /api/presets/{apply,save,delete}` (`{"name": ...}`), `GET /api/fans`.

Environment (compose): `ILO_HOST`, `ILO_VERIFY_SSL`, `ILO_SECRETS_PATH`,
`SETTINGS_PATH`, `FAN_BRIDGE_MCLK` (MHz, or `off`), `FAN_BRIDGE_MCLK_PCI`
(comma-separated PCI addresses), `FAN_BRIDGE_MCLK_WAIT_SEC` (default 1800).

## Notes

- Tests: `python3 -m pytest tests`.

- Logs: `docker logs -f fan-bridge`, or the Log card on the dashboard.
- If a different iLO/firmware rejects the PATCH (`PropertyNotWritable`),
  capture the request iLO's own fan dialog sends (browser F12 -> Network) and
  update `ilo_fan.py` to match.
- The MCLK step and perf on demand write GPU sysfs files. Test any memory
  clock by hand first (perplexity unchanged, no RAS/dmesg errors, sane
  temperatures) before letting the container apply it at every start.
