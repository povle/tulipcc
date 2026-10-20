# cynthia/runtime.py
# The CYNTHIA PLAY-mode manager (§7, §17).
#
# Instruments are amyworld sketch FOLDERS loaded from /user/instruments/
# (§17): selecting one copies its sketch.py into /user/current/ and runs it
# through amyboard.run_sketch() — the exact loader the web editor and AMYboard
# boot use. A CYNTHIA-aware sketch additionally exposes a cynthia_instrument()
# hook returning an engine object (see cynthia/instruments/), and this module
# drives that engine exactly like the original Arduino firmware drove
# InstrumentJuno / InstrumentDX7:
#
#   - pots        -> engine.update_pot(channel, value)
#   - patch btns  -> engine.set_patch() / patch provider (§12)
#   - octave btns -> cynthia.midi.octave_shift()
#   - display     -> engine.draw_ui(disp, y_offset)
#
# Keyboard and joystick-in-play are NOT handled here: they feed AMY as MIDI
# sources (cynthia.midi / spark_main), so the loaded sketch plays them with no
# instrument-specific code (§6).
#
# Usage from REPL:
#   import cynthia.runtime as rt
#   rt.play("juno")      # load + run /user/instruments/juno
#   rt.next_patch(); rt.prev_patch()
#   rt.stop()

import sys
import time
import tulip

from . import surface, midi, display, i18n
from .i18n import t

# Button indices within spark_main's control-button events (§10.1).
BTN_OCTAVE_DOWN = 0
BTN_OCTAVE_UP = 1
BTN_PATCH_PREV = 2
BTN_PATCH_NEXT = 3

# Standard knob names (Instrument.h §standard pot mapping)
# 0-3: Custom (instrument-specific)
# 4: LFO Rate
# 5: LFO Depth
# 6: LFO Target
# 7: Cutoff
# 8: Resonance
# 9: Attack
# 10: Decay
# 11: Sustain
# 12: Release
# 13: Delay Level
# 14: Delay Time
# 15: Glide
# i18n KEYS, not literals: the overlay looks them up with t() every paint, so
# switching language takes effect immediately without rebuilding the table.
_POT_NAME_KEYS = [
    "custom1", "custom2", "custom3", "custom4",
    "rate", "depth", "target", "cutoff",
    "resonance", "attack", "decay", "sustain",
    "release", "delay", "delay_t", "glide",
]

# --- User patch system (§12) ---

PATCHES_DIR = "/user/patches"


def _patches_dir(inst_name):
    return PATCHES_DIR + "/" + inst_name


def save_patch(inst_name, idx, name, state_dict):
    """Save a user patch as JSON under /user/patches/<inst_name>/<idx>.json.
    state_dict is whatever the instrument wants to persist.
    Returns True on success."""
    import os, json
    d = _patches_dir(inst_name)
    try:
        os.mkdir(PATCHES_DIR)
    except OSError:
        pass
    try:
        os.mkdir(d)
    except OSError:
        pass
    path = d + "/" + str(idx) + ".json"
    try:
        with open(path, "w") as f:
            json.dump({"name": name, "state": state_dict}, f)
        return True
    except OSError:
        return False


def load_patch(inst_name, idx):
    """Load a user patch dict from JSON, or None."""
    import json
    path = _patches_dir(inst_name) + "/" + str(idx) + ".json"
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete_patch(inst_name, idx):
    """Delete a user patch file."""
    import os
    path = _patches_dir(inst_name) + "/" + str(idx) + ".json"
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def list_user_patches(inst_name):
    """Return sorted list of (idx, name) for saved user patches."""
    import os, json
    d = _patches_dir(inst_name)
    try:
        files = os.listdir(d)
    except OSError:
        return []
    result = []
    for fname in files:
        if fname.endswith(".json"):
            try:
                idx = int(fname[:-5])
                with open(d + "/" + fname) as fp:
                    data = json.load(fp)
                result.append((idx, data.get("name", "Patch %d" % idx)))
            except (OSError, ValueError):
                continue
    result.sort()
    return result


def count_user_patches(inst_name):
    return len(list_user_patches(inst_name))
# 48 PPQ / 120 BPM advances 96 ticks/sec, so 1 tick ~= 10ms -> ~100Hz control
# poll.
#
# How many poll exceptions to print in full before going quiet. The poll runs at
# ~30Hz, so an unthrottled traceback would itself starve the REPL.
_MAX_POLL_FAULTS = 3
_poll_faults = 0

# Redraw-on-change. The panel used to repaint unconditionally every 3rd poll
# (~10Hz), re-sending an identical 1024-byte frame over I2C 10x/sec (10KB/s) plus
# a full buffer clear+redraw — wasted bus time, CPU and OLED power during steady
# play. Anything that changes what should be on screen calls mark_dirty(); the
# poll then paints once on the next tick and otherwise does nothing.
_dirty = True
# Even with redraw-on-change, a few things move on their own: the battery
# readout drifts, and it is the only such value in the header. Repaint on a slow
# timer so it stays honest without the old 10Hz churn.
_REFRESH_MS = 2000       # full repaint at least this often (0 disables)
_last_paint_ms = 0

_engine = None          # the loaded instrument engine (cynthia_instrument())
_sketch_mod = None      # the imported sketch module
_seq = None             # TulipSequence driving _poll
_name = None            # instrument folder name
_tick = 0
_running = False
_patch_provider = None  # §12 provider (engine or sketch-supplied)
_menu_active = False
_menu_row = 0
_menu_col = 0
_menu_insts = []          # folder slugs (what play() takes)
_menu_labels = []         # friendly names for display (meta.json)
_menu_last_sel = 0
# The menu is 2 rows x 3 cols of instruments, with a full-width Settings bar as
# ROW 2. Settings gets its own row rather than "one more slot": as a slot it
# would only be reachable while the instrument list was short (slot N lands in
# row 2 of the grid, and rows above 1 were not selectable), so a full grid made
# the Settings app unreachable. A dedicated row is reachable at any count.
SETTINGS_SLOT = "settings"
SETTINGS_ROW = 2
_menu_includes_settings = True
_name_label = None   # friendly display name for the header (meta.json)

def _menu_selected_index():
    """Index into _menu_insts for the cursor's cell, or None if the cell is empty.

    The grid is a fixed 2x3, but the library is usually smaller, so most cells
    are empty. Without this, pressing on an empty cell did nothing at all and the
    menu looked broken. The Settings bar sits at index len(_menu_insts) and is
    returned as SETTINGS_SLOT instead of an instrument index.
    """
    if _menu_includes_settings and _menu_row == SETTINGS_ROW:
        return SETTINGS_SLOT
    idx = _menu_row * 3 + _menu_col
    return idx if idx < len(_menu_insts) else None


def _menu_slot_count():
    """Selectable instrument slots (the Settings bar is its own row)."""
    return len(_menu_insts)


def _menu_cursor_slot():
    """Cursor position as an instrument slot index, or len(_menu_insts) on Settings."""
    if _menu_includes_settings and _menu_row == SETTINGS_ROW:
        return len(_menu_insts)
    return _menu_row * 3 + _menu_col


def _menu_max_row():
    return SETTINGS_ROW if _menu_includes_settings else 1


