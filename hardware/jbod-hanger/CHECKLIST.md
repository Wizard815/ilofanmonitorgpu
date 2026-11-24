# JBOD fans: one-pass build checklist

The order is set up so nothing big gets printed, bought or wired before the
cheap checks pass. Fill in the blanks as you go; the measurements go
straight into `jbod_hanger.scad`.

**What can't be known until it's tried on your hardware:**
1. how the D3600 reacts with its fans removed
2. your rack's exact dimensions
3. whether three 140 mm fans move enough air through 12 drives

Steps 1–3 below settle each of these before you commit.

---

## 1. Go / no-go on the D3600 (about 30 min, nothing to buy)

- [ ] **Run the survey** on the Unraid host and keep the output. It only reads:
  ```bash
  bash tools/jbod_survey.sh 2>&1 | tee /tmp/jbod_survey.txt
  ```
  Check:
  - **Block devices:** every D3600 drive shows `expander=YES`. If none do, the
    controller hides the topology. Then list the drives by name in the
    dashboard's "Drives" setting, e.g. `disk1,disk2,…`.
  - **SES enclosures:** the D3600 appears, with its *Cooling* (fan) and
    *Temperature* elements. These are what to watch in the next step.
  - **poll_attributes:** if it isn't `60`, set Settings → Disk Settings →
    Tunable (poll_attributes) to **60**. At the default of 30 minutes, the
    drive temps the fans follow are stale.
- [ ] **Look at the back of the D3600.** Are the fans separate modules, or
      built into the power supplies? If they're inside the PSUs, **stop**:
      removing them leaves the PSUs uncooled. Write down which you have:
      `________________`
- [ ] **Pull ONE fan module** with the array running and idle, and watch for 10 minutes:
  - Front and back LEDs, and Unraid notifications
  - Does the other fan go to full speed?
  - Do any drives drop out?
  - Run the survey again and compare the SES section.
  - Put the module back.

  | Result | What to do |
  |---|---|
  | Just a warning, maybe the other fan at full speed | Go ahead |
  | Drives drop out, or the enclosure powers off | Stop. Don't remove the fans; the duct can still be added in front to help cool, with the stock fans left in. |

  Result: `________________`

## 2. Measure (15 min, tape and calipers)

Each answer maps to a parameter. Pass them to `./export.sh` (or edit the top of
`jbod_hanger.scad`). Measure **y** outward from the front face of the rack posts
(where equipment ears sit). Measure **z** up from the U boundary at the
*bottom* of the D3600: the middle of the narrower (12.7 mm) gap between rack holes.

| # | Measure | Parameter | Default | Yours |
|---|---|---|---|---|
| M1 | How far the frontmost part of any drive carrier (handle, latch) sticks out past the post face. Measure a few drives and take the largest, **+5 mm** for compressed foam. | `rim_y` | 30 | |
| M2 | Height of the bottom of the drive-carrier opening above the U boundary | `bay_z0` | 2 | |
| M3 | Height of its top | `bay_z1` | 87 | |
| M4 | Anything within 25 mm of each post's inner edge (ear handles, LEDs, a bezel lock) that sticks out **further than M1**? The duct's hinge ears sit there. | must be "no" | | |
| M5 | Is the 1U directly above the D3600 empty? | must be "yes" | | |
| M6 | How far the front of whatever sits in the U above that sticks out past the post face. When swung up, the duct reaches ~28 mm into that U, from ~25 mm out (the export prints exact numbers). Over ~20 mm → lower `open_angle`. | `open_angle` | 75 | |
| M7 | Room from the post face to the front door, if the rack has one. The closed duct sticks out **126 mm**, more with finger guards. | ≥ 140 | | |
| M8 | Rack holes: square (cage nuts) or threaded, and screw size. The slots fit M6, 10-32 and 12-24. | | | |
| M9 | Your printer's bed (shorter side) | `bed` | 180 | |
| M10 | Wi-Fi strength at the rack: a phone's Wi-Fi analyser, or the ESP32's `rssi` later. Better than −70 dBm is fine. | | | |

Then render and check:

```bash
openscad -D rim_y=XX -D bay_z0=XX -D bay_z1=XX -D 'part="assembly"' -o /tmp/a.stl hardware/jbod-hanger/jbod_hanger.scad
```

- An `ERROR: Assertion` names the measurement that doesn't fit. Fix it before printing anything.
- The `ECHO: open: …` line gives the swing-up reach and how far the closed duct sticks out (for M6 and M7).
- Put your final values at the top of `jbod_hanger.scad` so `./export.sh` and `./render.sh` use them.

