# cynthia/browser.py
# Instrument browser v1 — headless REPL menu (§17, §18 step 7).
#
# Product concept: list of amyworld sketches = list of selectable instruments.
#   /user/instruments/<name>/sketch.py   # standard amyworld sketch
#   /user/instruments/<name>/meta.json   # optional display name/author/tags
#   /user/instruments/<name>/state.json  # CYNTHIA-managed state (later step)
#
# v1 is REPL-only (no display yet): it scans /user/instruments, prints a
# numbered menu, and on selection copies the instrument's sketch.py into
# /user/current/ and reuses amyboard.run_sketch() — the exact same loader
# the web editor and AMYboard boot use (fresh import, _auto_generated_knobs
# applied, loop() started).
#
# Selection is remembered in /user/last_instrument.txt so boot can go
# straight to PLAY with it (§17 boot flow).

import os
import json

from .i18n import t

INSTRUMENTS_DIR = "/user/instruments"
CURRENT_SKETCH = "/user/current/sketch.py"
LAST_INSTRUMENT = "/user/last_instrument.txt"
# Bundled instruments ship on the read-only system partition; seed_defaults()
# copies them into /user/instruments/ the first time they are missing.
BUNDLED_INSTRUMENTS_DIR = "/sys/instruments"


def _ensure_dirs():
    for path in (INSTRUMENTS_DIR, "/user/current"):
        try:
            os.mkdir(path)
        except OSError:
            pass


def _refresh_meta(src_dir, dst_dir):
    """Copy meta.json from the bundled instrument if the local copy differs."""
    try:
        with open(src_dir + "/meta.json", "rb") as f:
            new = f.read()
    except OSError:
        return
    try:
        with open(dst_dir + "/meta.json", "rb") as f:
            if f.read() == new:
                return
    except OSError:
        pass
    try:
        with open(dst_dir + "/meta.json", "wb") as f:
            f.write(new)
    except OSError:
        pass


def seed_defaults():
    """Copy bundled instruments from /sys/instruments/ into /user/instruments/
    for any that are not already present. Idempotent."""
    _ensure_dirs()
    try:
        bundled = os.listdir(BUNDLED_INSTRUMENTS_DIR)
    except OSError:
        return
    for name in bundled:
        src = BUNDLED_INSTRUMENTS_DIR + "/" + name
        dst = INSTRUMENTS_DIR + "/" + name
        # Only copy folders that contain a sketch.py.
        try:
            open(src + "/sketch.py", "r").close()
        except OSError:
            continue
        # Already installed? Refresh the bundled DISPLAY METADATA but leave the
        # sketch alone. Without this, a firmware that renames an instrument
        # ("Juno-106" -> "juno") can never take effect on a board that was
        # seeded once, because the folder exists and seed_defaults used to skip
        # it wholesale. meta.json is shipped data, not user data (users name
        # patches, not instrument folders), so overwriting it is safe; sketch.py
        # is not, since a user may have edited it.
        try:
            open(dst + "/sketch.py", "r").close()
            _refresh_meta(src, dst)
            continue
        except OSError:
            pass
        try:
            os.mkdir(dst)
        except OSError:
            pass
        for fname in ("sketch.py", "meta.json"):
            try:
                with open(src + "/" + fname, "rb") as f:
                    data = f.read()
                with open(dst + "/" + fname, "wb") as f:
                    f.write(data)
            except OSError:
                pass


def list_instruments():
    """Return sorted list of instrument folder names with a usable sketch.py.

    "Usable" means non-empty, not just present: a folder left half-written by a
    failed install (disk full) or a truncated copy would otherwise show up in the
    menu and only fail when selected. An empty file is also what a failed write
    leaves behind, so this is the cheap guard against that class.
    """
    _ensure_dirs()
    seed_defaults()
    found = []
    try:
        entries = os.listdir(INSTRUMENTS_DIR)
    except OSError:
        return found
    for name in entries:
        path = INSTRUMENTS_DIR + "/" + name + "/sketch.py"
        try:
            if os.stat(path)[6] == 0:
                continue           # half-written / truncated: not selectable
            found.append(name)
        except OSError:
            continue
    found.sort()
    return found