def _menu_cursor_to_last():
    """Clamp the cursor to a real slot (after the list changes)."""
    global _menu_row, _menu_col
    n = len(_menu_insts)
    if n <= 0:
        # No instruments: land on Settings if it exists, else the first cell.
        _menu_row = SETTINGS_ROW if _menu_includes_settings else 0
        _menu_col = 0
        return
    # _menu_last_sel is either an instrument index or SETTINGS_SLOT, so that
    # reopening the menu after using Settings returns you to Settings rather
    # than snapping to the last instrument.
    if _menu_last_sel == SETTINGS_SLOT and _menu_includes_settings:
        _menu_row = SETTINGS_ROW
        _menu_col = 0
        return
    idxi = _menu_last_sel if isinstance(_menu_last_sel, int) else 0
    idx = min(max(idxi, 0), n - 1)
    if idx >= 3:
        _menu_row = 1
        _menu_col = idx - 3
    else:
        _menu_row = 0
        _menu_col = idx


def _menu_slot_is_settings(idx):
    return _menu_includes_settings and idx == len(_menu_insts)


def open_menu():
    global _menu_active, _menu_row, _menu_col, _menu_insts, _menu_labels
    from . import browser
    _menu_insts = browser.list_instruments()
    # The grid draws display names; selection still uses the slug.
    # (friendly, slug) pairs: the grid shows the friendly name and falls back
    # to the slug only if the friendly name cannot fit the cell.
    _menu_labels = [(browser._meta_name(n), n) for n in _menu_insts]
    _menu_active = True
    _joy_reset()
    # Return the cursor to where it was last time, so re-opening the menu after
    # an instrument switch doesn't dump you back at the top-left cell.
    global _menu_last_sel
    if _menu_last_sel in (0, None) and _name and _menu_last_sel != SETTINGS_SLOT:
        try:
            _menu_last_sel = _menu_insts.index(_name)
        except ValueError:
            _menu_last_sel = 0
    _menu_cursor_to_last()
    _draw_ui(force=True)

def close_menu():
    global _menu_active
    _menu_active = False
    _joy_reset()
    _draw_ui(force=True)

def is_menu_active():
    return _menu_active


def _current_sketch():
    """Return the already-imported `sketch` module.

    run_sketch() imports /user/current/sketch.py as module `sketch` and runs
    its top-level code (which creates + starts the instrument engine). We must
    NOT re-import it here — that would re-run the top level and build a second
    engine. Just hand back the loaded module.
    """
    return sys.modules.get("sketch")


def play(name):
    """Load instrument folder `name` from /user/instruments/ and run it.

    This is the BROWSER -> PLAY transition (§17): copy the sketch, run it via
    amyboard.run_sketch(), then attach the engine for surface routing.
    """
    global _engine, _sketch_mod, _name, _name_label, _running, _patch_provider, _tick

    from . import browser
    if name not in browser.list_instruments():
        print("cynthia: no instrument %r. Available:" % name)
        for n in browser.list_instruments():
            print("   ", n)
        return False

    stop()

    print("cynthia: loading %s..." % name)
    # Copy sketch.py into /user/current/ and remember the selection (§17).
    browser._copy_sketch(name)
    with open(browser.LAST_INSTRUMENT, "w") as f:
        f.write(name)

    # Run the sketch: applies _auto_generated_knobs, imports it, starts loop().
    # All-notes-off first so the previous instrument isn't left droning.
    import amyboard
    import amy
    amy.send(reset=amy.RESET_ALL_NOTES)
    amyboard.run_sketch()

    # Grab the engine + patch provider the sketch exposes (duck-typed hooks).
    _sketch_mod = _current_sketch()
    _engine = None
    _patch_provider = None
    if _sketch_mod is not None:
        if hasattr(_sketch_mod, "cynthia_instrument"):
            try:
                _engine = _sketch_mod.cynthia_instrument()
            except Exception as e:
                print("cynthia: cynthia_instrument() failed:", e)
        if hasattr(_sketch_mod, "cynthia_patches"):
            try:
                _patch_provider = _sketch_mod.cynthia_patches()
            except Exception:
                pass
    # An engine that implements the PatchProvider protocol is its own provider.
    if _patch_provider is None and _engine is not None and hasattr(_engine, "count"):
        _patch_provider = _engine

    _name = name
    try:
        from . import browser as _br
        _name_label = _br._meta_name(name)
    except Exception:
        _name_label = name
    _running = True
    _tick = 0
    surface.start()
    _start_poll()

    pname = current_patch_name()
    print("cynthia: %s ready%s" % (name, (" — patch: " + pname) if pname else ""))
    _draw_ui(force=True)
    return True


def restore_last():
    """Boot-time restore (§17): run the last-selected instrument if present."""
    from . import browser
    name = browser.last_instrument()
    if name:
        return play(name)
    return False


def attach_current(name):
    """Attach the runtime to the sketch that start_amy() already ran at boot.

    At boot, _boot.py copies the last instrument's sketch.py into /user/current/
    and amyboard.start_amy() -> run_sketch() imports it — which runs the sketch's
    top level, creating and starting its engine. This function does NOT re-run the
    sketch; it just grabs the already-imported engine and starts the surface poll
    loop so pots/buttons/display are routed. Call after registry.start().
    """
    global _engine, _sketch_mod, _name, _running, _patch_provider, _tick

    _sketch_mod = _current_sketch()
    if _sketch_mod is None:
        return False
    _engine = None
    _patch_provider = None
    if hasattr(_sketch_mod, "cynthia_instrument"):
        try:
            _engine = _sketch_mod.cynthia_instrument()
        except Exception as e:
            print("cynthia: cynthia_instrument() failed:", e)
    if hasattr(_sketch_mod, "cynthia_patches"):
        try:
            _patch_provider = _sketch_mod.cynthia_patches()
        except Exception:
            pass
    if _patch_provider is None and _engine is not None and hasattr(_engine, "count"):
        _patch_provider = _engine

    _name = name
    _running = True
    _tick = 0
    surface.start()
    _start_poll()
    if name is None:
        print("cynthia: no instrument attached (no engine; UI stays generic)")
        return False
    print("cynthia: attached to %s%s" % (name, (" — patch: " + current_patch_name()) if current_patch_name() else ""))
    _draw_ui(force=True)
    return True


def stop():
    """Stop the poll loop and the running sketch (PLAY -> BROWSER)."""
    global _engine, _sketch_mod, _seq, _running, _name, _patch_provider
    _running = False
    # Disarm BEFORE clearing the sequence: with the poll gone there is nothing to
    # feed the watchdog, and a legitimately stopped poll must not look like a
    # hung one (that would reboot the board every time the user left PLAY).
    _disarm_watchdog()
    if _seq is not None:
        _seq.clear()
        _seq = None
    if _engine is not None and hasattr(_engine, "stop"):
        try:
            _engine.stop()
        except Exception:
            pass
    import amyboard
    amyboard.stop_sketch()
    midi.all_notes_off()
    _engine = None
    _sketch_mod = None
    _patch_provider = None
    _name = None


def current():
    """Name of the loaded instrument, or None."""
    return _name if _running else None


def engine():
    """The loaded instrument engine (or None)."""
    return _engine


# --- Patch navigation (§12) ---

def _provider():
    return _patch_provider


def current_patch_name():
    p = _provider()
    if p is not None and hasattr(p, "name") and hasattr(p, "current"):
        try:
            return p.name(p.current())
        except Exception:
            pass
    if _engine is not None and hasattr(_engine, "get_patch_name"):
        return _engine.get_patch_name()
    return None


def set_patch(idx):
    p = _provider()
    if p is not None and hasattr(p, "select"):
        p.select(idx)
    elif _engine is not None and hasattr(_engine, "set_patch"):
        _engine.set_patch(idx)
    _draw_ui(force=True)