## 3. Print the two test pieces (about 2 h, ~35 g)

- [ ] **`fit_gauge`** (3 mm flat profile, about 1 h). Hold it upright just
      inside a post at the D3600, with its flat back edge on the post's front
      face and the three back notches on the three holes of the U above the
      D3600. Check:
  - [ ] the rim line is 3–6 mm in front of the drive handles
  - [ ] the two rim notches line up with the top and bottom of the drive bays
  - [ ] the hinge plate and the duct outline touch nothing
  - [ ] there's room in front for the fan box drawn on it

  Any miss: change the measurement and reprint the gauge.
- [ ] **`pin_test`** (tray arm + duct ear). The M6 bolt should turn freely
      without wobble, and an M4 pin should slide through both lock holes.
      Drill to 6.5 / 4.3 mm if your printer runs tight.

## 4. Order parts

See [README.md → Hardware](README.md#hardware) and [esp32/README.md](../../esp32/README.md).
Choices that matter:
- **Fans:** pick high static pressure. Air has to get through 12 drive carriers
  and the midplane. Noctua NF-A14 industrialPPC-2000 PWM is the safe
  choice; Arctic P14 PWM is cheaper and quieter, with less pressure.
  Buy all three the same.
- **ESP32:** a plain ESP32-WROOM DevKit (30 or 38 pin). The pins used (25, 26, 27, 32, 33, 35, 4) are on both.
- **Power:** an external 12 V 2 A brick. No mains inside the printed tray.
- **DS18B20 probe:** get it. When the server is down, it lets the ESP32 follow
  real exhaust temperature instead of a fixed 80 %.

## 5. Bench test the electronics (1 h, before anything goes in the rack)

- [ ] Flash the firmware ([esp32/README.md](../../esp32/README.md)). The serial monitor should show `exhaust probe: found` and the failsafe %.
- [ ] Power up with all three fans connected: 100 % for 2 s, then 80 % (failsafe).
- [ ] In the dashboard: Behaviour → JBOD fans → enable and enter the ESP32's address. The link shows "ok" and the mode changes to "server".
- [ ] **Self-test fans** (dashboard button, ~36 s). Every fan must say `ok`; each
      other result says exactly what to fix (inverted PWM, crossed wires,
      missing tach pull-up). Note the rpm at 30 % for each fan: ________
- [ ] **Failure drills:**
  - [ ] Stop the container (`docker stop fan-bridge`). Within 20 s the mode goes to "probe (server lost)" or "failsafe". Start it again.
  - [ ] Unplug one fan while running. After ~5 s the other two go to 100 % and the LED blinks fast.
  - [ ] Warm the probe in your hand. With the server stopped, the fans rise.

## 6. Print and assemble (about 1.5 days of printing, ~600 g PETG/ASA)

- [ ] Export with your values: `./export.sh`
- [ ] Print the ducts rim-down, the tray pieces back-down, and the shelf floor-down; no supports. Use 4 walls, 30 % gyroid.
- [ ] Assemble ([README.md → Assembly](README.md#assembly)). Fans blow **into** the duct.
- [ ] Mount the tray in the 1U above the D3600 and hang the ducts. Check that the rim rests on the drive handles all the way across, that it swings up and pins, and that it swings down by itself.

## 7. Thermal acceptance (an afternoon)

Do this with the stock fans **still in** first, then with them out. The
dashboard shows the hottest JBOD drive; Unraid shows each drive.

| Run | Stock fans | New fans | Load | Hottest drive | Enclosure temp (survey, SES) |
|---|---|---|---|---|---|
| A | in | off / unplugged | parity check, 30 min | | |
| B | **out** | 100 % (Behaviour → "Never below" = 100) | parity check, 30 min | | |
| C | out | on the curve ("Never below" back to 25) | parity check to the end | | |
| D | out | on the curve | idle, overnight | | |

Pass/fail for run C:

| Hottest drive | Verdict |
|---|---|
| ≤ 113 °F (45 °C) | Pass |
| 113–122 °F | Marginal: improve the rim seal (foam gaps), move the curve left, or use higher-pressure fans |
| > 122 °F (50 °C), or SES temps far above run A | Fail: put the stock fans back |

Also check, during run B:
- [ ] no SES temperature element (I/O modules, PSUs) climbs well past run A
- [ ] no new enclosure warnings

## 8. Done

- [ ] Put your measured values and fan model in the "Yours" column above and commit the file.
- [ ] Run the self-test again whenever a fan is replaced.
