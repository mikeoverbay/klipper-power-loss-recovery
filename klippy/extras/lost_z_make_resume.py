#!/usr/bin/env python3
# Build <name>_resume.gcode: continue a Cura print from where it stopped.
#   - layer: from the file position Klipper logged (sd_pos in klippy.log), else the
#     layer nearest the measured Z
#   - exact point (MODE=exact): the extruding move whose path passes closest to the
#     measured X/Y, searched from just before the logged position to the layer end
#   - babystep Z offset: measured Z - layer Z, applied with SET_GCODE_OFFSET
# Used by MAKE_RESUME_FILE (lost_z.py); can also be run by hand:
#   python3 lost_z_make_resume.py --file part.gcode --z 33.41 --x 120 --y 98 --preview
#
# Copyright (C) 2026 mikeoverbay
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse, glob, json, os, re, sys, urllib.request

Z_MAX = 250.
MAX_OFFSET = 0.15     # without the log, offsets past ~half a layer are ambiguous
MAX_LOG_OFFSET = 1.0  # with the layer known from klippy.log, allow real babysteps up to 1 mm
# The logged file position can be seconds behind the nozzle (the last log lines
# never reach the SD card when the Pi dies), so search from a little before it
# to the end of the layer.
EXACT_BACK = 300      # lines before the logged file position to also search
NEAR_TIE = 0.3        # mm; passes this close to the best count as a tie
EXACT_MAX_DIST = 2.0  # mm; farther than this from every move -> fall back to the layer start
OFFSET_STEP = 0.005   # Mainsail's smallest babystep; also ~ the measuring accuracy
NUM = r'(-?\d+(?:\.\d+)?)'

def last_unfinished_job_file():
    # The newest print that didn't complete. After a power cut or crash nothing
    # runs after it, and a cut print isn't always recorded as "failed" (it can
    # show as cancelled), so don't filter on the failure kind.
    url = 'http://127.0.0.1:7125/server/history/list?limit=50&order=desc'
    with urllib.request.urlopen(url, timeout=5) as r:
        jobs = json.load(r)['result']['jobs']
    for job in jobs:
        if job.get('status') == 'in_progress':
            continue   # the print running now, if any
        if job.get('status') == 'completed':
            raise SystemExit("The last print (%s) completed normally - nothing to"
                             " resume" % (job['filename'],))
        return job['filename'], job['status']
    raise SystemExit("No unfinished print in the history")

def crash_file_pos(log_dir):
    # Klipper logs its read position in the print file (sd_pos) once a second.
    # The last one in the logs is where the crashed print was.
    last = None
    logs = sorted(glob.glob(os.path.join(log_dir, 'klippy.log*')),
                  key=os.path.getmtime)
    for f in logs:
        with open(f, errors='replace') as fh:
            for line in fh:
                if line.startswith('Stats ') and 'sd_pos=' in line:
                    m = re.search(r'sd_pos=(\d+)', line)
                    if m:
                        last = int(m.group(1))
    return last

def layer_at_byte(path, layers, pos):
    # Line index containing byte offset pos, then the layer it belongs to.
    with open(path, 'rb') as f:
        line_idx = f.read(pos).count(b'\n')
    cur = None
    for l in layers:
        if l[1] <= line_idx:
            cur = l
        else:
            break
    return cur

MOVE_TYPES = {'WALL-OUTER': 'outer wall', 'WALL-INNER': 'inner wall', 'FILL': 'infill',
              'SKIN': 'top/bottom skin', 'SUPPORT': 'support', 'SUPPORT-INTERFACE':
              'support interface', 'SKIRT': 'skirt/brim', 'PRIME-TOWER': 'prime tower'}

def move_type_at(path, idx):
    # Cura labels each section with ;TYPE:..., so the last label before the
    # line says what the nozzle was printing.
    kind = None
    with open(path, errors='replace') as f:
        for i, line in enumerate(f):
            if i > idx:
                break
            if line.startswith(';TYPE:'):
                kind = line[6:].strip()
    return MOVE_TYPES.get(kind, kind) if kind else 'unknown'

