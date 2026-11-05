# ilofanmonitorgpu (fan-bridge)

Drives HPE iLO chassis fans from GPU temperatures. iLO cannot see the
temperature of add-in GPUs like the AMD Instinct MI50/MI60 or NVIDIA cards, so on its own
it never spins the fans up for them. fan-bridge reads the GPU sensors every
second and raises iLO's global minimum fan speed when the GPUs get busy or
hot, then hands control back to iLO once they cool down.

Tested on iLO 5 (v3.14, HPE ML350 Gen10) with two MI50s (gfx906, ROCm).

## Features

- **1 s polling straight from sysfs** (`/sys/class/drm/card*/device/hwmon`):
  edge, junction and HBM temperature, power and busy %. `rocm-smi` is the
  fallback.
- **Load trigger:** as soon as any GPU draws `load_power_w` or reaches
  `load_busy_pct`, the fan floor jumps to `load_floor_pct`, before the heat
  arrives. Junction can rise 20+ C within a second of load, faster than a
  temperature-only loop can react.
- **Fan curve** (7 draggable points) on junction temperature, and on HBM +
  `mem_offset_c` (HBM has a lower limit than junction).
- **Fast up, slow down:** the floor rises at once, but drops only after the
  lower value has held `step_down_sec`, by at most `step_down_pct` per step.
- **Cooldown / release timers** before handing the fans back to iLO.
- **Perf on demand (optional):** `power_dpm_force_performance_level` = `high`
  while the GPUs are in use, `auto` after `perf_idle_sec` idle (about half the
  idle power, cooler heatsinks). Off = always `high`. Needs the writable sysfs
  mounts below.
- **MCLK step (optional, off by default):** at container start, with the GPUs
  idle, raise the top memory clock state of the listed MI50s
  (`smcPPTable/FreqTableUclk/2` and `/3`, via [UPP](https://github.com/sibradzic/upp)).
  Skips cards already at the target, backs up each pp_table first and restores
  it on any failure. Never touches voltages, timings, SCLK or the power limit.
  Nothing is flashed: a reboot loads the stock VBIOS table.
- **Safety:** the loop restarts itself after a crash; if GPU sensors vanish
  it pushes `failsafe_fan_pct`; a stale floor left by a restart is released;
  Docker healthcheck fails if the loop stops polling.
- **Dashboard** at `:5000` with all settings, live per-GPU sensors, MCLK and
  perf mode, and the log.

## NVIDIA GPUs (this branch)

`GPU_VENDOR` picks the sensor source: `nvidia` (nvidia-smi), `amd` (sysfs,
then rocm-smi) or `auto` (NVIDIA if `nvidia-smi` is present, else AMD). The
compose file here runs with `runtime: nvidia` and
`NVIDIA_DRIVER_CAPABILITIES=utility`, which needs the NVIDIA Container Toolkit
(on Unraid: the Nvidia Driver plugin).

What is read per GPU: core temperature, memory temperature (HBM data-center
cards only; GeForce/Quadro report N/A), power, utilization and memory clock.

Differences from AMD:

- NVML has no hotspot/junction sensor, so the curve runs on the **core**
  temperature, which reads well below an AMD junction. Lower the curve and
  `target_max_c` to match: NVIDIA cards start to slow down around 83-90 C core
  (check `nvidia-smi -q -d TEMPERATURE` for your card's slowdown temperature).
- `mem_offset_c` applies to the HBM temperature when the card reports one.
- Perf on demand and the MCLK step are AMD MI50/MI60 only and stay off.

Not tested on real NVIDIA hardware yet; the parsing was tested against
sample `nvidia-smi` output.

## How the fan control works

iLO 5 exposes one writable fan setting, the global minimum fan speed (the same
request iLO's own Administration -> Fan Settings dialog sends):

```
PATCH /redfish/v1/Chassis/1/Thermal
{"Oem":{"Hpe":{"FanPercentMinimum": <0-100>}}}
```

It raises the floor of every chassis fan at once. Per-fan control is not
writable on this firmware.

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
| `thresholds` | 7-point curve | `[temp_c, fan_pct]` points |
| `target_max_c` | 78 | trigger: script takes over at or above this |
| `load_power_w` / `load_busy_pct` | 100 / 50 | what counts as "under load" |
| `load_floor_pct` | 40 | floor while under load (0 = load trigger off) |
| `step_down_sec` / `step_down_pct` | 20 / 5 | how slowly the floor drops |
| `cooldown_sec` / `release_sec` | 60 / 120 | timers before handing back to iLO |
| `min_fan_pct` | 0 | floor after release (0 = iLO fully in control) |
| `idle_poll_sec` / `active_poll_sec` | 1 / 1 | poll intervals |
| `mem_offset_c` | 11 | HBM is treated as this much hotter (null = ignore HBM) |
| `failsafe_fan_pct` / `failsafe_after_polls` | 60 / 5 | floor when sensors vanish |
| `perf_control` | false | perf on demand on/off |
| `perf_idle_sec` | 600 | idle time before `auto` |
| `perf_busy_pct` / `perf_power_w` | 20 / 80 | what counts as "in use" |

Environment (compose): `ILO_HOST`, `ILO_VERIFY_SSL`, `ILO_SECRETS_PATH`,
`SETTINGS_PATH`, `FAN_BRIDGE_MCLK` (MHz, or `off`), `FAN_BRIDGE_MCLK_PCI`
(comma-separated PCI addresses), `FAN_BRIDGE_MCLK_WAIT_SEC` (default 1800).

## Notes

- Logs: `docker logs -f fan-bridge`, or the Log card on the dashboard.
- If a different iLO/firmware rejects the PATCH (`PropertyNotWritable`),
  capture the request iLO's own fan dialog sends (browser F12 -> Network) and
  update `ilo_fan.py` to match.
- The MCLK step and perf on demand write GPU sysfs files. Test any memory
  clock by hand first (perplexity unchanged, no RAS/dmesg errors, sane
  temperatures) before letting the container apply it at every start.