def next_patch():
    p = _provider()
    if p is not None and hasattr(p, "current") and hasattr(p, "select") and hasattr(p, "count"):
        p.select((p.current() + 1) % p.count())
    elif _engine is not None and hasattr(_engine, "set_patch") and hasattr(_engine, "patch_index"):
        _engine.set_patch(_engine.patch_index + 1)
    _draw_ui(force=True)
    return current_patch_name()


def prev_patch():
    p = _provider()
    if p is not None and hasattr(p, "current") and hasattr(p, "select") and hasattr(p, "count"):
        p.select((p.current() - 1) % p.count())
    elif _engine is not None and hasattr(_engine, "set_patch") and hasattr(_engine, "patch_index"):
        _engine.set_patch(_engine.patch_index - 1)
    _draw_ui(force=True)
    return current_patch_name()


# --- Poll loop (§8: paced via TulipSequence, never busy-loop) ---

def _disarm_watchdog():
    """Stop the hardware watchdog from rebooting us. Called wherever the poll is
    intentionally stopped (stop(), and the calibration loop), because "no poll"
    is otherwise indistinguishable from "hung sketch"."""
    if hasattr(tulip, "cynthia_wdt_disarm"):
        try:
            tulip.cynthia_wdt_disarm()
        except Exception:
            pass


def _arm_watchdog():
    """Arm the hardware watchdog (idempotent)."""
    if hasattr(tulip, "cynthia_wdt_arm"):
        try:
            tulip.cynthia_wdt_arm()
        except Exception as e:
            print("cynthia: could not arm watchdog:", e)


def _start_poll():
    global _seq, _stable_after
    import sequencer
    # TulipSequence's "divider" is a note division: divider=1 is a WHOLE note
    # (4 beats = 2s at 120 BPM) — that made the surface poll run once every
    # two seconds. For a 1-tick period (~10ms at 120 BPM / 48 PPQ, ~96Hz)
    # the divider must be 4*PPQ. Keys, knobs, buttons and display all ride
    # this poll (single-threaded: they share one I2C bus).
    _seq = sequencer.TulipSequence(4 * sequencer.PPQ, _poll)
    # Arm the "this boot is stable" timer (see _poll / cynthia.guard).
    _stable_after = _tick + _STABLE_POLLS
    # Arm the hardware-side watchdog. This is the mechanism that recovers the
    # board from a sketch whose loop() never returns: the poll below stops
    # running, the C side sees no feed and reboots. Armed here (not at boot)
    # because only now is the poll actually running — see cynthia_wdt.h.
    _arm_watchdog()


def _poll(tick):
    """One UI poll. MUST NOT raise: a scheduled callback that throws is swallowed
    by the VM, which silently kills every later poll — keys keep playing (they are
    C-side) while buttons, knobs, joystick and display go dead until reboot. So
    the whole body is guarded, and the traceback is printed a bounded number of
    times so a recurring fault cannot flood the console and starve the REPL."""
    global _tick, _poll_faults
    if not _running or _STOP_POLL:
        return
    # Feed the hardware watchdog first: the whole point is that this callback is
    # the liveness signal a stuck sketch cannot provide. Put before anything that
    # could itself be slow, so a heavy poll still counts as alive — and before the
    # try/except, so a fault in the body does not also stop the feed.
    if hasattr(tulip, "cynthia_wdt_feed"):
        tulip.cynthia_wdt_feed()
    try:
        _poll_body(tick)
    except Exception as e:
        # Report the first N faults with a full traceback (so the cause is
        # visible), then stay quiet: the poll runs at ~30Hz, so an unthrottled
        # print here would itself starve the REPL.
        _poll_faults += 1
        if _poll_faults <= _MAX_POLL_FAULTS:
            print("cynthia: poll error #%d:" % _poll_faults)
            try:
                sys.print_exception(e)
            except Exception:
                print("  ", type(e).__name__, e)
            if _poll_faults == _MAX_POLL_FAULTS:
                print("cynthia: further poll errors will be suppressed")
        return


def _poll_body(tick):
    """The actual poll work, split out so _poll can guard it as a unit."""
    global _tick, _stable_after, _last_knob_ch, _last_knob_time, _dirty
    now = time.ticks_ms()
    surface.poll(now)

    # Route pot movements to the engine (§9.2 -> instrument params).
    if _engine is not None and hasattr(_engine, "update_pot"):
        for ch, v in surface.knob_changes():
            _engine.update_pot(ch, v)
            # Record for the param-name overlay.
            _last_knob_ch = ch
            _last_knob_time = now
            mark_dirty()

    # Joystick → MIDI pitch bend (X axis) + modulation (Y axis), C++ style.
    joy = surface.joystick()
    if joy is not None:
        jx, jy, j_btn = joy
        # `midi` is already imported at module scope; re-importing it here ran a
        # sys.modules lookup 30x/sec for nothing.
        midi.pitch_bend((jx - 0.5) * 2.0)
        midi.cc(1, jy * 127)

    # Joystick menu navigation (C++ System.cpp handleInstrumentMenu).
    if _menu_active:
        if joy is not None:
            _handle_joystick_menu(jx, jy)
    elif _patch_list_active:
        if joy is not None:
            _handle_joystick_patch_list(jx, jy)
    elif _save_menu_active:
        if joy is not None:
            _handle_joystick_save_menu(jx, jy)
    elif _settings_active:
        if joy is not None:
            _handle_joystick_settings(jx, jy)

    # Hold Octave- and Octave+ together for 5s to calibrate. Read as a HELD
    # mask rather than button events: events only report short/long presses,
    # so a two-button chord has no event of its own.
    _check_calibration_chord(now)

    # Route control-button events.
    for ev in surface.events():
        _handle_event(ev)

    # Proclaim the boot stable once the poll has run for a while without the
    # board dying. This is what resets the boot guard's post-boot (watchdog)
    # counter: a sketch that completes boot and *then* hangs every time will
    # never get here, so the counter accumulates and safe mode eventually trips
    # (see cynthia/guard.py note_wdt_boot). An occasional one-off reset never
    # accumulates, because any boot that survives this long clears it.
    if _stable_after is not None and _tick >= _stable_after:
        _stable_after = None
        try:
            from . import guard
            guard.clear_wdt()
        except Exception:
            pass

    # The knob overlay auto-expires after 500ms; when it does, the screen must
    # repaint back to the normal header even though no event arrived.
    if _last_knob_ch >= 0 and time.ticks_diff(now, _last_knob_time) >= 500:
        _last_knob_ch = -1
        mark_dirty()

    # Paint when something changed, or on a slow timer for values that drift on
    # their own (the battery readout). Replaces the old unconditional repaint
    # every 3rd poll, which resent an identical 1024-byte frame ~10x/sec.
    _tick += 1
    if _dirty or (_REFRESH_MS and time.ticks_diff(now, _last_paint_ms) >= _REFRESH_MS):
        _draw_ui()

_last_joy = None
_last_knob_ch = -1
_last_knob_time = 0

# --- joystick nav: edge-triggered stepping -------------------------------
# The original C++ mapped the stick's POSITION to a menu row (top third = row 0,
# bottom third = row 1). That works for two rows because releasing the stick
# recentres it and re-selects the same row. With three selectable rows it breaks
# completely: the middle band forced row 1 on every poll, so the cursor snapped
# back to row 1 whenever the stick was released and rows 0/2 were unreachable
# without holding.
#
# So navigation is STEP-based: a deflection produces one step, and further steps
# come from auto-repeat while held. A latch prevents a held stick from stepping
# on every poll, and the stick must return near centre to re-arm.
_JOY_DEAD = 0.22        # deflection from centre needed to engage
_JOY_RELEASE = 0.10     # must come back inside this to re-arm
_JOY_REPEAT_MS = 280    # auto-repeat while held

