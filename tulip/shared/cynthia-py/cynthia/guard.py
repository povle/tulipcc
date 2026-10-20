# cynthia/guard.py
# Boot guard: make "boot -> crash -> reboot" recoverable instead of a loop.
#
# The failure this exists for: a sketch in /user/instruments/<name>/sketch.py
# that reliably breaks the board (hard fault, or a loop() that starves the CPU
# until the task watchdog resets it). CYNTHIA's boot flow copies the
# last-selected instrument into /user/current/ *before* running it, so every
# reboot faithfully reloads the same broken sketch -> another crash -> another
# reboot. amyboard.run_sketch()'s self-heal cannot save this: it reacts to a
# Python *exception* on import, but (a) a hard fault reboots before it runs, and
# (b) even when it does fire, the next boot overwrites the repaired file from
# the instrument folder again.
#
# So the guard counts boots that never completed. A boot that reaches the end of
# _boot.py clears the counter; a boot that dies before that leaves it set. After
# MAX_BOOT_ATTEMPTS in a row, safe mode skips the restore and lands at the REPL
# with the offending instrument named — which is reachable without any input
# hardware, since CYNTHIA is headless and has no boot-button escape hatch
# (self_test_if_button() is AMYBOARD-only).
#
# Counting in a *file* rather than RTC_NOINIT on purpose: it survives a full
# power cycle, which is exactly what a person does when the board appears dead.

import json

GUARD_PATH = "/user/boot_guard.json"
MAX_BOOT_ATTEMPTS = 3
# A boot that completes and then dies anyway (watchdog reset from a hanging
# loop()) needs its own, larger budget: this is "the board keeps rebooting while
# I'm not touching it", which is what a person actually experiences. Higher than
# MAX_BOOT_ATTEMPTS because a completed boot is much more likely to be a good
# instrument with one bad moment, and resetting the count requires a *stable*
# boot — so a rare, occasional reset never accumulates here.
MAX_WDT_ATTEMPTS = 6


def _read():
    try:
        with open(GUARD_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    try:
        with open(GUARD_PATH, "w") as f:
            json.dump(d, f)
        return True
    except OSError:
        return False


def note_boot(instrument=None):
    """Record that a boot has started. Returns the consecutive attempt count.

    Called early, before the last instrument is restored, so a boot that never
    completes is counted.

    The count is of consecutive failures of the *same* instrument: if a different
    one is being tried, the previous count belongs to the old one and the count
    restarts. Without that, a single bad instrument would drag every later (good)
    instrument into safe mode with it.
    """
    d = _read()
    prev = d.get("last")
    if instrument and prev and instrument != prev:
        d["attempts"] = 0
    d["attempts"] = int(d.get("attempts", 0)) + 1
    if instrument:
        d["last"] = instrument
    _write(d)
    return d["attempts"]


def clear():
    """Boot completed — reset the "never completed" counter, keep the rest.

    'Completed' means _boot.py reached the end: the instrument was restored AND
    its top-level code ran AND the runtime attached.

    Deliberately does NOT reset `wdt`, the post-boot counter, because a boot that
    completes and *then* wedges looks identical to a healthy one from here — see
    `note_wdt_boot()`. `wdt` is cleared separately, once a boot has survived
    long enough to be considered stable.
    """
    d = _read()
    d["attempts"] = 0
    _write(d)


def note_wdt_boot():
    """Called at the start of a boot whose previous reset was the watchdog.

    The gap this closes (found on hardware, not in simulation): the counter above
    only sees boots that never *complete*, but a sketch whose top-level code is
    fine and whose `loop()` hangs completes boot normally — imports, starts
    loop(), reaches guard.clear() — and only wedges several ticks later. So
    `attempts` stays 0 forever while the board reboots every 5 seconds in an
    infinite loop and safe mode never trips.

    Counting those separately is what makes "the board keeps rebooting while I'm
    not touching it" a condition the guard can act on.
    """
    d = _read()
    d["wdt"] = int(d.get("wdt", 0)) + 1
    _write(d)
    return d["wdt"]


def clear_wdt():
    """Mark this boot as stable (called from the poll loop after a quiet period).

    Only a *stable* boot resets the post-boot counter, so an occasional watchdog
    reset cannot accumulate, while a sketch that hangs every time trips it.
    """
    d = _read()
    if d.get("wdt"):
        d["wdt"] = 0
        _write(d)


def wdt_attempts():
    return int(_read().get("wdt", 0))


def in_wdt_safe_mode():
    return wdt_attempts() >= MAX_WDT_ATTEMPTS


def enter_wdt_safe_mode(culprit):
    """Safe mode triggered by repeated post-boot watchdog resets.

    Clears both counters for the same reason as `enter_safe_mode`: safe mode is a
    transition. See that function for the full rationale.
    """
    suspend(culprit)
    d = _read()
    d["attempts"] = 0
    d["wdt"] = 0
    _write(d)


def attempts():
    return int(_read().get("attempts", 0))


def enter_safe_mode(culprit):
    """Called when safe mode has been *decided* for `culprit`.

    Clears BOTH counters. This is essential and not obvious: safe mode is a
    TRANSITION, not a state. If a counter were left high, every subsequent boot
    would also satisfy the corresponding `in_*_safe_mode()`, so the board would
    never run an instrument again — turning a recoverable crash loop into a
    REPL-only brick, which is worse than the problem the guard exists to solve.

    Suspends `culprit` so its own re-restore cannot restart the loop. The next
    boot therefore starts clean, restores whatever is selected (skipping the
    suspended one), and if that also fails it has a full budget of its own.
    """
    suspend(culprit)
    d = _read()
    d["attempts"] = 0
    d["wdt"] = 0
    _write(d)


def in_safe_mode():
    """Are we booting too many times without completing?"""
    return attempts() >= MAX_BOOT_ATTEMPTS


def last_instrument():
    return _read().get("last")


def suspend(name):
    """Remember an instrument as bad, so the boot restore skips it.

    Kept separate from `attempts` so that clearing the counter on a good boot
    does not immediately re-arm the broken sketch on the next reboot.
    """
    if not name:
        return
    d = _read()
    s = d.get("suspended") or []
    if name not in s:
        s.append(name)
    d["suspended"] = s
    _write(d)


def suspended():
    return list(_read().get("suspended") or [])


def resume(name):
    """Clear a suspension (the user re-picked it deliberately)."""
    d = _read()
    s = [n for n in (d.get("suspended") or []) if n != name]
    d["suspended"] = s
    _write(d)


def reset():
    """Forget everything: attempt count and suspensions."""
    _write({"attempts": 0, "suspended": []})


def validate(text, filename="sketch.py"):
    """Compile a sketch without running it. Returns a reason string, or None if OK.

    Used before a sketch becomes the *active* one (browser.select / runtime.play /
    library.archive), so a truncated download or a broken edit is refused while
    the good instrument keeps playing — rather than being installed and then
    discovered at the next boot, where the only symptom is a crash loop.

    Deliberately NOT called from the boot path: run_sketch() already self-heals a
    file that fails to import, and compiling 100KB+ at every boot would cost real
    milliseconds for no extra safety.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeError:
            return "not valid UTF-8"
    try:
        compile(text, filename, "exec")
    except SyntaxError as e:
        return "SyntaxError: %s" % (e,)
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)
    return None


def read_sketch(folder):
    """Read <folder>/sketch.py as text, or None."""
    try:
        with open(folder + "/sketch.py") as f:
            return f.read()
    except OSError:
        return None
