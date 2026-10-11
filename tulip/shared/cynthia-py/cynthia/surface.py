# cynthia/surface.py
# The control/UI-plane surface (§9).
#
# Aggregates the loaded daughterboard drivers (cynthia.registry) into one
# readable surface: pots, buttons, joystick, display, LEDs. Note inputs
# (keyboard, joystick-in-play) go through MIDI routing (cynthia.midi) and do
# NOT appear here.
#
# Getters return CACHED values, never block (§8). start()/stop() manage the
# driver layer + ADS polling.

import time
import tulip

from . import registry, ads

# Pot values must move more than this (0..1) before they count as changed.
KNOB_EPSILON = 0.004

_drivers = {}
_main = None          # driver providing joystick/display (spark_main)
_pot = None           # driver providing knobs()
_display = None
_values = []          # cached knob positions 0..1
_last = []            # previous poll's knob positions
_started = False


def start():
    """Start the daughterboard drivers (config-driven) and discover caps.

    Idempotent: drivers already running (e.g. started by _boot) are kept.
    """
    global _main, _pot, _display, _values, _last, _started
    registry.start()
    _drivers = registry.drivers()
    _main = None
    _pot = None
    for drv in _drivers.values():
        if hasattr(drv, "joystick") and hasattr(drv, "display"):
            _main = drv
        if hasattr(drv, "knobs"):
            _pot = drv
    _display = _main.display() if _main and hasattr(_main, "display") else None
    n = len(_pot.knobs()) if _pot else 16
    _values = [0.0] * n
    _last = []         # empty: knob_changes() will align _last to curr on first call
    _started = True


def stop():
    """Stop all drivers and release their hardware."""
    global _started, _main, _pot, _display
    registry.stop()
    _started = False
    _main = None
    _pot = None
    _display = None


def started():
    return _started


def poll(now_ms=None):
    """One paced poll step: ADS conversions, driver scans, knob cache update.

    Call at ~50-100Hz (§8). Never blocks on input; I2C reads are the only
    cost and they are bounded by the number of active drivers.
    """
    if now_ms is None:
        now_ms = time.ticks_ms()
    mgr = ads.get()
    if mgr:
        mgr.poll()
    if _main and hasattr(_main, "poll"):
        _main.poll(now_ms)
    if _pot and hasattr(_pot, "poll"):
        _pot.poll(now_ms)
        k = _pot.knobs()
        for i in range(len(_values)):
            _values[i] = k[i]
    elif hasattr(tulip, "cynthia_knobs"):
        k = tulip.cynthia_knobs()
        if len(_values) < len(k):
            _values.extend([0.0] * (len(k) - len(_values)))
        for i in range(len(k)):
            _values[i] = k[i]


def knobs():
    """All pot positions 0..1 (cached)."""
    if hasattr(tulip, "cynthia_knobs"):
        return list(tulip.cynthia_knobs())
    return _values


def knob_changes(epsilon=KNOB_EPSILON):
    """[(index, value)] for pots that moved more than epsilon since the last
    call. Used by the runtime to route only real movements to the sketch.
    First call or after init aligns _last to curr so no false changes fire."""
    curr = knobs()
    changed = []
    global _last
    if len(_last) < len(curr):
        # Init new slots to curr value so initial position doesn't trigger
        _last = list(curr) if len(_last) <= 0 else _last + [0.0] * (len(curr) - len(_last))
    for i in range(len(curr)):
        v = curr[i]
        d = v - _last[i]
        if d < 0:
            d = -d
        if d > epsilon:
            _last[i] = v
            changed.append((i, v))
    return changed


def sync_knobs(values):
    """Knob takeover sync (§9.1): align the cached values (and endless-knob
    virtual positions, when the driver supports them) with a patch's values
    so loading an instrument doesn't jump."""
    global _values, _last
    n = min(len(values), len(_values)) if _values else len(values)
    for i in range(n):
        _values[i] = values[i]
        _last[i] = values[i]
    if _pot and hasattr(_pot, "sync"):
        try:
            _pot.sync(values)
        except Exception:
            pass


