# Klipper power-loss & crash recovery

Resume a print after a power cut or a frozen Klipper host. Nothing is written
continuously during the print. Instead, the printer **measures where the nozzle
stopped**, and the add-on works out exactly where in the G-code that was.

- **One button** in Mainsail: *POWER LOSS RESUME*
- Heats the nozzle **in place** first, so it lets go of the plastic it cooled in
- Measures the stop point **X, Y and Z** by counting motor steps back to the endstops
- Finds the **exact move** the nozzle was on (not just the layer)
- Recovers your **babystep Z offset**, keeps a **Z hop** out of it, and puts the
  **bed mesh** the print had back
- Shows one **confirmation popup** (file, layer, point, what it was printing, offset), then prints

> **Status:** works on the author's Ender 3 Pro (SKR Mini E3 V2, optical Z endstop,
> Klipper v0.13, Mainsail, Cura). It has only been tested on that one machine. Read
> [Requirements](#requirements) and [Safety](#safety) before using it on yours.

---

## Why (almost) "no writes"?

Most power-loss scripts for Klipper save the position to the SD card every layer. On a
Raspberry Pi that means constant small writes during every print. This add-on takes a
different approach:

- **The machine itself is the record.** With the power off, lead screws and belts don't
  move, so the nozzle is still where it stopped.
- **Klipper already logs enough.** Its `klippy.log` contains the print file position
  (`sd_pos`) about once a second. That's a normal log line, not something extra.
- **Two things the measurement can't tell apart from the nozzle height are noted when
  they change**, in `config/lost_z.json`: the Z gcode offset (babystep) and the active
  bed mesh. That's one small write at the start of a print, one when a mesh is made
  or loaded, and one per babystep - not a write every layer.

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
   The logged position can be a little **behind** the nozzle (the last log lines never
   reach the SD card when the host dies) or **ahead** of it (Klipper reads ahead of
   the moves it is running), so close to a layer change the neighbouring layer that
   fits the measured Z best is used. If the log doesn't fit the measurement at all, it
   falls back to the layer nearest the measured Z.
4. **Find the exact move.** Inside that layer, it picks the extruding move whose
   **path** (the whole segment or arc, not just its end points) passes closest to the
   measured X/Y, searched from shortly before the logged position to the end of the
   layer. When the move it was on and the next one both pass about as close (they
   share an end point), it takes the earlier one: a little overlap, never a gap.
5. **Babystep, mesh, hop.** The recorded babystep and bed mesh are taken off the
   measured Z, leaving the G-code Z - which tells a Z hop from a layer (see
   [Babysteps, bed mesh and Z hops](#babysteps-bed-mesh-and-z-hops)).
6. **Build the resume file** in its own folder (`gcodes/power_loss/`), where you can
   see it in Mainsail. The last measurement is saved in `config/lost_z.json`. The file:
   1. checks the printer is homed
   2. sets the offset and puts the print's bed mesh back (or none)
   3. heats bed and nozzle while parked in a corner
   4. primes, restores the extruder position and fan, and the last acceleration
      (`M204`, `SET_VELOCITY_LIMIT`) and `SET_PRESSURE_ADVANCE` the file had sent
   5. moves above the resume point, lowers to the layer height, sets the print speed
      the file was using there
   6. continues the original file from that move
7. **Confirm and print.** A Mainsail popup shows the file, the slicer, the layer, the
   resume point, the move type (outer wall, infill, … from the slicer's labels) and
   the offset. *Start print* builds the file and starts it.

### Slicers

The slicer is read from the file header, and its layer markers and move-type labels
are used:

| Slicer | Layer markers | Move-type labels |
|---|---|---|
| Cura | `;LAYER:n` (Z is set before the marker) | `;TYPE:WALL-OUTER` |
| PrusaSlicer, SuperSlicer | `;LAYER_CHANGE` + `;Z:0.6` | `;TYPE:External perimeter` |
| OrcaSlicer, Creality Print | `;LAYER_CHANGE` + `;Z:0.6` | `;TYPE:Outer wall` |
| Bambu Studio / Orca for Bambu printers | `; CHANGE_LAYER` + `; Z_HEIGHT: 0.6` | `; FEATURE: Outer wall` |
| Simplify3D | `; layer 3, Z = 0.600` | `; feature outer perimeter` |

A file with no layer markers at all still works: a layer starts at the first
extrusion above everything printed so far (Z hops don't count). Arcs (`G2`/`G3`,
e.g. PrusaSlicer's or Orca's arc fitting) are followed point by point, the way
Klipper's `[gcode_arcs]` prints them.

Tested on files from Cura 5.13 and PrusaSlicer 2.7 / 2.9 (with arcs, and a spiral
vase), plus Orca, Bambu and marker-less copies of the PrusaSlicer file. Orca, Bambu,
Creality Print and Simplify3D follow their documented formats but haven't been run on
a real file from them yet - reports welcome.

A measurement is only "fresh" until the next print starts, so an old crash's
measurement can't be reused by mistake. Pressing the button again before printing
won't re-measure the parked corner.

### Babysteps, bed mesh and Z hops

The measured Z is the real nozzle height. Three things besides the layer go into it,
and a power cut wipes the first two from memory:

```
measured Z = G-code Z + babystep + bed mesh correction at X/Y (faded with height)
```

- **Babystep** (Mainsail's Z +/- buttons, any `SET_GCODE_OFFSET`): recorded in
  `lost_z.json` each time it changes during the print.
- **Bed mesh**: the active mesh (its probe points, settings and fade) is recorded when
  it appears - loaded from a profile *or* made by `BED_MESH_CALIBRATE` in the start
  G-code. The correction at the measured X/Y is worked out with the same maths as
  Klipper's `bed_mesh`, and taken off.
- **What's left is the G-code Z** the nozzle was at. That is matched together with
  X/Y against every move around the logged position - travels and Z moves included.
  So a stop **during a Z hop** is found as the hop (the popup says "during a 0.20 mm Z
  hop") instead of being read as a layer higher up, or as a +0.2 mm babystep.

The resume file sends the recorded babystep (`SET_GCODE_OFFSET Z=...`) and resets it
to 0 at the end. It puts the **same bed mesh** back (`_LOST_Z_RESTORE_MESH` rebuilds it
from the recorded points, so an adaptive mesh made at print start works too), or
clears the mesh if the print had none - Klipper may have loaded a default one at
start-up.

A hop exactly as high as a layer is the hard case: hopping at 0.2 mm over a spot is
the same X/Y/Z as printing the next 0.2 mm layer there. The logged file position
decides which; without it, the **lower** layer is taken, so the worst case is a short
overlap rather than a missing piece of layer.

**If nothing was recorded** (an older install, or a cut within a second of the print
starting), it falls back to: babystep = measured Z − the matched move's G-code Z,
rounded to 0.005 mm and only accepted if it's a believable babystep - up to ±1 mm
when the log gives the position, ±0.15 mm when it doesn't. A mesh can't be separated
from the babystep that way.

A **saved** Z offset (`Z_OFFSET_APPLY_ENDSTOP` / `SAVE_CONFIG`) needs nothing extra:
it is part of the endstop position, so homing already includes it.

## Requirements

- **Klipper + Moonraker**, and **Mainsail** for the popups (any client that supports
  `action:prompt` should work).
- **A Z endstop the gantry triggers** (mechanical or optical switch), with the X/Y
  home corner clear of the part. Z-max homing should work too (the measurement only
  counts steps to the endstop, and homing moves away from the part) but is untested.
  **Not** a bed probe used as the Z endstop with `safe_z_home` at the bed centre:
  homing there would drive the nozzle into the part.
- **Axes that hold still with the power off:** lead-screw Z, normal belts. Don't touch
  the head after a power cut; the X/Y motors are unpowered. A belt-driven Z that drops
  without power won't work.
- **Cartesian or CoreXY** kinematics with steppers named `stepper_x`, `stepper_y`,
  `stepper_z`. Several Z motors are fine if they home together or square against
  their own endstops (homing does what it did at the start of the print). Only
  `stepper_z` is counted, so the height is measured on its side. **Not supported:**
  machines that need `Z_TILT_ADJUST` or `QUAD_GANTRY_LEVEL` before printing - those
  probe the bed, which can't be redone with a part on it.
- **A bed mesh is fine** - it's recorded and restored (see above).
- **G-code from a common slicer** (see [Slicers](#slicers)); others fall back to
  finding layers from the Z moves.

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
