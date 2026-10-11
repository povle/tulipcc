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
        # Skip if the user already has this instrument.
        try:
            open(dst + "/sketch.py", "r").close()
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
    """Return sorted list of instrument folder names that contain sketch.py."""
    _ensure_dirs()
    seed_defaults()
    found = []
    try:
        entries = os.listdir(INSTRUMENTS_DIR)
    except OSError:
        return found
    for name in entries:
        try:
            open(INSTRUMENTS_DIR + "/" + name + "/sketch.py", "r").close()
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
    _copy_sketch(name)
    with open(LAST_INSTRUMENT, "w") as f:
        f.write(name)
    import amyboard
    amyboard.stop_sketch()
    # AMY reset recipe (§17): all-notes-off for a clean slate.
    import amy
    amy.send(reset=amy.RESET_ALL_NOTES)
    amyboard.run_sketch()


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
    None. Does NOT run the sketch — start_amy() does that."""
    name = last_instrument()
    if name:
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
        print("\nCYNTHIA instruments:")
        for i, name in enumerate(instruments):
            print("  %d) %s" % (i + 1, _meta_name(name)))
        print("  0) cancel")
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
            print("Loading %s..." % name)
            select(name)
            return