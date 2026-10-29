# Power-loss / host-crash recovery for Klipper - nothing written continuously
# during the print (only the babystep and bed mesh, when they change).
#
# After a power cut or a frozen host the lead screws and belts don't move, so the
# nozzle is still where it stopped. RECOVER_FROM_POWER_LOSS heats the nozzle in place
# (so it lets go of the plastic), lifts a little, homes X, Y and Z, and counts each
# motor's steps from the power-up spot to its endstop. Steps x step distance + the
# homed position = where the nozzle was. MAKE_RESUME_FILE (lost_z_make_resume.py)
# then finds the exact move it stopped on and writes a resume file into the
# gcodes/power_loss folder; START_RESUME_FILE prints it. RESUME_AFTER_POWER_LOSS does all of it with
# one confirmation popup (Mainsail action:prompt dialogs).
#
# Config:
#   [lost_z]
#   lift: 5                 # mm to raise before homing X/Y (clears the part)
#   park_x: 0               # optional X/Y to home Z at (must be clear of the part)
#   park_y: 0
#   min_nozzle_temp: 170    # heat to at least this before anything moves (0 = off)
#   resume_dir: power_loss  # folder under gcodes for resume files
#   state_file: ~/printer_data/config/lost_z.json  # last measurement (shown in Mainsail)
#   hold_homing_time: 7200  # idle timeout (s) held between recovery and resuming
#
# Copyright (C) 2026 mikeoverbay
# SPDX-License-Identifier: GPL-3.0-or-later
import json, logging, os, subprocess, sys, time

