# Klipper power-loss & crash recovery

Resume a print after a power cut or a frozen Klipper host. Nothing is written
during the print. Instead, the printer **measures where the nozzle stopped**,
and the add-on works out exactly where in the G-code that was.

- **One button** in Mainsail: *POWER LOSS RESUME*
- Heats the nozzle **in place** first, so it lets go of the plastic it cooled in
- Measures the stop point **X, Y and Z** by counting motor steps back to the endstops
- Finds the **exact move** the nozzle was on (not just the layer)
- Recovers your **babystep Z offset**
- Shows one **confirmation popup** (file, layer, point, what it was printing, offset), then prints

> **Status:** works on the author's Ender 3 Pro (SKR Mini E3 V2, optical Z endstop,
> Klipper v0.13, Mainsail, Cura). It has only been tested on that one machine. Read
> [Requirements](#requirements) and [Safety](#safety) before using it on yours.

---

## Why "no writes"?

Most power-loss scripts for Klipper save the position to the SD card every layer. On a
Raspberry Pi that means constant small writes during every print. This add-on takes a
different approach:

- **The machine itself is the record.** With the power off, lead screws and belts don't
  move, so the nozzle is still where it stopped.
- **Klipper already logs enough.** Its `klippy.log` contains the print file position
  (`sd_pos`) about once a second. That's a normal log line, not something extra.

## How it works

1. **Heat in place.** If the nozzle is below `min_nozzle_temp` (170 °C), it's heated
   with **no motion** until it gets there. After a power cut the nozzle is frozen into
   the top layer, and lifting it cold would rip the part off the bed. If it can't reach
   temperature, the heater is switched off and nothing moves.
2. **Measure.** Klipper keeps each stepper's step count continuous through
   `set_position()` and homing. Before anything moves, the add-on reads the Z, X and Y
   step counts. It then lifts 5 mm, homes X/Y, homes Z (at the X/Y home corner) and
   reads them again:

   ```
   position where it stopped = position after homing + (steps before − steps after) × step distance
   ```

3. **Find the layer.** The last `sd_pos` in `klippy.log` gives the layer directly. This
   matters: with a big babystep, the measured Z alone could point at the wrong layer.
   If the log doesn't fit the measurement, it falls back to the layer nearest the
   measured Z.
4. **Find the exact move.** Inside that layer, it picks the extruding move whose
   **path** (the whole segment, not just its end points) passes closest to the measured
   X/Y. The logged position is seconds **behind** the nozzle: the last log lines never
   reach the SD card when the host dies, and the MCU keeps running its buffered moves.
   So the search runs from shortly before the logged position to the end of the layer.
5. **Recover the babystep.** `measured Z − layer Z` = the `SET_GCODE_OFFSET` that was
   active. The resume file re-applies it and resets it at the end.
6. **Build the resume file** in a hidden folder (`gcodes/.power_loss/`), so it can't be
   deleted by mistake. The file:
   1. checks the printer is homed
   2. sets the offset
   3. heats bed and nozzle while parked in a corner
   4. primes, restores the extruder position and fan
   5. moves above the resume point, lowers to the layer height
   6. continues the original file from that move
7. **Confirm and print.** A Mainsail popup shows the file, the layer, the resume point,
   the move type (outer wall, infill, …, from Cura's `;TYPE:` labels) and the offset.
   *Start print* builds the file and starts it.

A measurement is only "fresh" until the next print starts, so an old crash's
measurement can't be reused by mistake. Pressing the button again before printing
won't re-measure the parked corner.

## Requirements

- **Klipper + Moonraker**, and **Mainsail** for the popups (any client that supports
  `action:prompt` should work).
- **A Z endstop at the bottom of travel that the gantry triggers** (mechanical or
  optical switch). **Not** a bed probe used as the Z endstop with `safe_z_home` at the
  bed centre: homing there would drive the nozzle into the part.
- **Axes that hold still with the power off:** lead-screw Z, normal belts. Don't touch
  the head after a power cut; the X/Y motors are unpowered.
- **Cartesian or CoreXY** kinematics with steppers named `stepper_x`, `stepper_y`,
  `stepper_z`. Only `stepper_z` is read, so multi-Z setups assume the Z motors stay
  in sync.
- **Cura-style G-code** (`;LAYER:n` markers, Z set before each marker). Other slicers
  would need their own layer parsing. Arcs (`G2`/`G3`) aren't handled yet.

## Install

```bash
cd ~
git clone https://github.com/mikeoverbay/klipper-power-loss-recovery.git
cd klipper-power-loss-recovery
./install.sh
```

`install.sh` does three things:
- links the two Python files into `~/klipper/klippy/extras/`
- copies `config/power_loss.cfg` into `~/printer_data/config/` (unless it already exists)
- tells Klipper's git to ignore the linked files, so updates don't complain

Then add to `printer.cfg`:

```ini
[include power_loss.cfg]

[lost_z]
lift: 5
#park_x: 0     # only if X/Y homing can leave the head above a part
#park_y: 0
```

Then restart the Klipper **service** (`sudo systemctl restart klipper`). A Klipper
`RESTART` command doesn't load new Python modules.

Optional, for updates through Mainsail's update manager (`moonraker.conf`):

```ini
[update_manager klipper-power-loss-recovery]
type: git_repo
path: ~/klipper-power-loss-recovery
origin: https://github.com/mikeoverbay/klipper-power-loss-recovery.git
primary_branch: main
managed_services: klipper
install_script: install.sh
```

## Using it after a power cut or crash

1. Power back on. If Klipper shows "shutdown", use **FIRMWARE_RESTART**. **Don't home.**
2. Heat the bed to the print temperature and check the part is still stuck.
3. **Check the X/Y home corner is clear:** Z homes there and the gantry comes down to
   the bed.
4. Press **POWER LOSS RESUME**. It heats, lifts, homes, measures, and then shows the
   popup.
5. Check the popup: *Print* (the right file?), *Starts from*, *It was printing*,
   *Z offset*.
6. **Start print.**

## Commands

| Command | What it does |
|---|---|
| `RESUME_AFTER_POWER_LOSS` | All of the below, with one popup (the Mainsail button). Params: `TEMP`, `MODE`, `LIFT` |
| `RECOVER_FROM_POWER_LOSS` | Heat in place, lift, home, measure X/Y/Z. Refuses if Z is already homed (`FORCE=1` overrides) or a print is active |
| `MAKE_RESUME_FILE` | Preview popup, then build the resume file. `MODE=exact` (default), `redo` (whole layer) or `next`; `Z=`, `LAYER=`, `FILE=`, `NAME=` overrides |
| `START_RESUME_FILE` | Popup to start the last resume file (offers Start only when homed) |

`lost_z_make_resume.py` also runs on its own, which is handy for checking a result
without touching the printer:

```bash
python3 ~/klipper/klippy/extras/lost_z_make_resume.py --file part.gcode --z 1.185 --x 107 --y 134.9 --preview
```

## Test results (author's machine)

**Measuring Z.** For each height, Z was moved there, then `FIRMWARE_RESTART` made
Klipper forget the position, then it was measured:

| Z before reset | Measured | Error |
|---|---|---|
| 3.000 | 3.000 | 0.000 mm |
| 12.345 | 12.342 | −0.003 mm |
| 30.000 | 30.003 | +0.003 mm |
| 57.800 | 57.800 | 0.000 mm |
| 100.000 | 100.005 | +0.005 mm |

That's within ±2 microsteps (0.0025 mm each on an 8 mm lead screw at 16 microsteps).
The optical endstop helps.

**Heat-before-move.** From a 20 °C nozzle, all three motors stayed off for 34 s while it
heated to 170 °C. Only then did it lift and home.

**Exact resume.** Two real mid-print stops, both in infill. The chosen resume
move was **0.06 mm** and **0.07 mm** from the measured X/Y. In both cases the logged
file position was a couple of seconds **behind** the nozzle, which is why the search
runs forward from it.

## Safety

- **Z homes at the X/Y home corner** (X0 Y0 unless you set `park_x`/`park_y`). Nothing
  may be under the nozzle there.
- **Never home before recovering.** That throws away the measurement. The command
  refuses if Z is already homed.
- **Don't push the head after a power cut.** X/Y are measured from where it sits.
- **When the X/Y motors switch on,** they can snap up to ±2 full steps. Z is the
  accurate axis; X/Y are good to a few hundredths of a millimetre, with ±0.4 mm worst
  case on 20-tooth GT2 pulleys.
- **The part must stay stuck** while it's cold, and the join is usually visible. This is
  a rescue, not a seamless resume.
- **Check the popup's *Print* line** before pressing *Start print*. It uses the newest
  unfinished job in Moonraker's history.

## License

GPL-3.0-or-later, the same as Klipper. See [LICENSE](LICENSE).