def buttons():
    """Raw control-button states (driver-specific dict)."""
    if hasattr(tulip, "cynthia_buttons"):
        mask = tulip.cynthia_buttons()
        return {
            "octave_down": bool(mask & (1 << 0)),
            "octave_up": bool(mask & (1 << 1)),
            "patch_prev": bool(mask & (1 << 2)),
            "patch_next": bool(mask & (1 << 3)),
            "joystick": bool(mask & (1 << 4)),
        }
    if _main and hasattr(_main, "buttons"):
        return _main.buttons()
    return {}


# --- Joystick calibration ---

_CAL_FILE = "/user/joystick_cal.json"
_joy_cal = None  # cached calibration dict


def load_joystick_cal():
    """Load saved joystick calibration from flash."""
    global _joy_cal
    try:
        import json
        with open(_CAL_FILE) as f:
            _joy_cal = json.load(f)
        for k in ("cx", "cy"):
            if k not in _joy_cal:
                _joy_cal = None
                break
    except (OSError, ValueError):
        _joy_cal = None
    return _joy_cal


def save_joystick_cal(data):
    """Save calibration dict to flash. Call after calibrate_joystick()."""
    import json
    try:
        with open(_CAL_FILE, "w") as f:
            json.dump(data, f)
        global _joy_cal
        _joy_cal = data
        return True
    except OSError:
        return False


def calibrate_joystick(samples=200):
    """Sample joystick at rest, compute center only (simple)."""
    return _calibrate_center_only(samples)

def _calibrate_center_only(samples):
    """Internal: sample center."""
    global _joy_cal
    xs, ys = [], []
    print("cynthia: calibrating joystick — hold still, do not touch...")
    import time as _time
    for _ in range(samples):
        joy = _raw_joystick()
        if joy:
            x, y, _b = joy
            if 0.001 < x < 0.999 and 0.001 < y < 0.999:
                xs.append(x)
                ys.append(y)
        _time.sleep_ms(5)
    if not xs:
        print("cynthia: calibration failed — no samples")
        return None
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    cal = {"cx": cx, "cy": cy}
    save_joystick_cal(cal)
    print("cynthia: calibration saved: cx=%.3f cy=%.3f" % (cx, cy))
    return cal


def calibrate_joystick_full():
    """Full guided calibration with center + range.

    Phase 1: hold joystick centered, press joystick button.
    Phase 2: sweep joystick to all extremes, press joystick button.
    Saves center and endpoint ranges to /user/joystick_cal.json.

    Designed to work with the display UI (runtime.calibrate_ui() calls this).
    Returns the calibration dict or None on failure.
    """
    import time as _time
    global _joy_cal

    # Phase 1: Center
    print("cynthia: calibrate — center joystick, then press joystick button")
    _time.sleep_ms(500)
    while True:
        ev = _read_joy_btn()
        if ev == "short_press":
            break
        _time.sleep_ms(20)

    # Wait a moment for hand to be steady, then sample center.
    _time.sleep_ms(300)
    xs, ys = [], []
    for _ in range(100):
        joy = _raw_joystick()
        if joy:
            x, y, _b = joy
            if 0.001 < x < 0.999 and 0.001 < y < 0.999:
                xs.append(x)
                ys.append(y)
        _time.sleep_ms(10)
    if not xs:
        print("cynthia: calibration failed at center")
        return None
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    print("cynthia: center: cx=%.3f cy=%.3f" % (cx, cy))

    # Phase 2: Range — sweep joystick to all extremes
    print("cynthia: calibrate — sweep joystick to all corners, then press button")
    while True:
        ev = _read_joy_btn()
        if ev == "short_press":
            break
        joy = _raw_joystick()
        if joy and 0.001 < joy[0] < 0.999 and 0.001 < joy[1] < 0.999:
            xs.append(joy[0])
            ys.append(joy[1])
        _time.sleep_ms(20)

    lo_x = min(xs) if xs else cx - 0.1
    hi_x = max(xs) if xs else cx + 0.1
    lo_y = min(ys) if ys else cy - 0.1
    hi_y = max(ys) if ys else cy + 0.1

    cal = {"cx": cx, "cy": cy,
           "min_x": lo_x, "max_x": hi_x,
           "min_y": lo_y, "max_y": hi_y}
    save_joystick_cal(cal)
    print("cynthia: calibration saved: cx=%.3f cy=%.3f x=[%.3f..%.3f] y=[%.3f..%.3f]"
          % (cx, cy, lo_x, hi_x, lo_y, hi_y))
    return cal