def _meta_name(name):
    """Display name from meta.json, falling back to the folder name."""
    try:
        with open(INSTRUMENTS_DIR + "/" + name + "/meta.json") as f:
            meta = json.load(f)
        return meta.get("name", name)
    except (OSError, ValueError):
        return name


def _copy_sketch(name):
    """Copy the instrument's sketch.py into /user/current/sketch.py."""
    src = INSTRUMENTS_DIR + "/" + name + "/sketch.py"
    with open(src, "rb") as f:
        data = f.read()
    with open(CURRENT_SKETCH, "wb") as f:
        f.write(data)


def select(name):
    """Load instrument `name` into the current env and run it."""
    # Refuse a sketch that cannot even compile, before it replaces the running
    # one. Otherwise the bad file lands in /user/current/sketch.py and the only
    # symptom is at the NEXT boot (crash loop), with the good instrument already
    # gone from the current env.
    try:
        from . import guard
        text = guard.read_sketch(INSTRUMENTS_DIR + "/" + name)
        if text is not None:
            why = guard.validate(text, name + "/sketch.py")
            if why:
                print("cynthia: refusing to run %r — %s" % (name, why))
                return False
    except Exception:
        pass
    _copy_sketch(name)
    with open(LAST_INSTRUMENT, "w") as f:
        f.write(name)
    # A deliberate re-selection clears any boot-guard suspension: the user has
    # asked for this instrument explicitly, so it is no longer "known bad".
    try:
        from . import guard
        guard.resume(name)
    except Exception:
        pass
    import amyboard
    amyboard.stop_sketch()
    # AMY reset recipe (§17): all-notes-off for a clean slate.
    import amy
    amy.send(reset=amy.RESET_ALL_NOTES)
    amyboard.run_sketch()
    return True


def last_instrument():
    try:
        with open(LAST_INSTRUMENT) as f:
            name = f.read().strip()
        if name in list_instruments():
            return name
    except OSError:
        pass
    return None


def restore_last():
    """Boot-time restore (§17): copy the last instrument's sketch.py into
    /user/current/ BEFORE amyboard.start_amy() runs it. Returns the name or
    None. Does NOT run the sketch — start_amy() does that.

    Skips instruments on the boot guard's suspended list (cynthia/guard.py):
    those are known to prevent boot from completing, and re-restoring one would
    re-enter the crash loop the guard exists to break."""
    name = last_instrument()
    if not name:
        return None
    try:
        from . import guard
        if name in guard.suspended():
            print("cynthia: skipping suspended instrument %r "
                  "(cynthia.guard.resume(%r) to re-enable)" % (name, name))
            return None
    except Exception:
        pass
    _copy_sketch(name)
    return name


def run():
    """Headless REPL menu. Blocks on input(); returns when an instrument is
    chosen (and running), or immediately if there is nothing to choose."""
    instruments = list_instruments()
    if not instruments:
        print("cynthia: no instruments in %s — add <name>/sketch.py folders" % INSTRUMENTS_DIR)
        return
    while True:
        print("\n" + t("instruments_menu"))
        for i, name in enumerate(instruments):
            print("  %d) %s" % (i + 1, _meta_name(name)))
        print("  0) " + t("cancel_prompt"))
        try:
            choice = input("Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not choice:
            continue
        try:
            idx = int(choice)
        except ValueError:
            # Allow typing the instrument's folder name directly.
            if choice in instruments:
                idx = instruments.index(choice) + 1
            else:
                print("No such instrument: %r" % choice)
                continue
        if idx == 0:
            return
        if 1 <= idx <= len(instruments):
            name = instruments[idx - 1]
            print("%s %s..." % (t("loading_prompt"), name))
            select(name)
            return