_joy_dir = [0, 0]       # latched direction per axis (-1, 0, +1)
_joy_next = [0, 0]      # ticks_ms of the next auto-repeat per axis


def _joy_axis(i, v, now, repeat_ms=_JOY_REPEAT_MS):
    """Discrete step for axis `i` from a 0..1 value centred at 0.5.

    Returns -1/0/+1; non-zero only on a fresh deflection or an auto-repeat, so
    callers can treat it as "one move".
    """
    d = 0
    if v > 0.5 + _JOY_DEAD:
        d = 1
    elif v < 0.5 - _JOY_DEAD:
        d = -1
    if d == 0:
        if abs(v - 0.5) <= _JOY_RELEASE:
            _joy_dir[i] = 0
        return 0
    if d != _joy_dir[i]:
        _joy_dir[i] = d
        _joy_next[i] = now + _JOY_REPEAT_MS
        return d
    if repeat_ms and time.ticks_diff(now, _joy_next[i]) >= 0:
        _joy_next[i] = now + repeat_ms
        return d
    return 0


def _joy_reset():
    """Forget latched directions (called when a screen closes)."""
    _joy_dir[0] = _joy_dir[1] = 0


def _handle_joystick_menu(jx, jy):
    """Step the grid cursor: up/down = row, left/right = column."""
    global _menu_row, _menu_col
    now = time.ticks_ms()
    changed = False

    # surface.joystick() reverses Y, so +1 = pushed away = "up".
    dy = _joy_axis(1, jy, now)
    if dy:
        new_row = _menu_row - dy
        if new_row < 0:
            new_row = 0
        elif new_row > _menu_max_row():
            new_row = _menu_max_row()
        if new_row != _menu_row:
            _menu_row = new_row
            changed = True

    dx = _joy_axis(0, jx, now)
    if dx:
        new_col = _menu_col + dx
        if new_col < 0:
            new_col = 0
        elif new_col > 2:
            new_col = 2
        if new_col != _menu_col:
            _menu_col = new_col
            changed = True

    if changed:
        # Never leave the cursor on an empty cell: a 2x3 grid with 2 instruments
        # would otherwise let the joystick park on a blank square where pressing
        # does nothing. Snap to the nearest real instrument (the Settings bar is
        # always real).
        n = len(_menu_insts)
        if _menu_row != SETTINGS_ROW or not _menu_includes_settings:
            idx = _menu_row * 3 + _menu_col
            if n == 0:
                _menu_row = SETTINGS_ROW if _menu_includes_settings else 0
                _menu_col = 0
            elif idx >= n:
                idx = n - 1
                _menu_row = 1 if idx >= 3 else 0
                _menu_col = idx % 3
        _draw_ui(force=True)


def _handle_joystick_patch_list(jx, jy):
    """UP/DOWN: scroll the cursor. LEFT/RIGHT: switch [Default] <-> [User].

    Step-based (see _joy_axis): a deflection is one move, held = auto-repeat.
    """
    global _patch_list_cursor, _patch_list_user, _patch_list_items
    now = time.ticks_ms()
    changed = False

    # Folder switch is edge-only (no repeat): repeating it would flip back and
    # forth while the stick is held, which is never what the user wants.
    dx = _joy_axis(0, jx, now, repeat_ms=0)
    if dx < 0 and _patch_list_user:
        _patch_list_user = False
        _patch_list_cursor = 0
        _rebuild_patch_list()
        changed = True
    elif dx > 0 and not _patch_list_user:
        _patch_list_user = True
        _patch_list_cursor = 0
        _rebuild_patch_list()
        changed = True

    dy = _joy_axis(1, jy, now)
    count = len(_patch_list_items)
    if dy and count > 0:
        new_cur = _patch_list_cursor + dy
        if new_cur < 0:
            new_cur = 0
        elif new_cur > count - 1:
            new_cur = count - 1
        if new_cur != _patch_list_cursor:
            _patch_list_cursor = new_cur
            changed = True

    if changed:
        _draw_ui(force=True)


def _check_calibration_chord(now):
    """Start calibration when Octave- and Octave+ are held together 5s."""
    global _chord_start
    if _STOP_POLL:
        return
    try:
        b = surface.buttons()
    except Exception:
        return
    both = b.get("octave_down") and b.get("octave_up")
    if not both:
        if _chord_start is not None:
            _chord_start = None
            mark_dirty()
        return
    if _chord_start is None:
        _chord_start = now
        return
    held = time.ticks_diff(now, _chord_start)
    if held >= CALIBRATE_CHORD_MS:
        _chord_start = None
        calibrate_ui()