class LostZ:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.lift = config.getfloat('lift', 5., minval=0., maxval=50.)
        self.park_x = config.getfloat('park_x', None)
        self.park_y = config.getfloat('park_y', None)
        self.min_temp = config.getfloat('min_nozzle_temp', 170., minval=0.)
        self.state_file = os.path.expanduser(
            config.get('state_file', '~/printer_data/config/lost_z.json'))
        self.gcode_dir = os.path.expanduser(
            config.get('gcode_dir', '~/printer_data/gcodes'))
        self.resume_script = os.path.join(os.path.dirname(
            os.path.realpath(__file__)), 'lost_z_make_resume.py')
        # Resume files go in their own folder under gcodes, so they're easy to
        # find and don't mix with the slicer files; START_RESUME_FILE prints them.
        self.resume_dir = config.get('resume_dir', 'power_loss')
        # Between RECOVER and starting the resume, don't let the idle timeout
        # switch the motors off (that loses homing).
        self.hold_time = config.getfloat('hold_homing_time', 7200., minval=600.)
        self.last = self._load_state()
        self.proc = None
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('RECOVER_FROM_POWER_LOSS', self.cmd_RECOVER_FROM_POWER_LOSS,
                                    desc=self.cmd_RECOVER_FROM_POWER_LOSS_help)
        self.gcode.register_command('MAKE_RESUME_FILE', self.cmd_MAKE_RESUME_FILE,
                                    desc=self.cmd_MAKE_RESUME_FILE_help)
        self.gcode.register_command('_LOST_Z_CHECK_HOMED',
                                    self.cmd_LOST_Z_CHECK_HOMED)
        self.gcode.register_command('_LOST_Z_PROMPT_END',
                                    self.cmd_LOST_Z_PROMPT_END)
        self.gcode.register_command('START_RESUME_FILE', self.cmd_START_RESUME_FILE,
                                    desc=self.cmd_START_RESUME_FILE_help)
        self.gcode.register_command('_LOST_Z_START_PRINT',
                                    self.cmd_LOST_Z_START_PRINT)
        self.gcode.register_command('RESUME_AFTER_POWER_LOSS',
                                    self.cmd_RESUME_AFTER_POWER_LOSS,
                                    desc=self.cmd_RESUME_AFTER_POWER_LOSS_help)
        self.gcode.register_command('_LOST_Z_RESTORE_MESH',
                                    self.cmd_LOST_Z_RESTORE_MESH)
        self.autostart = False
        self.was_active = False
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        self.reactor.register_timer(self._watch_prints, self.reactor.NOW)

    def _watch_prints(self, eventtime):
        stats = self.printer.lookup_object('print_stats').get_status(eventtime)
        active = stats['state'] in ('printing', 'paused')
        if active:
            # A measurement is only "fresh" until the next print starts; after
            # that it belongs to an old crash and must be measured again.
            changed = bool(self.last.pop('fresh', False))
            # Record the two things a power cut wipes that the measurement
            # can't tell apart from the nozzle height: the babystep (Z gcode
            # offset) and the bed mesh. Written only when they change - at
            # the start and on each babystep - not continuously.
            rec = self.last.get('print') or {}
            if not self.was_active or rec.get('file') != stats['filename']:
                rec = {'file': stats['filename'], 'started': time.time()}
                changed = True
            gcode_move = self.printer.lookup_object('gcode_move')
            offset = round(gcode_move.get_status(eventtime)['homing_origin'][2], 4)
            if rec.get('offset') != offset:
                rec['offset'] = offset
                changed = True
            mesh_key, mesh = self._current_mesh()
            if rec.get('mesh_key') != mesh_key or 'mesh' not in rec:
                rec['mesh_key'], rec['mesh'] = mesh_key, mesh
                changed = True
            if changed:
                self.last['print'] = rec
                self._save_state()
        self.was_active = active
        return eventtime + 1.

    def _current_mesh(self):
        # (identity, record) of the active bed mesh; (None, None) if there
        # isn't one. The record holds what's needed to rebuild it and to redo
        # Klipper's fade maths later.
        bed_mesh = self.printer.lookup_object('bed_mesh', None)
        z_mesh = getattr(bed_mesh, 'z_mesh', None)
        if z_mesh is None:
            return None, None
        key = "%s/%x" % (z_mesh.profile_name, id(z_mesh))
        return key, {
            'name': z_mesh.profile_name,
            'params': dict(z_mesh.get_mesh_params()),
            'points': [list(r) for r in z_mesh.get_probed_matrix()],
            'offsets': list(getattr(z_mesh, 'mesh_offsets', [0., 0.])),
            'fade_start': bed_mesh.fade_start, 'fade_end': bed_mesh.fade_end,
            'fade_target': bed_mesh.fade_target,
            'tool_offset': getattr(bed_mesh, 'tool_offset', 0.)}

    def _build_mesh(self, rec):
        from . import bed_mesh
        z_mesh = bed_mesh.ZMesh(rec['params'], rec['name'])
        z_mesh.build_mesh(rec['points'])
        if hasattr(z_mesh, 'set_mesh_offsets'):
            z_mesh.set_mesh_offsets(rec.get('offsets', [0., 0.]))
        return z_mesh

    def _mesh_adjust(self, rec, x, y, z):
        # How much the mesh moved the nozzle at X/Y when the toolhead was at
        # (physical) height z - the same sums as bed_mesh's get_position().
        z_mesh = self._build_mesh(rec)
        fade_start, fade_end = rec['fade_start'], rec['fade_end']
        fade_target = rec['fade_target']
        max_adj = z_mesh.calc_z(x, y)
        z_adj = max_adj - fade_target
        fade_z = z + rec.get('tool_offset', 0.)
        factor = 1.
        if min(fade_z, fade_z - max_adj) >= fade_end:
            factor = 0.
        elif max(fade_z, fade_z - max_adj) >= fade_start:
            factor = ((fade_end + fade_target - fade_z)
                      / ((fade_end - fade_start) - z_adj))
            factor = min(1., max(0., factor))
        return factor * z_adj + fade_target

    def _load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.last, f)
        except Exception:
            logging.exception("lost_z: could not save state")

    def get_status(self, eventtime):
        return {'x': self.last.get('x'), 'y': self.last.get('y'),
                'z': self.last.get('z'), 'measured_at': self.last.get('time')}

    def _stepper(self, name):
        toolhead = self.printer.lookup_object('toolhead')
        for s in toolhead.get_kinematics().get_steppers():
            if s.get_name() == name:
                return s
        raise self.printer.command_error("lost_z: no %s found" % (name,))

    def _stepper_z(self):
        return self._stepper('stepper_z')

    cmd_RECOVER_FROM_POWER_LOSS_help = ("After a power loss: heat the nozzle, then find the"
                               " Z height it was left at (homes X, Y and Z)")
    def cmd_RECOVER_FROM_POWER_LOSS(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        gcode_move = self.printer.lookup_object('gcode_move')
        eventtime = self.reactor.monotonic()
        stats = self.printer.lookup_object('print_stats').get_status(eventtime)
        if stats['state'] in ('printing', 'paused'):
            raise gcmd.error("RECOVER_FROM_POWER_LOSS: a print is active")
        homed = toolhead.get_status(eventtime)['homed_axes']
        if 'z' in homed and not gcmd.get_int('FORCE', 0):
            raise gcmd.error(
                "RECOVER_FROM_POWER_LOSS: Z is already homed, so the power-up position is"
                " gone. Run it right after power-up / FIRMWARE_RESTART, before"
                " any homing (FORCE=1 to run anyway).")
        lift = gcmd.get_float('LIFT', self.lift, minval=0., maxval=50.)
        park_x = gcmd.get_float('X', self.park_x)
        park_y = gcmd.get_float('Y', self.park_y)
        stepper_z = self._stepper_z()
        # Keep the motors (and so the homing) on until the resume print starts;
        # the resume file puts the normal timeout back at its end.
        self.gcode.run_script_from_command(
            "SET_IDLE_TIMEOUT TIMEOUT=%d" % (self.hold_time,))
        heated_here = self._heat_nozzle_before_moving(gcmd)

        # Treat the power-up position as Z=0 so we can lift relative to it.
        toolhead.wait_moves()
        start_steps = stepper_z.get_mcu_position()
        # X and Y are measured the same way: steps from the power-up spot to
        # their endstops during G28 X Y.
        stepper_x, stepper_y = self._stepper('stepper_x'), self._stepper('stepper_y')
        start_x, start_y = stepper_x.get_mcu_position(), stepper_y.get_mcu_position()
        pos = toolhead.get_position()
        pos[2] = 0.
        toolhead.set_position(pos, homing_axes="z")
        gcode_move.reset_last_position()
        if lift:
            self.gcode.run_script_from_command("G90\nG1 Z%.3f F600" % (lift,))
        self.gcode.run_script_from_command("G28 X Y")
        toolhead.wait_moves()
        homed_pos = toolhead.get_position()
        lost_x = homed_pos[0] + (start_x - stepper_x.get_mcu_position()) * stepper_x.get_step_dist()
        lost_y = homed_pos[1] + (start_y - stepper_y.get_mcu_position()) * stepper_y.get_step_dist()
        if park_x is not None or park_y is not None:
            cur = toolhead.get_position()
            self.gcode.run_script_from_command("G1 X%.3f Y%.3f F6000" % (
                park_x if park_x is not None else cur[0],
                park_y if park_y is not None else cur[1]))
        self.gcode.run_script_from_command("G28 Z")
        toolhead.wait_moves()
        end_steps = stepper_z.get_mcu_position()
        z_after_home = toolhead.get_position()[2]
        travelled = (start_steps - end_steps) * stepper_z.get_step_dist()
        lost_z = z_after_home + travelled
        # Keep what was recorded during the print (babystep, bed mesh).
        keep = {k: self.last[k] for k in ('print', 'resume_mesh') if k in self.last}
        self.last = {'z': round(lost_z, 4), 'x': round(lost_x, 3),
                     'y': round(lost_y, 3), 'time': time.time(),
                     'start_steps': start_steps, 'end_steps': end_steps,
                     'z_after_home': z_after_home, 'fresh': True}
        self.last.update(keep)
        self._save_state()
        # Don't leave a hot nozzle sitting on the bed corner.
        self.gcode.run_script_from_command("G90\nG1 Z5 F600")
        if heated_here:
            self.gcode.run_script_from_command("M104 S0")
        gcmd.respond_info(
            "Nozzle was at X=%.2f Y=%.2f Z=%.3f when it stopped.\n"
            "(Z moved %.3f mm down to the endstop, incl. %.1f mm lift.)"
            " X, Y and Z are now homed%s. Next: MAKE_RESUME_FILE"
            % (lost_x, lost_y, lost_z, travelled, lift,
               ", nozzle heater off again" if heated_here else ""))

    def _heat_nozzle_before_moving(self, gcmd):
        # After a power cut the nozzle is frozen into the top layer. Soften the
        # plastic first so the lift doesn't tear the part off the bed.
        extruder = self.printer.lookup_object('extruder', None)
        if extruder is None or not self.min_temp:
            return False
        heater = extruder.get_heater()
        temp, target = heater.get_temp(self.reactor.monotonic())
        if temp >= self.min_temp:
            return False
        want = gcmd.get_float('TEMP', self.min_temp, minval=self.min_temp,
                              maxval=heater.max_temp)
        gcmd.respond_info("RECOVER_FROM_POWER_LOSS: nozzle is %.0f C - heating to"
                          " %.0f C in place before anything moves..." % (temp, want))
        self.gcode.run_script_from_command("M109 S%.0f" % (want,))
        temp, target = heater.get_temp(self.reactor.monotonic())
        if temp < self.min_temp - 2.:
            self.gcode.run_script_from_command("M104 S0")
            raise gcmd.error("RECOVER_FROM_POWER_LOSS: nozzle only reached %.0f C"
                             " (need %.0f C) - nothing was moved" % (temp, self.min_temp))
        return True

    cmd_RESUME_AFTER_POWER_LOSS_help = (
        "One button after a power cut or crash: heat, measure where it stopped"
        " (unless already measured), work out the resume point, confirm, print")
    def cmd_RESUME_AFTER_POWER_LOSS(self, gcmd):
        eventtime = self.reactor.monotonic()
        stats = self.printer.lookup_object('print_stats').get_status(eventtime)
        if stats['state'] in ('printing', 'paused'):
            raise gcmd.error("RESUME_AFTER_POWER_LOSS: a print is active")
        if self.last.get('fresh') and self.last.get('z') is not None:
            # Measuring again would read the parked corner, not the crash point.
            gcmd.respond_info(
                "Using the measurement already taken: X=%.2f Y=%.2f Z=%.3f"
                % (self.last.get('x') or 0., self.last.get('y') or 0., self.last['z']))
        else:
            self.cmd_RECOVER_FROM_POWER_LOSS(gcmd)
        self.autostart = True
        try:
            self.cmd_MAKE_RESUME_FILE(gcmd)
        except Exception:
            self.autostart = False
            raise

    cmd_MAKE_RESUME_FILE_help = ("Write <file>_resume.gcode that continues a print from"
                            " the measured Z (FILE= Z= LAYER= MODE=redo|next"
                            " NAME=)")
    def cmd_MAKE_RESUME_FILE(self, gcmd):
        if self.proc is not None:
            raise gcmd.error("MAKE_RESUME_FILE: already running")
        eventtime = self.reactor.monotonic()
        stats = self.printer.lookup_object('print_stats').get_status(eventtime)
        if stats['state'] in ('printing', 'paused'):
            raise gcmd.error("MAKE_RESUME_FILE: a print is active")
        fname = gcmd.get('FILE', None)
        layer = gcmd.get_int('LAYER', None)
        # The measured Z is used even with a hand-picked LAYER, so the babystep
        # offset isn't lost.
        z = gcmd.get_float('Z', self.last.get('z'))
        if layer is None and z is None:
            raise gcmd.error("MAKE_RESUME_FILE: no Z measured yet - run"
                             " RECOVER_FROM_POWER_LOSS or pass Z= / LAYER=")
        mode = gcmd.get('MODE', 'exact').lower()
        if mode not in ('exact', 'redo', 'next'):
            raise gcmd.error("MAKE_RESUME_FILE: MODE must be exact, redo or next")
        line = gcmd.get_int('LINE', None, minval=0)
        name = gcmd.get('NAME', None)
        offset = gcmd.get_float('OFFSET', None, minval=-1., maxval=1.)
        idle = self.printer.lookup_object('idle_timeout')
        args = [sys.executable, self.resume_script,
                '--gcode-dir', self.gcode_dir, '--mode', mode,
                '--out-dir', self.resume_dir,
                '--idle-timeout', '%d' % (idle.idle_timeout,)]
        if fname:
            args += ['--file', fname]
        if layer is not None:
            args += ['--layer', str(layer)]
        if z is not None:
            args += ['--z', '%.4f' % (z,)]
        if name:
            args += ['--name', name]
        if offset is not None:
            args += ['--offset', '%.3f' % (offset,)]
        if line is not None:
            args += ['--line', str(line)]
        x, y = self.last.get('x'), self.last.get('y')
        if x is not None and y is not None:
            args += ['--x', '%.3f' % (x,), '--y', '%.3f' % (y,)]
        # What was recorded during the print; the script only uses it if it is
        # for the file being resumed.
        rec = self.last.get('print') or {}
        if rec.get('file'):
            args += ['--record-file', rec['file'],
                     '--babystep', '%.4f' % (rec.get('offset') or 0.,)]
            if rec.get('mesh') and None not in (x, y, z):
                try:
                    adj = self._mesh_adjust(rec['mesh'], x, y, z)
                except Exception as e:
                    logging.exception("lost_z: bed mesh rebuild failed")
                    raise gcmd.error("MAKE_RESUME_FILE: couldn't rebuild the bed mesh"
                                     " recorded during the print (%s)" % (e,))
                args += ['--mesh-adj', '%.4f' % (adj,)]
        if gcmd.get_int('CONFIRM', 0):
            # The resume file's _LOST_Z_RESTORE_MESH puts back the mesh the
            # print had - or none - if the record is for this file.
            if fname and rec.get('file') == fname:
                self.last['resume_mesh'] = (
                    {'mode': 'set', 'mesh': rec['mesh']} if rec.get('mesh')
                    else {'mode': 'clear'})
            else:
                self.last['resume_mesh'] = {'mode': 'keep'}
            self._save_state()
            # Popup confirmed: build the file for real (and start it if this
            # came from the one-button flow).
            self.autostart = bool(gcmd.get_int('AUTOSTART', 0))
            self._prompt(["action:prompt_end"])
            gcmd.respond_info("MAKE_RESUME_FILE: building resume file...")
            self._run(args, self._built)
        else:
            # First press: work out what would be made and ask in a popup.
            # (autostart is set by RESUME_AFTER_POWER_LOSS just before this.)
            gcmd.respond_info("MAKE_RESUME_FILE: checking the print file...")
            self._run(args + ['--preview'], self._show_confirm)

    def _run(self, args, done):
        # Run in a separate process so a 20+ MB file doesn't stall Klipper.
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        self.proc_done = done
        self.reactor.register_timer(self._poll_proc, self.reactor.NOW)

    def _poll_proc(self, eventtime):
        if self.proc.poll() is None:
            return eventtime + .5
        out = self.proc.stdout.read().decode(errors='replace').strip()
        returncode = self.proc.returncode
        self.proc = None
        self.proc_done(returncode, out)
        return self.reactor.NEVER

    def _built(self, returncode, out):
        # The script's last line is RESUME_PATH=<path under gcodes>.
        lines = out.splitlines()
        path = None
        if not returncode and lines and lines[-1].startswith('RESUME_PATH='):
            path = lines.pop()[len('RESUME_PATH='):]
        self._report_result(returncode, "\n".join(lines))
        if path is None:
            return
        self.last['resume_path'] = path
        self._save_state()
        if not self.autostart:
            self._show_start_prompt("Resume file ready")
            return
        self.autostart = False
        homed = self.printer.lookup_object('toolhead').get_status(
            self.reactor.monotonic())['homed_axes']
        script = ""
        if homed != 'xyz':
            # Motors timed out since the measurement; RECOVER left the head
            # parked clear at the corner, so homing there is safe.
            self.gcode.respond_info("Homing first (head is parked clear at the corner)")
            script = "G28\n"
        self.gcode.run_script(script + "SDCARD_PRINT_FILE FILENAME=%s" % (path,))

    def _resume_header(self, path):
        # First line: what it resumes; the "Z offset ..." comment: the offset.
        try:
            with open(os.path.join(self.gcode_dir, path), errors='replace') as f:
                head = [f.readline() for _ in range(12)]
            first = head[0].strip().lstrip('; ')
            offset = next((l.strip().lstrip('; ') for l in head
                           if l.startswith('; Z offset')), "")
            return first, offset
        except OSError:
            return None, None

    def _show_start_prompt(self, title):
        path = self.last.get('resume_path')
        first, offset = self._resume_header(path) if path else (None, None)
        if first is None:
            self._prompt([
                "action:prompt_begin No resume file",
                "action:prompt_text There is no resume file yet. Run POWER LOSS"
                " RECOVER, then POWER LOSS MAKE RESUME.",
                "action:prompt_footer_button Close|_LOST_Z_PROMPT_END|secondary",
                "action:prompt_show"])
            return
        homed = self.printer.lookup_object('toolhead').get_status(
            self.reactor.monotonic())['homed_axes']
        lines = ["action:prompt_begin %s" % (title,),
                 "action:prompt_text %s" % (first,),
                 "action:prompt_text %s" % (offset,)]
        if homed != 'xyz':
            lines.append("action:prompt_text Printer isn't homed - press Home All"
                         " first (the head is parked clear at the corner after"
                         " RECOVER), then START RESUME.")
            lines.append("action:prompt_footer_button Close|_LOST_Z_PROMPT_END|secondary")
        else:
            lines.append("action:prompt_footer_button Later|_LOST_Z_PROMPT_END|secondary")
            lines.append("action:prompt_footer_button Start print|"
                         "_LOST_Z_START_PRINT|primary")
        lines.append("action:prompt_show")
        self._prompt(lines)

    def cmd_LOST_Z_START_PRINT(self, gcmd):
        self._prompt(["action:prompt_end"])
        path = self.last.get('resume_path')
        if not path or not os.path.exists(os.path.join(self.gcode_dir, path)):
            raise gcmd.error("START_RESUME_FILE: the resume file is missing -"
                             " run POWER LOSS MAKE RESUME again")
        self.gcode.run_script_from_command("SDCARD_PRINT_FILE FILENAME=%s" % (path,))

    cmd_START_RESUME_FILE_help = "Start the last resume file made by MAKE_RESUME_FILE"
    def cmd_START_RESUME_FILE(self, gcmd):
        eventtime = self.reactor.monotonic()
        stats = self.printer.lookup_object('print_stats').get_status(eventtime)
        if stats['state'] in ('printing', 'paused'):
            raise gcmd.error("START_RESUME_FILE: a print is active")
        self._show_start_prompt("Start resume print?")

    def _report_result(self, returncode, out):
        msg = out or "MAKE_RESUME_FILE: finished with no output"
        if returncode:
            self.gcode.respond_raw("!! " + msg.replace("\n", "\n!! "))
        else:
            self.gcode.respond_info(msg)

    def _prompt(self, lines):
        # Mainsail shows "// action:prompt_*" console lines as a popup dialog.
        for line in lines:
            self.gcode.respond_info(line, log=False)

    def _show_confirm(self, returncode, out):
        try:
            info = json.loads(out.splitlines()[-1]) if not returncode else None
        except (ValueError, IndexError):
            info = None
        if info is None:
            self._report_result(1, out)
            self._prompt([
                "action:prompt_begin Can't make a resume file",
                "action:prompt_text %s" % (out.replace("\n", " ") or "Unknown error",),
                "action:prompt_footer_button Close|_LOST_Z_PROMPT_END|secondary",
                "action:prompt_show"])
            return
        which = "last unfinished print, %s" % (info['status'],) \
                if info['status'] else "chosen by hand"
        save = info['out_name']
        if info['out_exists']:
            save += " (replaces the existing file)"
        # Send back exactly what was checked, including the offset worked out
        # against the layer it stopped in (LAYER may be the next one).
        if info.get('mode') == 'exact':
            where = "LINE=%d MODE=exact" % (info['line'],)
        else:
            where = "LAYER=%d MODE=redo" % (info['layer'],)
        confirm = ("MAKE_RESUME_FILE FILE=%s %s NAME=%s OFFSET=%.3f CONFIRM=1" % (
            info['file'], where, info['out_name'], info.get('offset', 0.)))
        if info.get('z') is not None:
            confirm += " Z=%.4f" % (info['z'],)
        if self.autostart:
            confirm += " AUTOSTART=1"
            title, button = "Resume after power loss?", "Start print"
        else:
            title, button = "Make resume file?", "Make it"
        self.autostart = False
        self._prompt([
            "action:prompt_begin %s" % (title,),
            "action:prompt_text Print: %s (%s)" % (info['file'], which),
            "action:prompt_text Sliced with: %s" % (info.get('slicer', 'unknown slicer'),),
            "action:prompt_text Resume at layer %d of %d, Z %.3f mm" % (
                info['layer'], info['layers'], info['layer_z']),
            "action:prompt_text Starts from: %s" % (info.get('resume_point', ''),),
            "action:prompt_text It was printing: %s" % (info.get('move_type', 'unknown'),),
            "action:prompt_text %s" % (info['note'],),
            "action:prompt_text %s" % (info.get('offset_note', ''),),
            "action:prompt_text Save as: %s" % (save,),
            "action:prompt_footer_button Cancel|_LOST_Z_PROMPT_END|secondary",
            "action:prompt_footer_button %s|%s|primary" % (button, confirm),
            "action:prompt_show"])
        self.gcode.respond_info("Resume check: %s, layer %d (Z %.3f) -> %s."
                                " Confirm in the popup." % (
                                    info['file'], info['layer'],
                                    info['layer_z'], info['out_name']))

    def cmd_LOST_Z_RESTORE_MESH(self, gcmd):
        # Called by the resume file: the same bed mesh as the stopped print,
        # rebuilt from the recorded probe points (so a mesh made in the start
        # G-code works too), or no mesh if it had none.
        want = self.last.get('resume_mesh') or {'mode': 'keep'}
        bed_mesh = self.printer.lookup_object('bed_mesh', None)
        if want['mode'] == 'keep':
            return
        if bed_mesh is None:
            if want['mode'] == 'set':
                raise gcmd.error("Resume file: the print used a bed mesh, but"
                                 " there's no [bed_mesh] in the config now")
            return
        if want['mode'] == 'clear':
            bed_mesh.set_mesh(None)
            gcmd.respond_info("Resume: no bed mesh, as during the print")
            return
        rec = want['mesh']
        bed_mesh.set_mesh(self._build_mesh(rec))
        if hasattr(bed_mesh, 'tool_offset'):
            bed_mesh.tool_offset = rec.get('tool_offset', 0.)
        gcmd.respond_info("Resume: bed mesh %s restored as during the print"
                          % (rec.get('name'),))

    def cmd_LOST_Z_PROMPT_END(self, gcmd):
        self._prompt(["action:prompt_end"])

    def cmd_LOST_Z_CHECK_HOMED(self, gcmd):
        toolhead = self.printer.lookup_object('toolhead')
        homed = toolhead.get_status(self.reactor.monotonic())['homed_axes']
        if homed != 'xyz':
            raise gcmd.error(
                "Resume file: the printer isn't homed (motors were off). If"
                " RECOVER_FROM_POWER_LOSS already ran and the head is still parked"
                " at the corner, just Home All (G28) and start the resume again."
                " Don't run RECOVER again.")

def load_config(config):
    return LostZ(config)
