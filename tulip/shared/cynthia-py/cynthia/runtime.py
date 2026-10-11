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

from . import surface, midi, display

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
_POT_NAMES = [
    "Custom 1", "Custom 2", "Custom 3", "Custom 4",
    "Rate", "Depth", "Target", "Cutoff",
    "Resonance", "Attack", "Decay", "Sustain",
    "Release", "Delay", "Delay T", "Glide",
]
# 48 PPQ / 120 BPM advances 96 ticks/sec, so 1 tick ~= 10ms -> ~100Hz control
# poll. The display redraws every _DISPLAY_EVERY polls (~33fps, §8 >=15fps).
_DISPLAY_EVERY = 3

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
_menu_insts = []

def open_menu():
    global _menu_active, _menu_row, _menu_col, _menu_insts
    from . import browser
    _menu_insts = browser.list_instruments()
    _menu_active = True
    _menu_row = 0
    _menu_col = 0
    _draw_ui()

def close_menu():
    global _menu_active
    _menu_active = False
    _draw_ui()

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
    global _engine, _sketch_mod, _name, _running, _patch_provider, _tick

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
    _running = True
    _tick = 0
    surface.start()
    _start_poll()

    pname = current_patch_name()
    print("cynthia: %s ready%s" % (name, (" — patch: " + pname) if pname else ""))
    _draw_ui()
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
    print("cynthia: attached to %s%s" % (name, (" — patch: " + current_patch_name()) if current_patch_name() else ""))
    _draw_ui()
    return True


def stop():
    """Stop the poll loop and the running sketch (PLAY -> BROWSER)."""
    global _engine, _sketch_mod, _seq, _running, _name, _patch_provider
    _running = False
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
    _draw_ui()


def next_patch():
    p = _provider()
    if p is not None and hasattr(p, "current") and hasattr(p, "select") and hasattr(p, "count"):
        p.select((p.current() + 1) % p.count())
    elif _engine is not None and hasattr(_engine, "set_patch") and hasattr(_engine, "patch_index"):
        _engine.set_patch(_engine.patch_index + 1)
    _draw_ui()
    return current_patch_name()


def prev_patch():
    p = _provider()
    if p is not None and hasattr(p, "current") and hasattr(p, "select") and hasattr(p, "count"):
        p.select((p.current() - 1) % p.count())
    elif _engine is not None and hasattr(_engine, "set_patch") and hasattr(_engine, "patch_index"):
        _engine.set_patch(_engine.patch_index - 1)
    _draw_ui()
    return current_patch_name()


# --- Poll loop (§8: paced via TulipSequence, never busy-loop) ---

def _start_poll():
    global _seq
    import sequencer
    # TulipSequence's "divider" is a note division: divider=1 is a WHOLE note
    # (4 beats = 2s at 120 BPM) — that made the surface poll run once every
    # two seconds. For a 1-tick period (~10ms at 120 BPM / 48 PPQ, ~96Hz)
    # the divider must be 4*PPQ. Keys, knobs, buttons and display all ride
    # this poll (single-threaded: they share one I2C bus).
    _seq = sequencer.TulipSequence(4 * sequencer.PPQ, _poll)


def _poll(tick):
    global _tick
    if not _running or _STOP_POLL:
        return
    now = time.ticks_ms()
    surface.poll(now)

    # Route pot movements to the engine (§9.2 -> instrument params).
    if _engine is not None and hasattr(_engine, "update_pot"):
        for ch, v in surface.knob_changes():
            _engine.update_pot(ch, v)
            # Record for the param-name overlay.
            global _last_knob_ch, _last_knob_time
            _last_knob_ch = ch
            _last_knob_time = now

    # Joystick → MIDI pitch bend (X axis) + modulation (Y axis), C++ style.
    joy = surface.joystick()
    if joy is not None:
        jx, jy, j_btn = joy
        from . import midi as _midi
        _midi.pitch_bend((jx - 0.5) * 2.0)
        _midi.cc(1, jy * 127)

    # Joystick menu navigation (C++ System.cpp handleInstrumentMenu).
    if _menu_active:
        if joy is not None:
            _handle_joystick_menu(jx, jy)

    # Route control-button events.
    for ev in surface.events():
        _handle_event(ev)

    # Redraw the display at a reduced rate.
    _tick += 1
    if _tick % _DISPLAY_EVERY == 0:
        _draw_ui()

