# cynthia/settings.py
# Persistent user settings (§17), stored as /user/settings.json.
#
# Deliberately flat and schema-free-ish: this file is hand-editable over the
# REPL and is written by the settings menu on device. Every read is defensive —
# a corrupt or partially-written settings.json must never stop the synth from
# booting, because the file is written from the UI and a power cut mid-write is
# a realistic failure. Each accessor therefore has a documented default and
# falls back to it on any parse/type problem.
#
# Which settings live here vs elsewhere:
#   - locale            (this file; i18n.py reads the same key)
#   - midi_channel      mirror of cynthia.midi.channel()
#   - velocity          mirror of cynthia.midi.velocity()
#   - octave            mirror of cynthia.midi.octave_shift()
#   - ble_midi_enabled  whether to advertise the BLE-MIDI service at boot
#   - ble_name          advertised device name
# Hardware wiring stays in /user/daughterboard.json (cynthia/config.py) — that
# file is authoritative for slot assignment and is a different concern; mixing
# them would make one corrupt file take out both.

import json

SETTINGS_PATH = "/user/settings.json"

# name -> default. The settings menu is driven by this table, so a new setting
# only needs an entry here plus a renderer branch (see runtime._draw_settings_ui).
DEFAULTS = {
    "locale": "en",
    "midi_channel": 0,
    "velocity": 100,
    "octave": 0,
    # On by default: CYNTHIA is meant to be visible to a phone/DAW out of the
    # box, and "I don't see Bluetooth" is the failure mode when it defaults off
    # and is only discoverable from a menu. Toggleable in Settings.
    "ble_midi_enabled": True,
    "ble_name": "CYNTHIA",
    # Instrument browser presentation: "grid" (2x3 tiles) or "list" (one
    # column). The grid wastes most of its cells once you have a handful of
    # instruments, and truncated tile labels are hard to scan.
    "menu_style": "grid",
}

_cache = None


def _read():
    """Load the whole file into a dict (cached). Always returns a dict."""
    global _cache
    if _cache is not None:
        return _cache
    data = {}
    try:
        with open(SETTINGS_PATH) as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data = raw
    except (OSError, ValueError):
        pass
    _cache = data
    return _cache


def get(key):
    """Current value of `key`, or its default."""
    data = _read()
    if key in data:
        return data[key]
    return DEFAULTS.get(key)


def set(key, value):
    """Set `key` and persist. Returns the stored value."""
    data = _read()
    data[key] = value
    _save()
    return value


def _save():
    """Write the cache to disk.

    Written to a temporary name and renamed: settings are saved from the UI, and
    a power cut between truncate and write would otherwise leave an empty
    settings.json that the next boot reads as "everything default". rename() is
    atomic on FAT for a same-directory replace, which is the property needed.
    """
    try:
        with open(SETTINGS_PATH + ".tmp", "w") as f:
            json.dump(_read(), f)
        try:
            import os
            os.remove(SETTINGS_PATH)
        except OSError:
            pass
        import os
        os.rename(SETTINGS_PATH + ".tmp", SETTINGS_PATH)
        return True
    except OSError:
        return False


def reload():
    """Drop the cache so the next read comes from disk (hand edits over REPL)."""
    global _cache
    _cache = None


def items():
    """[(key, value)] in DEFAULTS order — the settings menu's row order."""
    return [(k, get(k)) for k in DEFAULTS]


def apply_to_runtime():
    """Push the persisted MIDI settings into cynthia.midi.

    Called once at boot after the MIDI module is importable. Kept separate from
    the accessors so a unit test (or the settings menu) can read settings
    without touching live routing state.
    """
    from . import midi
    try:
        midi.channel(int(get("midi_channel")))
        midi.velocity(int(get("velocity")))
        midi.octave_shift(int(get("octave")))
    except (ValueError, TypeError):
        pass