def _handle_event(ev):
    global _menu_active, _menu_row, _menu_col, _menu_insts
    global _patch_list_active, _patch_list_cursor
    global _save_menu_active, _save_menu_save_selected
    if not ev:
        return

    if ev[0] == "button":
        _, idx, kind = ev
        # Long-press on joystick button (idx 4) opens menu (C++ System.cpp)
        if kind == "long_press" and idx == 4:
            # Long joystick press is the universal "back to BROWSER" (§7), so it
            # must also leave the settings app rather than stack menus.
            if _settings_active:
                close_settings()
            elif _menu_active:
                close_menu()
            else:
                open_menu()
            return

        if _menu_active:
            if kind == "short_press" and idx == 4:
                # Joystick button short press = select instrument.
                global _menu_last_sel
                idx_sel = _menu_selected_index()
                if idx_sel is None:
                    # Empty grid cell: say so instead of silently doing nothing.
                    print("cynthia: empty cell (only %d instruments)"
                          % len(_menu_insts))
                elif idx_sel == SETTINGS_SLOT:
                    # Settings is an app at the same level as the instruments.
                    _menu_last_sel = SETTINGS_SLOT
                    _menu_active = False
                    open_settings()
                else:
                    name = _menu_insts[idx_sel]
                    _menu_last_sel = idx_sel
                    print("cynthia: selecting %s..." % name)
                    _menu_active = False
                    play(name)
                return
            if kind == "short_press":
                if idx == BTN_OCTAVE_DOWN:
                    _menu_col = max(0, _menu_col - 1)
                    _draw_ui(force=True)
                elif idx == BTN_OCTAVE_UP:
                    _menu_col = min(2, _menu_col + 1)
                    _draw_ui(force=True)
                elif idx == BTN_PATCH_PREV:
                    _menu_row = max(0, _menu_row - 1)
                    _menu_col = min(_menu_col, 2)
                    _draw_ui(force=True)
                elif idx == BTN_PATCH_NEXT:
                    _menu_row = min(_menu_max_row(), _menu_row + 1)
                    _menu_col = min(_menu_col, 2)
                    _draw_ui(force=True)
            return

        if _save_menu_active:
            if kind == "short_press" and idx == 4:
                # Joystick click confirms (C++ saveMenuReadyToConfirm && joyClicked)
                if _save_menu_save_selected:
                    sname = _engine.get_patch_name() if _engine and hasattr(_engine, "get_patch_name") else None
                    new_idx = _do_save_current_patch()
                    _save_menu_active = False
                    if new_idx is not None:
                        # C++ switches to the User folder showing the new patch.
                        global _patch_list_active, _patch_list_user, _patch_list_cursor
                        _patch_list_active = True
                        _patch_list_user = True
                        _rebuild_patch_list()
                        _patch_list_cursor = len(_patch_list_items) - 1
                        if _patch_list_cursor < 0:
                            _patch_list_cursor = 0
                    _draw_ui(force=True)
                else:
                    _close_save_menu()
                return
            if kind == "short_press" and idx == BTN_PATCH_PREV:
                # C++: short-press button 2 cancels a nav state.
                _close_save_menu()
                return
            return

        if _settings_active:
            if kind == "short_press" and idx == 4:
                # Joystick click: activate "Back" (leave), otherwise cycle the
                # value forward — the same step the joystick's right does, so
                # the panel button can edit too when the stick is centred.
                rows = _settings_rows()
                if 0 <= _settings_row < len(rows):
                    key, skind, _lk = rows[_settings_row]
                    if skind == "back":
                        close_settings()
                    elif skind == "toggle":
                        _settings_step(key, skind, 1)
                        _draw_ui(force=True)
                return
            if kind == "short_press" and idx == BTN_PATCH_PREV:
                # Long-press semantics on the hardware buttons are not needed
                # here; a short press on "prev" acts as "back".
                close_settings()
                return
            return

        if _patch_list_active:
            if kind == "short_press" and idx == 4:
                # Joystick click = select patch
                _select_current_patch_list_item()
                _patch_list_active = False
                _draw_ui(force=True)
                return
            if kind == "short_press":
                count = len(_patch_list_items)
                if count > 0:
                    if idx == BTN_PATCH_PREV:
                        _patch_list_cursor = max(0, _patch_list_cursor - 1)
                        _draw_ui(force=True)
                    elif idx == BTN_PATCH_NEXT:
                        _patch_list_cursor = min(count - 1, _patch_list_cursor + 1)
                        _draw_ui(force=True)
            return

        # Not in menu: route button to normal functions.
        # C++ System.cpp: long-press btn2 (prev) → SAVE menu; long-press btn3
        # (next) → patch LIST. Short press keeps normal patch navigation.
        if kind == "long_press":
            if idx == BTN_PATCH_PREV:
                # The engine must be able to describe its state for a save to
                # mean anything. Gate on that, but SAY SO when it cannot: a
                # silent no-op is indistinguishable from a dead button (which
                # is exactly how the DX7's missing save_patch_state presented).
                if _engine is None:
                    print("cynthia: no instrument loaded — nothing to save")
                elif not hasattr(_engine, "save_patch_state"):
                    print("cynthia: %s cannot save patches" % type(_engine).__name__)
                else:
                    _open_save_menu()
            elif idx == BTN_PATCH_NEXT:
                _toggle_patch_list()
            return

        if kind != "short_press":
            return
        if idx == BTN_OCTAVE_DOWN:
            midi.octave_shift(midi.octave_shift() - 1)
            print("cynthia: octave", midi.octave_shift())
            _draw_ui(force=True)
        elif idx == BTN_OCTAVE_UP:
            midi.octave_shift(midi.octave_shift() + 1)
            print("cynthia: octave", midi.octave_shift())
            _draw_ui(force=True)
        elif idx == BTN_PATCH_PREV:
            print("cynthia: patch", prev_patch())
        elif idx == BTN_PATCH_NEXT:
            print("cynthia: patch", next_patch())



# --- Settings app (§17) ---------------------------------------------------
# A peer of the instruments in the menu: same cursor space, its own screen.
# Every row edits one key in /user/settings.json (cynthia/settings.py). Changes
# apply immediately where that is safe; the BLE enable takes effect at the next
# boot because bringing the stack up mid-flight would need a reboot of the
# radio, and a stalled advertisement is worse than a deferred one.

_settings_active = False
_settings_row = 0
_settings_scroll = 0


def _settings_rows():
    """The rows the settings screen shows, in display order.

    (key, kind) where kind says how to render and step the value:
      "choice"  - cycles through `choices`
      "int"     - stepped numerically within [lo, hi]
      "toggle"  - Off/On
      "back"    - leaves the screen
    """
    # (settings.json key, kind, i18n label key) — the three differ for
    # "locale" (stored key) vs "language" (label), so keep them explicit.
    return [
        ("locale", "choice", "language"),
        ("midi_channel", "int", "midi_channel"),
        ("velocity", "int", "velocity"),
        ("octave", "int", "octave"),
        ("ble_midi_enabled", "toggle", "ble_midi"),
        ("ble_name", "choice", "ble_name"),
        ("ble_status", "info", "ble_status"),
        ("calibrate", "action", "calibrate"),
        ("back", "back", "back"),
    ]


# How each row is presented and what its value cycles through.
_SETTINGS_CHOICES = {
    "locale": ("en", "ru"),
    "ble_name": ("CYNTHIA", "CYNTHIA-1", "MIDI"),
}
_SETTINGS_RANGE = {
    "midi_channel": (0, 15),
    "velocity": (1, 127),
    "octave": (-4, 4),
}


def open_settings():
    """Enter the settings app (BROWSER -> SETTINGS)."""
    global _settings_active, _settings_row, _settings_scroll
    from . import settings as _settings
    _settings.reload()          # pick up hand edits made over the REPL
    _settings_active = True
    _joy_reset()
    _settings_row = 0
    _settings_scroll = 0
    _draw_ui(force=True)


def close_settings():
    global _settings_active
    _settings_active = False
    _joy_reset()
    _draw_ui(force=True)


def is_settings_active():
    return _settings_active


def settings_value_text(key, value):
    """Render one setting's value for the screen."""
    if key == "ble_status":
        # Read-only row: shows what the radio is actually doing, so a silent
        # pairing failure is visible instead of looking like a dead toggle.
        from . import ble_midi as _ble
        st = _ble.status()
        if not _ble.is_available():
            return "n/a"
        if not st["enabled"]:
            return t("ble_off")
        if st["connected"]:
            return t("connected")
        if st["error"]:
            # Surface the real reason: "Advertising" while the radio rejected
            # the advertisement is what made this look like it worked.
            return t("ble_error")
        return t("advertising")
    if key == "locale":
        return "Русский" if value == "ru" else "English"
    if key == "ble_midi_enabled":
        return t("ble_on") if value else t("ble_off")
    if key == "octave":
        return "%+d" % int(value)
    return str(value)


def _settings_label(label_key):
    return t(label_key)


def _settings_step(key, kind, delta):
    """Advance one setting by `delta` and persist it."""
    from . import settings as _settings
    cur = _settings.get(key)
    if kind == "info":
        return
    if kind == "action":
        if key == "calibrate":
            # Leave the settings screen first: calibration owns the display and
            # must not have a menu repaint fighting it.
            close_settings()
            calibrate_ui()
        return
    if kind == "toggle":
        new = not bool(cur)
    elif kind == "choice":
        opts = _SETTINGS_CHOICES.get(key, ())
        if not opts:
            return
        try:
            i = opts.index(cur)
        except ValueError:
            i = 0
        new = opts[(i + delta) % len(opts)]
    elif kind == "int":
        lo, hi = _SETTINGS_RANGE.get(key, (0, 127))
        new = min(hi, max(lo, int(cur) + delta))
    else:
        return
    _settings.set(key, new)
    # Apply to live routing immediately: these three are read on every key/CC
    # send, so the change is audible without a reboot.
    if key == "locale":
        i18n.locale(new)
    elif key == "midi_channel":
        midi.channel(int(new))
    elif key == "velocity":
        midi.velocity(int(new))
    elif key == "octave":
        midi.octave_shift(int(new))
    elif key == "ble_midi_enabled":
        # Turning BLE on/off live is safe: start() only advertises and taps the
        # MIDI bus, and stop() unhooks the tap. The radio stack itself is
        # compiled in at boot, which is the part that needs a reboot (to change
        # that you would reflash), not the advertising.
        from . import ble_midi as _ble
        if new:
            if _ble.start(name=_settings.get("ble_name")):
                print("cynthia: BLE MIDI enabled")
            else:
                print("cynthia: BLE MIDI failed:", _ble.status()["error"])
        else:
            _ble.stop()
            print("cynthia: BLE MIDI disabled")
    print("cynthia: %s = %s" % (key, new))


