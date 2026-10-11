# cynthia/registry.py
# Daughterboard driver registry (§4).
#
# Reads /user/daughterboard.json (authoritative), best-effort I2C-probes for
# pre-fill/warning, instantiates the configured drivers with their Slot, and
# aggregates their capabilities into the surface.
#
# Driver modules live frozen in cynthia/drivers/<name>.py. A driver is a
# class (or module) exposing:
#   Driver(slot, cfg_entry) with start()/stop() and capability attributes.

from . import config, connector, pins

# Known I2C device signatures for best-effort autodetect (type only — the
# shared buses mean a probe can never say WHICH slot a device sits in).
KNOWN_I2C = {
    0x20: "mcp23017",   # spark_main keyboard/buttons (also 0x21)
    0x21: "mcp23017",
    0x3C: "sh1106",     # spark_main display (also 0x3D)
    0x3D: "sh1106",
    0x1B: "at42qt2120",  # ondine touch
    0x48: "ads1x15",     # mainboard ADS (always present, not a daughterboard)
}

_loaded = {}   # slot_id -> driver instance
_slots = {}    # slot_id -> Slot


def slot(slot_id):
    """Return the Slot for `slot_id`, creating it on first use."""
    if slot_id not in _slots:
        _slots[slot_id] = connector.Slot(slot_id)
    return _slots[slot_id]


def _import_driver(name):
    """Import cynthia.drivers.<name> and return its Driver class."""
    mod = __import__("cynthia.drivers.%s" % name, None, None, ("Driver",))
    return mod.Driver


def autodetect_report():
    """Probe both I2C buses; return {addr: type} of known daughterboard devices.

    Best-effort only: pot boards probe as nothing, and shared buses can't
    attribute a device to a slot. Used to pre-fill an empty config or warn.
    """
    found = {}
    for bus in (0, 1):
        i2c = connector.i2c_bus(
            bus,
            pins.I2C_SCL if bus == 0 else pins.I2C_DISPLAY_SCL,
            pins.I2C_SDA if bus == 0 else pins.I2C_DISPLAY_SDA,
        )
        for addr in config.probe_i2c(i2c):
            if addr in KNOWN_I2C and addr != pins.ADS1115_ADDR:
                found[addr] = KNOWN_I2C[addr]
    return found


def start():
    """Load config, instantiate configured drivers, call their start().

    Returns the dict of loaded drivers {slot_id: driver}. Slots without a
    configured driver are left untouched (a "none" surface still works).
    """
    cfg = config.load()
    for slot_id in range(pins.NUM_SLOTS):
        name = config.driver_for(cfg, slot_id)
        if not name or name == "none":
            continue
        if slot_id in _loaded:
            continue  # already running
        try:
            driver_cls = _import_driver(name)
        except (ImportError, AttributeError) as e:
            print("cynthia: no driver %r for slot %d: %s" % (name, slot_id, e))
            continue
        entry = cfg["slots"].get("slot%d" % slot_id, {})
        try:
            drv = driver_cls(slot(slot_id), entry)
            drv.start()
            _loaded[slot_id] = drv
        except Exception as e:
            print("cynthia: driver %r slot %d failed to start: %s" % (name, slot_id, e))
    return _loaded


def stop():
    """Stop all loaded drivers and release their slots."""
    for slot_id, drv in list(_loaded.items()):
        try:
            drv.stop()
        except Exception as e:
            print("cynthia: driver slot %d stop failed: %s" % (slot_id, e))
        if slot_id in _slots:
            _slots[slot_id].release()
    _loaded.clear()


def drivers():
    """Loaded driver instances {slot_id: driver}."""
    return _loaded