_last_joy = None
_last_menu_nav_time = 0
_last_knob_ch = -1
_last_knob_time = 0

def _handle_joystick_menu(jx, jy):
    """Joystick navigation in menu mode (200ms gate, matching C++).

    C++ Controls.readJoystick() converts raw 0..1 to:
      X: -1..1 (bipolar, center 0)
      Y: 0..1 (unipolar, reversed: forward = 1)
    Then handleInstrumentMenu uses:
      jY > 0.65 → row 0 (top), jY < 0.35 → row 1 (bottom)
      jX < -0.2 → col left, jX > 0.2 → col right

    Our surface.joystick() returns calibrated 0..1 for both axes.
    Convert to the C++ convention.
    """
    global _menu_row, _menu_col, _last_menu_nav_time
    import time as _time
    now = _time.ticks_ms()
    if now - _last_menu_nav_time < 200:
        return

    # Convert: calibrated 0..1 → bipolar X (-1..1), Y stays 0..1
    jx_bip = (jx - 0.5) * 2.0  # -1..1

    changed = False
    if jy > 0.65:
        _menu_row = 0
        changed = True
    elif jy < 0.35:
        _menu_row = 1
        changed = True
    if jx_bip < -0.2:
        if _menu_col > 0:
            _menu_col -= 1
            changed = True
    elif jx_bip > 0.2:
        if _menu_col < 2:
            _menu_col += 1
            changed = True
    if changed:
        _last_menu_nav_time = now
        _draw_ui()


def _handle_event(ev):
    global _menu_active, _menu_row, _menu_col, _menu_insts
    if not ev:
        return

    if ev[0] == "button":
        _, idx, kind = ev
        # Long-press on joystick button (idx 4) opens menu (C++ System.cpp)
        if kind == "long_press" and idx == 4:
            if _menu_active:
                close_menu()
            else:
                open_menu()
            return

        if _menu_active:
            if kind == "short_press" and idx == 4:
                # Joystick button short press = select instrument
                idx_sel = _menu_row * 3 + _menu_col
                if idx_sel < len(_menu_insts):
                    name = _menu_insts[idx_sel]
                    print("cynthia: selecting %s..." % name)
                    _menu_active = False
                    play(name)
                return
            if kind == "short_press":
                if idx == BTN_OCTAVE_DOWN:
                    _menu_col = max(0, _menu_col - 1)
                    _draw_ui()
                elif idx == BTN_OCTAVE_UP:
                    _menu_col = min(2, _menu_col + 1)
                    _draw_ui()
                elif idx == BTN_PATCH_PREV:
                    _menu_row = max(0, _menu_row - 1)
                    _draw_ui()
                elif idx == BTN_PATCH_NEXT:
                    _menu_row = min(1, _menu_row + 1)
                    _draw_ui()
            return

        # Not in menu: route button to normal functions.
        if kind != "short_press":
            return
        if idx == BTN_OCTAVE_DOWN:
            midi.octave_shift(midi.octave_shift() - 1)
            print("cynthia: octave", midi.octave_shift())
            _draw_ui()
        elif idx == BTN_OCTAVE_UP:
            midi.octave_shift(midi.octave_shift() + 1)
            print("cynthia: octave", midi.octave_shift())
            _draw_ui()
        elif idx == BTN_PATCH_PREV:
            print("cynthia: patch", prev_patch())
        elif idx == BTN_PATCH_NEXT:
            print("cynthia: patch", next_patch())