def _draw_settings_ui(disp):
    """Settings list: one row highlighted, value right-aligned, scroll arrows."""
    rows = _settings_rows()
    title = t("settings_title")
    tw = disp.str_width(display.FONT_5X7, title)
    disp.draw_str(display.FONT_5X7, max(0, (128 - tw) // 2), 16, title, 1)
    disp.hline(0, 19, 128, 1)

    line_h = 8
    visible = 5
    start_y = 28
    # Keep the highlighted row on screen.
    global _settings_scroll
    if _settings_row < _settings_scroll:
        _settings_scroll = _settings_row
    elif _settings_row >= _settings_scroll + visible:
        _settings_scroll = _settings_row - visible + 1

    from . import settings as _settings
    for i in range(visible):
        ri = _settings_scroll + i
        if ri >= len(rows):
            break
        key, kind, label_key = rows[ri]
        y = start_y + i * line_h
        sel = (ri == _settings_row)

        label = disp.fit_text(display.FONT_5X7, _settings_label(label_key), 64)
        if kind in ("back", "action"):
            val = ""
        else:
            val = settings_value_text(key, _settings.get(key))
            val = disp.fit_text(display.FONT_5X7, val, 52)
        vx = 126 - disp.str_width(display.FONT_5X7, val) if val else 126

        if sel:
            disp.fill_rect(0, y - 6, 128, line_h, 1)
            disp.draw_str(display.FONT_5X7, 2, y, label, 0)
            if val:
                disp.draw_str(display.FONT_5X7, vx, y, val, 0)
        else:
            disp.draw_str(display.FONT_5X7, 2, y, label, 1)
            if val:
                disp.draw_str(display.FONT_5X7, vx, y, val, 1)

    # Scroll indicators: something above / below the visible window.
    if _settings_scroll > 0:
        disp.draw_str(display.FONT_4X6, 120, 24, "^", 1)
    if _settings_scroll + visible < len(rows):
        disp.draw_str(display.FONT_4X6, 120, 63, "v", 1)


def _handle_joystick_settings(jx, jy):
    """Vertical = move between rows; horizontal = step the value."""
    global _settings_row
    now = time.ticks_ms()
    rows = _settings_rows()
    changed = False

    dy = _joy_axis(1, jy, now)
    if dy:
        new_row = _settings_row - dy
        if new_row < 0:
            new_row = 0
        elif new_row > len(rows) - 1:
            new_row = len(rows) - 1
        if new_row != _settings_row:
            _settings_row = new_row
            changed = True

    # Horizontal repeats, so holding the stick sweeps a numeric value. A
    # choice row cycling on repeat is a bit eager but harmless (the user sees
    # the value change); "action"/"back" rows ignore it in _settings_step.
    dx = _joy_axis(0, jx, now)
    if dx and 0 <= _settings_row < len(rows):
        key, kind, _lk = rows[_settings_row]
        if kind != "back":
            _settings_step(key, kind, dx)
            changed = True

    if changed:
        _draw_ui(force=True)


# --- Patch save/load UI helpers ---

_patch_list_active = False
_patch_list_cursor = 0
_patch_list_user = False       # False = ROM patches, True = user patches
_patch_list_items = []          # [(idx, name), ...]
_save_menu_active = False
_save_menu_save_selected = True  # True = Save highlighted, False = Cancel


def _open_save_menu():
    """C++ System.cpp: long-press button 2 in PATCH_NAV_NORMAL enters NAV_SAVE."""
    global _save_menu_active, _save_menu_save_selected
    _save_menu_active = True
    _joy_reset()
    _save_menu_save_selected = True   # C++ defaults to Save selected
    _draw_ui(force=True)


def _close_save_menu():
    global _save_menu_active
    _save_menu_active = False
    _joy_reset()
    _draw_ui(force=True)


def _toggle_patch_list():
    """Toggle patch-list overlay (C++ PATCH_NAV_LIST mode)."""
    global _patch_list_active, _patch_list_cursor, _patch_list_user, _patch_list_items
    _patch_list_active = not _patch_list_active
    _joy_reset()
    if _patch_list_active:
        # C++ seeds the cursor from the active patch index / folder.
        _patch_list_user = False
        _patch_list_cursor = _engine.current() if _engine and hasattr(_engine, "current") else 0
        _rebuild_patch_list()
        if _patch_list_cursor >= len(_patch_list_items):
            _patch_list_cursor = 0
    _draw_ui(force=True)


def _rebuild_patch_list():
    global _patch_list_items, _patch_list_user
    import os
    if _patch_list_user:
        _patch_list_items = list_user_patches(_name) if _name else []
    else:
        n = _engine.count() if _engine and hasattr(_engine, "count") else 0
        _patch_list_items = [(i, _engine.name(i)) for i in range(n)] if _engine else []


def _do_save_current_patch():
    """Save the current instrument patch as a new user patch slot."""
    if _engine is None or _name is None:
        return None
    idx = count_user_patches(_name)
    state = _engine.save_patch_state()
    pname = _engine.get_patch_name() if hasattr(_engine, "get_patch_name") else "Patch %d" % idx
    if save_patch(_name, idx, pname, state):
        print("cynthia: patch saved as %s/%d.json (%s)" % (_name, idx, pname))
        return idx
    print("cynthia: save failed")
    return None


def _draw_save_menu_ui(disp):
    """Draw SAVE PATCH / Save / Cancel, matching C++ drawSaveMenu()."""
    title = t("save_patch")
    title_w = disp.str_width(display.FONT_5X7, title)
    disp.draw_str(display.FONT_5X7, max(0, (128 - title_w) // 2), 18, title, 1)

    cur = _engine.get_patch_name() if _engine and hasattr(_engine, "get_patch_name") else "---"
    # The patch name itself is user content: never translated, only the label.
    disp.draw_str(display.FONT_5X7, 2, 28, t("patch_name") + ": " + cur, 1)

    btn_y, btn_w, btn_h = 40, 40, 14
    btn_gap = (128 - 2 * btn_w) // 3

    # Save button. Translated labels vary in width ("Сохранить" is much wider
    # than "Save"), so measure the ACTIVE label and clip it to the button —
    # otherwise a longer translation overflows a 40px box.
    save_lbl = disp.fit_text(display.FONT_5X7, t("save"), btn_w - 2)
    save_x = btn_gap
    save_tx = save_x + (btn_w - disp.str_width(display.FONT_5X7, save_lbl)) // 2
    if _save_menu_save_selected:
        disp.fill_rect(save_x, btn_y, btn_w, btn_h, 1)
        disp.draw_str(display.FONT_5X7, save_tx, btn_y + 11, save_lbl, 0)
    else:
        disp.rect(save_x, btn_y, btn_w, btn_h, 1)
        disp.draw_str(display.FONT_5X7, save_tx, btn_y + 11, save_lbl, 1)

    # Cancel button
    cancel_lbl = disp.fit_text(display.FONT_5X7, t("cancel"), btn_w - 2)
    cancel_x = 2 * btn_gap + btn_w
    cancel_tx = cancel_x + (btn_w - disp.str_width(display.FONT_5X7, cancel_lbl)) // 2
    if not _save_menu_save_selected:
        disp.fill_rect(cancel_x, btn_y, btn_w, btn_h, 1)
        disp.draw_str(display.FONT_5X7, cancel_tx, btn_y + 11, cancel_lbl, 0)
    else:
        disp.rect(cancel_x, btn_y, btn_w, btn_h, 1)
        disp.draw_str(display.FONT_5X7, cancel_tx, btn_y + 11, cancel_lbl, 1)


def _handle_joystick_save_menu(jx, jy):
    """LEFT selects Save, RIGHT selects Cancel (edge-triggered)."""
    global _save_menu_save_selected
    now = time.ticks_ms()
    dx = _joy_axis(0, jx, now, repeat_ms=0)
    if dx < 0 and not _save_menu_save_selected:
        _save_menu_save_selected = True
        _draw_ui(force=True)
    elif dx > 0 and _save_menu_save_selected:
        _save_menu_save_selected = False
        _draw_ui(force=True)


def _select_current_patch_list_item():
    """Load the patch under the patch list cursor."""
    if not _patch_list_items or _patch_list_cursor >= len(_patch_list_items):
        return
    idx, name = _patch_list_items[_patch_list_cursor]
    if _patch_list_user:
        if _name and _engine and hasattr(_engine, "load_patch_state"):
            data = load_patch(_name, idx)
            if data and "state" in data:
                _engine.load_patch_state(data["state"])
                print("cynthia: loaded user patch %d (%s)" % (idx, name))
    else:
        set_patch(idx)
        print("cynthia: loaded default patch %d (%s)" % (idx, name))


def _draw_patch_list_ui(disp):
    """Draw patch list view with 5 visible lines, matching C++ drawPatchList()."""
    # Folder labels are chrome -> translated. The patch names beside them are
    # user/ROM content -> never translated.
    folder_label = t("user") if _patch_list_user else t("default")
    folder_w = disp.str_width(display.FONT_5X7, folder_label)
    disp.draw_str(display.FONT_5X7, max(0, (128 - folder_w) // 2), 18, folder_label, 1)

    count = len(_patch_list_items)
    if count == 0:
        disp.draw_str(display.FONT_5X7, 30, 35, t("empty"), 1)
        return

    visible_lines = 5
    line_h = 8
    start_y = 26

    scroll_offset = 0
    if _patch_list_cursor >= visible_lines:
        scroll_offset = _patch_list_cursor - visible_lines + 1

    for i in range(visible_lines):
        item_idx = scroll_offset + i
        if item_idx >= count:
            break
        y_pos = start_y + i * line_h
        is_cur = (item_idx == _patch_list_cursor)

        idx_val, patch_name = _patch_list_items[item_idx]
        line_str = "%3d %s" % (idx_val + 1 if not _patch_list_user else idx_val, patch_name)
        # Patch names are user-typed, so they can be arbitrarily long; at x=2 the
        # usable width is 124px and a long name ("255 Very Long Patch Name Here"
        # = 144px) ran off the panel. Trim to fit instead of clipping mid-glyph.
        line_str = disp.fit_text(display.FONT_5X7, line_str, 124)

        if is_cur:
            disp.fill_rect(0, y_pos - 6, 128, line_h, 1)
            disp.draw_str(display.FONT_5X7, 2, y_pos, line_str, 0)
        else:
            disp.draw_str(display.FONT_5X7, 2, y_pos, line_str, 1)

    # Scrollbar on right edge
    if count > visible_lines:
        bar_h = max(4, (visible_lines * line_h * visible_lines) // count)
        bar_y = start_y + (scroll_offset * (visible_lines * line_h - bar_h)) // (count - visible_lines)
        disp.fill_rect(125, bar_y, 3, bar_h, 1)



def mark_dirty():
    """Request a repaint on the next poll. Call whenever state the UI shows has
    changed (instrument, patch, menu movement, a knob whose value is on screen,
    octave, mode). Cheap and idempotent."""
    global _dirty
    _dirty = True


def _draw_ui(force=False):
    """Repaint if something changed (or `force`). Returns True if it painted."""
    global _dirty, _last_paint_ms
    if not force and not _dirty:
        return False
    _dirty = False
    _last_paint_ms = time.ticks_ms()
    disp = surface.display()
    if disp is None:
        return
    disp.fill(0)

    # Always draw header (C++ _drawHeader before everything).
    # Prefer the instrument's friendly name from meta.json ("Juno-106") over the
    # folder slug ("juno") — the REPL menu already shows the friendly one, so the
    # screen and the console disagreed. Fitted so a long name cannot overrun the
    # header.
    _lbl = _name_label or _name or "CYNTHIA"
    inst_title = _lbl.upper()
    inst_title = disp.fit_text(display.FONT_RESOLEDBOLD, inst_title, 124)
    global _last_joy

    # Check if we should show a knob-change overlay (500ms persist).
    global _last_knob_ch, _last_knob_time
    import time as _time
    show_overlay = (_last_knob_ch >= 0 and _time.ticks_diff(_time.ticks_ms(), _last_knob_time) < 500)

    if show_overlay:
        # Universal param name — engine can override with pot_name().
        if _engine is not None and hasattr(_engine, "pot_name"):
            _pname = _engine.pot_name(_last_knob_ch)
        elif 0 <= _last_knob_ch < len(_POT_NAME_KEYS):
            _pname = t(_POT_NAME_KEYS[_last_knob_ch])
        else:
            _pname = "Ch %d" % _last_knob_ch
        disp.draw_str(display.FONT_5X7, 2, 7, _pname, 1)
        # Value display: knob percentage
        kn = surface.knobs()
        if _last_knob_ch < len(kn):
            pct = int(kn[_last_knob_ch] * 100)
            val_str = "%d%%" % pct
            disp.draw_str(display.FONT_5X7, 100, 7, val_str, 1)
        # Center: instrument name
        text_w = disp.str_width(display.FONT_RESOLEDBOLD, inst_title)
        start_x = max(0, (display.WIDTH - text_w) // 2)
        disp.draw_str(display.FONT_RESOLEDBOLD, start_x, 7, inst_title, 1)
        disp.hline(0, 10, display.WIDTH, 1)
    else:
        # Normal header: battery voltage, not joystick
        batt_v = None
        if hasattr(tulip, "cynthia_battery_voltage"):
            batt_v = tulip.cynthia_battery_voltage()
        disp.draw_header(octave=midi.octave_shift(), inst_name=inst_title,
                         battery_v=batt_v)

    if _menu_active:
        # Only the 2x3 instrument grid; Settings is the separate bar below, so
        # a cursor on the Settings row must not also draw a cell highlight.
        if _menu_row < SETTINGS_ROW:
            disp.draw_instrument_grid(_menu_labels, _menu_row, _menu_col)
        else:
            disp.draw_instrument_grid(_menu_labels, -1, -1)
        disp.draw_app_bar(t("settings"), selected=(_menu_row == SETTINGS_ROW))
        disp.show()
        return
    if _settings_active:
        _draw_settings_ui(disp)
        disp.show()
        return
    if _chord_start is not None:
        # Chord progress: show how much longer to hold, so the 5s gesture is
        # discoverable rather than feeling like nothing happened.
        held = time.ticks_diff(time.ticks_ms(), _chord_start)
        frac = min(1.0, held / float(CALIBRATE_CHORD_MS))
        disp.fill(0)
        tw = disp.str_width(display.FONT_5X7, t("calibrate"))
        disp.draw_str(display.FONT_5X7, max(0, (128 - tw) // 2), 24,
                      t("calibrate"), 1)
        disp.rect(14, 34, 100, 10, 1)
        w = int(96 * frac)
        if w > 0:
            disp.fill_rect(16, 36, w, 6, 1)
        disp.show()
        return

    if _save_menu_active:
        _draw_save_menu_ui(disp)
        disp.show()
        return
    if _patch_list_active:
        _draw_patch_list_ui(disp)
        disp.show()
        return


    if _engine is None or not hasattr(_engine, "draw_ui"):
        return

    # Engine UI below the header (y_offset=10 matches the C++ drawUI(10))
    try:
        _engine.draw_ui(disp, 10)
    except Exception as e:
        print("cynthia: draw_ui failed:", e)
    disp.show()

# --- Joystick calibration with display UI ---

_STOP_POLL = False  # Prevent _poll from running during calibration
# Hold-to-calibrate chord: Octave- + Octave+ together for this long. Stored as
# the ticks_ms() when the chord was first seen, or None when not held.
CALIBRATE_CHORD_MS = 5000
_chord_start = None

# Polls a boot must survive before it counts as stable (see _poll). At ~30Hz that
# is roughly 5 seconds — the same order as the 5s task watchdog timeout, so a
# hang-every-time sketch never reaches it.
_STABLE_POLLS = 150
_stable_after = None

def _calib_screen(disp, title, line1="", line2="", progress=None):
    """One calibration screen. `progress` 0..1 draws a bar instead of line2."""
    if disp is None:
        return
    disp.fill(0)
    tw = disp.str_width(display.FONT_5X7, title)
    disp.draw_str(display.FONT_5X7, max(0, (128 - tw) // 2), 12, title, 1)
    disp.hline(0, 16, 128, 1)
    if line1:
        disp.draw_str(display.FONT_5X7, 2, 30, disp.fit_text(display.FONT_5X7, line1, 124), 1)
    if progress is not None:
        # Progress bar: an outline plus a filled portion, so time-to-finish is
        # visible instead of the user staring at a frozen screen.
        disp.rect(6, 40, 116, 10, 1)
        w = int(112 * max(0.0, min(1.0, progress)))
        if w > 0:
            disp.fill_rect(8, 42, w, 6, 1)
    elif line2:
        disp.draw_str(display.FONT_5X7, 2, 44, disp.fit_text(display.FONT_5X7, line2, 124), 1)
    disp.show()


def _calib_center(disp, seconds=1.5):
    """Sample the at-rest center. Returns (cx, cy) or None if no samples."""
    xs = []
    ys = []
    n = 60
    dt = int(seconds * 1000 / n)
    for i in range(n):
        if i % 5 == 0:
            _calib_screen(disp, t("calibrate"), t("cal_hold_still"),
                          progress=i / float(n))
        joy = surface._raw_joystick()
        if joy:
            x, y, _b = joy
            # 0.0/1.0 means the ADC railed: the stick is being pushed, not at
            # rest. Including those would bias the center outward.
            if 0.001 < x < 0.999 and 0.001 < y < 0.999:
                xs.append(x)
                ys.append(y)
        time.sleep_ms(dt)
    if not xs:
        return None
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _calib_range(disp, cx, cy, seconds=5.0):
    """Sample the full travel while the user sweeps the stick. Returns dict."""
    xs = []
    ys = []
    n = 100
    dt = int(seconds * 1000 / n)
    for i in range(n):
        if i % 5 == 0:
            _calib_screen(disp, t("calibrate"), t("cal_sweep"),
                          progress=i / float(n))
        joy = surface._raw_joystick()
        if joy:
            x, y, _b = joy
            if 0.001 < x < 0.999 and 0.001 < y < 0.999:
                xs.append(x)
                ys.append(y)
        time.sleep_ms(dt)
    lo_x = min(xs) if xs else cx - 0.1
    hi_x = max(xs) if xs else cx + 0.1
    lo_y = min(ys) if ys else cy - 0.1
    hi_y = max(ys) if ys else cy + 0.1
    # Reject a "sweep" that never actually moved: saving it would claim the
    # stick has no travel and the menu/param controls would feel dead. Fall
    # back to a sane default span around the measured center instead.
    if hi_x - lo_x < 0.15:
        lo_x, hi_x = max(0.0, cx - 0.3), min(1.0, cx + 0.3)
    if hi_y - lo_y < 0.15:
        lo_y, hi_y = max(0.0, cy - 0.3), min(1.0, cy + 0.3)
    return {"cx": cx, "cy": cy,
            "min_x": lo_x, "max_x": hi_x,
            "min_y": lo_y, "max_y": hi_y}


def calibrate_ui():
    """Guided joystick calibration, reachable from the settings app or by
    holding Octave- and Octave+ together for 5s.

    Runs on the UI task with the poll stopped, so it draws its own screens.
    Every exit path re-arms the watchdog and repaints, because a calibration
    that dies half way must not leave the board without a liveness signal.
    """
    global _STOP_POLL, _chord_start
    if _STOP_POLL:
        # Already calibrating (e.g. the chord fired twice); do not nest.
        return
    _STOP_POLL = True
    _chord_start = None
    _disarm_watchdog()
    disp = surface.display()
    try:
        print("cynthia: joystick calibration — hold the stick still")
        _calib_screen(disp, t("calibrate"), t("cal_hold_still"), t("cal_start_hint"))
        time.sleep_ms(1200)

        c = _calib_center(disp)
        if c is None:
            _calib_screen(disp, t("calibrate"), t("cal_failed"), t("cal_retry"))
            print("cynthia: calibration failed — no valid samples")
            time.sleep_ms(1800)
            return
        cx, cy = c
        print("cynthia: center cx=%.3f cy=%.3f" % (cx, cy))

        cal = _calib_range(disp, cx, cy)
        if not surface.save_joystick_cal(cal):
            _calib_screen(disp, t("calibrate"), t("cal_save_failed"))
            print("cynthia: calibration could not be saved")
            time.sleep_ms(1800)
            return
        print("cynthia: calibration saved cx=%.3f cy=%.3f x=[%.3f..%.3f] y=[%.3f..%.3f]"
              % (cal["cx"], cal["cy"], cal["min_x"], cal["max_x"],
                 cal["min_y"], cal["max_y"]))

        # Verification: live readout so the user can see the result instead of
        # taking it on faith. Exits on a short press or after 6s.
        _calib_screen(disp, t("calibrate"), t("cal_test"), "")
        end = time.ticks_add(time.ticks_ms(), 6000)
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            joy = surface.joystick()
            if joy is not None:
                jx, jy, jb = joy
                # Draw the calibrated position as a dot in a box.
                disp.fill(0)
                tw = disp.str_width(display.FONT_5X7, t("cal_test"))
                disp.draw_str(display.FONT_5X7, max(0, (128 - tw) // 2), 12,
                              t("cal_test"), 1)
                disp.hline(0, 16, 128, 1)
                disp.rect(24, 22, 80, 40, 1)
                px = 24 + int(jx * 78)
                py = 22 + int((1.0 - jy) * 38)
                disp.fill_rect(px - 1, py - 1, 3, 3, 1)
                disp.show()
            if hasattr(tulip, "cynthia_button_event"):
                ev = tulip.cynthia_button_event()
                if ev and ev[0] == 4:
                    break
            time.sleep_ms(30)
    finally:
        # Restore regardless of how we left: a stuck _STOP_POLL freezes the
        # whole UI (no poll = no knobs, no buttons, no watchdog feed).
        _STOP_POLL = False
        _arm_watchdog()
        mark_dirty()
        _draw_ui(force=True)