def line_at_byte(path, pos):
    with open(path, 'rb') as f:
        return f.read(pos).count(b'\n')

def find_crash_move(path, first_idx, last_idx, x, y, near_idx=None):
    # Pick the extruding move between first_idx and last_idx whose path
    # (the whole segment, not just its end points) passes closest to the
    # measured X/Y. If several passes are about as close, take the one nearest
    # near_idx (the logged file position).
    cx = cy = None
    e_abs, e_last = True, 0.
    cands = []
    with open(path, errors='replace') as f:
        for idx, raw in enumerate(f):
            if idx > last_idx:
                break
            line = raw.split(';')[0].strip()
            if not line:
                continue
            cmd = line.split()[0].upper()
            if cmd == 'M82':
                e_abs = True
            elif cmd == 'M83':
                e_abs = False
            elif cmd == 'G92':
                v = param(line, 'E')
                if v is not None:
                    e_last = v
            elif cmd in ('G0', 'G1'):
                nx, ny, e = param(line, 'X'), param(line, 'Y'), param(line, 'E')
                nx = cx if nx is None else nx
                ny = cy if ny is None else ny
                extrudes = e is not None and (e > e_last if e_abs else e > 0)
                if (idx >= first_idx and extrudes and None not in (cx, cy, nx, ny)):
                    cands.append((seg_dist(x, y, cx, cy, nx, ny), idx, cx, cy))
                if e is not None:
                    e_last = e if e_abs else e_last + e
                cx, cy = nx, ny
    if not cands:
        return None
    dmin = min(c[0] for c in cands)
    close = [c for c in cands if c[0] <= dmin + NEAR_TIE]
    if near_idx is not None:
        return min(close, key=lambda c: abs(c[1] - near_idx))
    return min(close, key=lambda c: c[0])