def _read_joy_btn(timeout_ms=30000):
    """Read joystick button events, returning 'short_press' or 'long_press' or None."""
    import time as _time
    start = _time.ticks_ms()
    while _time.ticks_diff(_time.ticks_ms(), start) < timeout_ms:
        ev = None
        if hasattr(tulip, "cynthia_button_event"):
            e = tulip.cynthia_button_event()
            if e:
                btn, kind = e
                if btn == 4:
                    ev = "long_press" if kind == 1 else "short_press"
        if ev:
            return ev
        _time.sleep_ms(10)
    return None


def _raw_joystick():
    """Raw joystick (x, y, btn) from C or driver, no calibration."""
    if hasattr(tulip, "cynthia_joystick"):
        return tulip.cynthia_joystick()
    if _main and hasattr(_main, "joystick"):
        return _main.joystick()
    return None


def joystick():
    """(x, y, btn) calibrated joystick state, or None.

    Applies saved calibration: raw 0..1 → centered at 0.5 with deadzone.
    Falls back to raw if no calibration saved.
    """
    raw = _raw_joystick()
    if raw is None:
        return None
    jx, jy, btn = raw

    global _joy_cal
    if _joy_cal is None:
        load_joystick_cal()
    if _joy_cal is not None:
        cx, cy = _joy_cal["cx"], _joy_cal["cy"]
        lo_x = _joy_cal.get("min_x", cx - 0.1)
        hi_x = _joy_cal.get("max_x", cx + 0.1)
        lo_y = _joy_cal.get("min_y", cy - 0.1)
        hi_y = _joy_cal.get("max_y", cy + 0.1)

        def _cal(v, center, lo, hi):
            dz = 0.03
            if abs(v - center) < dz:
                return 0.5
            if v < center:
                return 0.5 - 0.5 * (center - v) / (center - lo)
            else:
                return 0.5 + 0.5 * (v - center) / (hi - center)

        jx = max(0.0, min(1.0, _cal(jx, cx, lo_x, hi_x)))
        jy = max(0.0, min(1.0, _cal(jy, cy, lo_y, hi_y)))

    return (jx, jy, btn)


def events():
    """Drain and return UI-plane events (("button", idx, "short_press") etc.)
    from all drivers."""
    evs = []
    if hasattr(tulip, "cynthia_button_event"):
        while True:
            ev = tulip.cynthia_button_event()
            if not ev:
                break
            btn_idx, kind_code = ev
            kind_str = "long_press" if kind_code == 1 else "short_press"
            evs.append(("button", btn_idx, kind_str))
    for drv in registry.drivers().values():
        e = getattr(drv, "events", None)
        if e:
            while e:
                evs.append(e.pop(0))
    return evs


def display():
    """The first driver-provided display (SH1106), or None if headless."""
    return _display


def set_leds(colors):
    """Explicit per-LED (r, g, b) (§10.2 manual mode)."""
    if _pot and hasattr(_pot, "set_leds"):
        _pot.set_leds(colors)


def set_led_mode(mode):
    """"auto" (track pots) or "manual" (§10.2)."""
    if _pot and hasattr(_pot, "set_led_mode"):
        _pot.set_led_mode(mode)