def _draw_ui():
    disp = surface.display()
    if disp is None:
        return
    disp.fill(0)

    # Always draw header (C++ _drawHeader before everything).
    inst_title = _name.upper() if _name else "CYNTHIA"
    global _last_joy

    # Check if we should show a knob-change overlay (500ms persist).
    global _last_knob_ch, _last_knob_time
    import time as _time
    show_overlay = (_last_knob_ch >= 0 and _time.ticks_diff(_time.ticks_ms(), _last_knob_time) < 500)

    if show_overlay:
        # Universal param name — engine can override with pot_name().
        if _engine is not None and hasattr(_engine, "pot_name"):
            _pname = _engine.pot_name(_last_knob_ch)
        elif 0 <= _last_knob_ch < len(_POT_NAMES):
            _pname = _POT_NAMES[_last_knob_ch]
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
        disp.draw_instrument_grid(_menu_insts, _menu_row, _menu_col)
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


def calibrate_ui():
    """Guided joystick calibration with on-screen instructions.
    Falls back to auto-calibration after 5s if no button press.
    """
    global _STOP_POLL
    _STOP_POLL = True

    disp = surface.display()
    import time as _time

    def _clear_show(msg, extra=""):
        if disp:
            disp.fill(0)
            disp.draw_str(display.FONT_5X7, 2, 7, "CALIBRATE", 1)
            disp.hline(0, 10, 128, 1)
            disp.draw_str(display.FONT_5X7, 2, 20, msg, 1)
            if extra:
                disp.draw_str(display.FONT_5X7, 2, 35, extra, 1)
            disp.show()

    print("cynthia: calibrating joystick...")

    # Phase 1: Auto-sample center (no button needed)
    _clear_show("Sampling center...", "hold joystick still")
    _time.sleep_ms(1000)

    xs, ys = [], []
    for _ in range(100):
        joy = surface._raw_joystick()
        if joy:
            x, y, _b = joy
            if 0.001 < x < 0.999 and 0.001 < y < 0.999:
                xs.append(x)
                ys.append(y)
        _time.sleep_ms(10)

    if not xs:
        _clear_show("Failed! No samples", "Try again")
        print("cynthia: calibration failed — no valid samples")
        _time.sleep_ms(2000)
        _STOP_POLL = False
        return

    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    print("cynthia: center: cx=%.3f cy=%.3f" % (cx, cy))

    # Phase 2: Auto-sample range (sweep joystick around) — ~4s
    _clear_show("Sweep joystick around...", "move to all extremes")
    print("cynthia: sweeping for range — move joystick through all extremes for ~4s...")

    x_vals = list(xs)
    y_vals = list(ys)
    for _ in range(400):
        joy = surface._raw_joystick()
        if joy and 0.001 < joy[0] < 0.999 and 0.001 < joy[1] < 0.999:
            x_vals.append(joy[0])
            y_vals.append(joy[1])
        _time.sleep_ms(10)

    lo_x = min(x_vals)
    hi_x = max(x_vals)
    lo_y = min(y_vals)
    hi_y = max(y_vals)

    cal = {"cx": cx, "cy": cy,
           "min_x": lo_x, "max_x": hi_x,
           "min_y": lo_y, "max_y": hi_y}

    print("cynthia: calibration: cx=%.3f cy=%.3f x=[%.3f..%.3f] y=[%.3f..%.3f]"
          % (cx, cy, lo_x, hi_x, lo_y, hi_y))

    _clear_show("Applying calibration...", "cx=%.2f cy=%.2f" % (cx, cy))
    _time.sleep_ms(500)
    surface.save_joystick_cal(cal)

    _clear_show("Done! (press to verify)", "")
    print("cynthia: calibration saved to /user/joystick_cal.json")
    _time.sleep_ms(2000)

    # Test: read the button briefly to see if it works
    print("cynthia: testing joystick button for 5s...")
    _clear_show("Test: press joystick", "button to confirm")
    found_btn = False
    for _ in range(250):  # 5s
        if hasattr(tulip, "cynthia_button_event"):
            ev = tulip.cynthia_button_event()
            if ev and ev[0] == 4:
                found_btn = True
                print("cynthia: joystick button event: btn=%d kind=%d" % (ev[0], ev[1]))
        _time.sleep_ms(20)

    if found_btn:
        print("cynthia: joystick button IS working")
    else:
        print("cynthia: no joystick button events detected (may be hardware)")

    _STOP_POLL = False
    _draw_ui()