def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** .5
    t = max(0., min(1., ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** .5

def param(line, letter):
    m = re.search(r'(?:^|\s)' + letter + NUM, line)
    return float(m.group(1)) if m else None

def scan_layers(path):
    # Returns [(layer, line_index, z)]. Cura raises Z and travels to the next
    # start point *before* writing ;LAYER:n, so a layer's height is the Z in
    # effect at its marker.
    layers, z = [], 0.
    with open(path, errors='replace') as f:
        for idx, line in enumerate(f):
            if line.startswith(';LAYER:'):
                layers.append([int(line[7:].strip()), idx, z])
            elif line[:3] in ('G0 ', 'G1 '):
                v = param(line.split(';')[0], 'Z')
                if v is not None:
                    z = v
    return layers

def state_before(path, stop_idx):
    # Replay everything before stop_idx to find the machine state at that point.
    st = {'e': 0., 'abs_e': True, 'fan': None, 'hotend': None, 'bed': None,
          'speed': None, 'flow': None, 'retracted': False,
          'x': None, 'y': None, 'z': None}
    with open(path, errors='replace') as f:
        for idx, raw in enumerate(f):
            if idx >= stop_idx:
                break
            line = raw.split(';')[0].strip()
            if not line:
                continue
            cmd = line.split()[0].upper()
            if cmd in ('G0', 'G1'):
                e = param(line, 'E')
                if e is not None:
                    st['e'] = e if st['abs_e'] else st['e'] + e
                for axis in 'xyz':
                    v = param(line, axis.upper())
                    if v is not None:
                        st[axis] = v
            elif cmd == 'G92':
                e = param(line, 'E')
                if e is not None:
                    st['e'] = e
            elif cmd == 'M82':
                st['abs_e'] = True
            elif cmd == 'M83':
                st['abs_e'] = False
            elif cmd in ('M104', 'M109'):
                s = param(line, 'S')
                if s is not None:
                    st['hotend'] = s
            elif cmd in ('M140', 'M190'):
                s = param(line, 'S')
                if s is not None:
                    st['bed'] = s
            elif cmd == 'M106':
                s = param(line, 'S')
                st['fan'] = 255. if s is None else s
            elif cmd == 'M107':
                st['fan'] = 0.
            elif cmd == 'M220':
                st['speed'] = param(line, 'S')
            elif cmd == 'M221':
                st['flow'] = param(line, 'S')
            elif cmd == 'G10':
                st['retracted'] = True
            elif cmd == 'G11':
                st['retracted'] = False
    return st

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gcode-dir', default=os.path.expanduser('~/printer_data/gcodes'))
    ap.add_argument('--file')
    ap.add_argument('--z', type=float)
    ap.add_argument('--layer', type=int)
    ap.add_argument('--mode', choices=('exact', 'redo', 'next'), default='exact',
                    help="exact = from the move it stopped on, redo = whole layer again,"
                         " next = skip to the next layer")
    ap.add_argument('--x', type=float, help="measured X where it stopped")
    ap.add_argument('--y', type=float, help="measured Y where it stopped")
    ap.add_argument('--line', type=int, help="start at this file line (0-based), as checked")
    ap.add_argument('--pos', type=int, help="file byte position at the crash (default: from klippy.log)")
    ap.add_argument('--prime', type=float, default=5.)
    ap.add_argument('--name', help="save as this file name instead of <file>_resume.gcode")
    ap.add_argument('--log-dir', default=os.path.expanduser('~/printer_data/logs'))
    ap.add_argument('--out-dir', default='',
                    help="folder under the gcodes folder to save into (e.g. power_loss)")
    ap.add_argument('--idle-timeout', type=float,
                    help="normal idle timeout to restore at the end of the resume")
    ap.add_argument('--no-log', dest='use_log', action='store_false',
                    help="don't use klippy.log's file position to find the layer")
    ap.add_argument('--offset', type=float,
                    help="babystep Z offset to apply (default: measured Z - layer Z)")
    ap.add_argument('--preview', action='store_true',
                    help="only report what would be made, as one JSON line")
    a = ap.parse_args()

    picked, status = "", None
    if a.file:
        fname = a.file
    else:
        fname, status = last_unfinished_job_file()
        picked = "Using the last unfinished print: %s (%s)\n" % (fname, status)
    path = fname if os.path.isabs(fname) else os.path.join(a.gcode_dir, fname)
    if not os.path.isfile(path):
        # Don't quietly fall back to an older print - that's how the wrong
        # file got picked before.
        raise SystemExit("%sThe print file %s no longer exists, so it can't be"
                         " resumed" % (picked, fname))

    layers = scan_layers(path)
    if not layers:
        raise SystemExit("No ;LAYER: markers - only Cura-style files are supported")
    pos = a.pos if a.pos is not None else (crash_file_pos(a.log_dir) if a.use_log else None)
    if pos is not None and pos > os.path.getsize(path):
        pos = None
    if a.line is not None:
        # Start line already chosen by the preview: use its layer as-is.
        a.layer = [l for l in layers if l[1] <= a.line][-1][0]
    if a.layer is not None:
        pick = [l for l in layers if l[0] == a.layer]
        if not pick:
            raise SystemExit("Layer %d not in file (0-%d)" % (a.layer, layers[-1][0]))
        target = pick[0]
        if a.z is None:
            note = "layer chosen by hand"
        else:
            note = "measured Z %.3f, layer Z %.3f (diff %+.3f mm)" % (
                a.z, target[2], a.z - target[2])
    else:
        if a.z is None:
            raise SystemExit("Need --z or --layer")
        # Best source: the file position Klipper logged when it died. That
        # gives the layer directly, so any babystep size can be recovered.
        log_layer = None
        if pos is not None:
            cand = layer_at_byte(path, layers, pos)
            if cand is not None and abs(a.z - cand[2]) <= MAX_LOG_OFFSET:
                log_layer = cand
        if log_layer is not None:
            target = log_layer
            from_log = True
            note = ("crashed in layer %d per klippy.log (file byte %d); measured Z"
                    " %.3f vs layer Z %.3f" % (target[0], pos, a.z, target[2]))
        else:
            from_log = False
            target = min(layers, key=lambda l: abs(l[2] - a.z))
            note = "nearest layer to measured Z %.3f is Z %.3f (diff %+.3f mm)" % (
                a.z, target[2], a.z - target[2])
            if pos is not None:
                note += " - klippy.log position didn't match this Z, not used"
            if abs(a.z - target[2]) > 0.15:
                note += " - WARNING: not close to any layer, check before printing"
    # The babystep Z offset that was in effect: the nozzle physically sat at
    # layer Z + offset, so measured Z minus the stopped layer's Z gives it back.
    if a.offset is not None:
        offset, offset_note = a.offset, "Z offset %+.3f mm (as checked)" % (a.offset,)
    elif a.z is not None:
        raw = a.z - target[2]
        limit = MAX_LOG_OFFSET if (a.layer is None and from_log) else MAX_OFFSET
        if abs(raw) > limit:
            offset = 0.
            offset_note = ("Z offset NOT applied: measured is %+.3f mm from the"
                           " layer, too far to be a babystep" % (raw,))
        else:
            offset = round(raw / OFFSET_STEP) * OFFSET_STEP
            offset_note = "Z offset you had: %+.3f mm (will be applied)" % (offset,)
    else:
        offset, offset_note = 0., "Z offset: none (no measured Z)"
    if a.mode == 'next':
        later = [l for l in layers if l[1] > target[1]]
        if not later:
            raise SystemExit("Layer %d is the last layer" % (target[0],))
        target = later[0]
    layer, start_idx, layer_z = target

    # Exact: inside the layer, start from the move that passes closest to the
    # measured X/Y, searched just before where the log says it stopped.
    resume_point = "start of layer %d" % (layer,)
    mode_used = 'next' if a.mode == 'next' else 'redo'
    if a.mode == 'exact':
        later = [l for l in layers if l[1] > start_idx]
        layer_end = later[0][1] - 1 if later else 10 ** 9
        if a.line is not None:
            start_idx, mode_used = a.line, 'exact'
            resume_point = "file line %d (as checked)" % (a.line + 1,)
        elif a.x is None or a.y is None:
            resume_point += " (no measured X/Y, so whole layer redone)"
        else:
            crash_line = line_at_byte(path, pos) if pos is not None else None
            if crash_line is not None and start_idx <= crash_line <= layer_end:
                first = max(start_idx, crash_line - EXACT_BACK)
            else:
                crash_line, first = None, start_idx
            best = find_crash_move(path, first, layer_end, a.x, a.y, crash_line)
            if best is not None and best[0] <= EXACT_MAX_DIST:
                start_idx, mode_used = best[1], 'exact'
                resume_point = ("X%.2f Y%.2f, file line %d (%.2f mm from the measured"
                                " spot)" % (best[2], best[3], best[1] + 1, best[0]))
            else:
                resume_point += (" (no move within %.0f mm of X%.2f Y%.2f, so whole"
                                 " layer redone)" % (EXACT_MAX_DIST, a.x, a.y))

    base = os.path.basename(fname)
    if a.name:
        out_name = os.path.basename(a.name.strip())
        if not out_name or out_name.startswith('.'):
            raise SystemExit("Bad NAME: %r" % (a.name,))
        if not out_name.lower().endswith('.gcode'):
            out_name += '.gcode'
    else:
        # part.gcode -> part_resume.gcode; resuming a resume -> part_resume2.gcode
        stem, ext = os.path.splitext(base)
        m = re.match(r'^(.*)_resume(\d*)$', stem)
        if m:
            out_name = "%s_resume%d%s" % (m.group(1), int(m.group(2) or 1) + 1, ext or '.gcode')
        else:
            out_name = "%s_resume%s" % (stem, ext or '.gcode')
    out_dir = os.path.join(a.gcode_dir, a.out_dir) if a.out_dir else os.path.dirname(path)
    out_path = os.path.join(out_dir, out_name)
    if os.path.realpath(out_path) == os.path.realpath(path):
        raise SystemExit("NAME would overwrite the original file %s" % (base,))

    if a.preview:
        # One JSON line for the confirm popup; nothing is written.
        print(json.dumps({
            'file': fname, 'status': status, 'layer': layer, 'z': a.z,
            'layers': layers[-1][0], 'layer_z': layer_z, 'note': note,
            'offset': round(offset, 3), 'offset_note': offset_note,
            'mode': mode_used, 'line': start_idx, 'resume_point': resume_point,
            'move_type': move_type_at(path, start_idx),
            'out_name': out_name, 'out_exists': os.path.exists(out_path)}))
        return

    st = state_before(path, start_idx)
    if st['hotend'] is None or st['bed'] is None:
        raise SystemExit("Could not find hotend/bed temperatures in the file")
    if st['x'] is None or st['y'] is None:
        raise SystemExit("Could not find the start position of layer %d" % (layer,))
    safe_z = min(layer_z + 5., Z_MAX)
    e_mode = 'M82' if st['abs_e'] else 'M83'
    head = [
        "; Resume of %s from layer %d (Z=%.3f) at %s; %s" % (
            base, layer, layer_z, resume_point, note),
        "; Generated by lost_z_make_resume.py - needs RECOVER_FROM_POWER_LOSS (or G28) first",
        "_LOST_Z_CHECK_HOMED",
        "; %s" % (offset_note,),
        ("SET_GCODE_OFFSET Z=%.3f MOVE=0" % (offset,)) if offset
        else "SET_GCODE_OFFSET Z=0 MOVE=0",
        "M140 S%g" % st['bed'],
        "M104 S%g" % st['hotend'],
        "G90",
        "G1 Z%.3f F600 ; lift clear of the part" % safe_z,
        "G1 X5 Y5 F6000 ; wait at the front-left corner",
        "M190 S%g" % st['bed'],
        "M109 S%g" % st['hotend'],
        "M83",
        "G1 E%.2f F200 ; prime" % a.prime,
        # Cura retracts (G10) just before each layer and unretracts after the
        # first travel, so match the state the original file had here.
        ("G10 ; retract before moving over the part" if st['retracted']
         else "; (file was not retracted at this point)"),
        e_mode,
        "G92 E%.5f ; extruder position at layer %d" % (st['e'], layer),
        ("M106 S%g" % st['fan']) if st['fan'] else "M107",
    ]
    if st['speed']:
        head.append("M220 S%g" % st['speed'])
    if st['flow']:
        head.append("M221 S%g" % st['flow'])
    head += [
        "G0 X%.3f Y%.3f F6000 ; above the resume point" % (st['x'], st['y']),
        "G0 Z%.3f F600 ; down to layer height" % layer_z,
        "; ---- original file from line %d (layer %d) ----" % (start_idx + 1, layer),
    ]

    os.makedirs(out_dir, exist_ok=True)
    tmp = out_path + '.tmp'
    with open(path, errors='replace') as src, open(tmp, 'w') as dst:
        dst.write("\n".join(head) + "\n")
        for idx, line in enumerate(src):
            if idx >= start_idx:
                dst.write(line)
        # Klipper keeps a gcode offset until restart - don't let this one
        # leak into the next normal print.
        dst.write("\n; ---- end of resume ----\nSET_GCODE_OFFSET Z=0 MOVE=0\n")
        if a.idle_timeout:
            # RECOVER held the motors on; put the normal timeout back.
            dst.write("SET_IDLE_TIMEOUT TIMEOUT=%d\n" % (a.idle_timeout,))
    os.replace(tmp, out_path)
    print(picked + "Wrote %s\nResumes at layer %d of %d (Z=%.3f), %s.\n"
          "Hotend %g C, bed %g C, fan %s, E=%.3f. Start it from Mainsail."
          % (out_name, layer, layers[-1][0], layer_z, note, st['hotend'],
             st['bed'], ('%g' % st['fan']) if st['fan'] else 'off', st['e']))
    print("RESUME_PATH=%s" % (os.path.relpath(out_path, a.gcode_dir),))

if __name__ == '__main__':
    main()
