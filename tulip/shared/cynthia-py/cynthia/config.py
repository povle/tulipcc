# cynthia/config.py
# Daughterboard configuration (§4).
#
# /user/daughterboard.json is AUTHORITATIVE for slot assignment:
#   {
#     "adc_chip": "ads1115",
#     "slots": {
#       "slot0": { "driver": "spark_main" },
#       "slot1": { "driver": "spark_pots_new" }
#     }
#   }
# Autodetect is best-effort only (pot boards have no I2C devices, and the
# I2C buses are shared across both slots so a probe can't say which slot a
# device is in). It pre-fills an empty config or warns on mismatch; it never
# silently overrides explicit config.

import json

CONFIG_PATH = "/user/daughterboard.json"

DEFAULTS = {
    "adc_chip": "ads1115",   # or "ads1015" — see daughterboard.json
    "slots": {},
}

# The stock Spark instrument wiring, used to seed a first-boot config when
# /user/daughterboard.json does not exist yet (see seed_default()). Slot ids
# are the 0-based JSON keys, which map to the hardware "IO_x_<slot+1>" names:
#   slot0 = hardware slot 1 (IO_x_1) -> spark_main keyboard/joystick/display
#   slot1 = hardware slot 2 (IO_x_2) -> spark_pots_legacy 16-pot board
# (spark_pots_legacy's mux address lines are IO_A_2..IO_D_2 and its mux
# output feeds slot 1's analog pin A = ADS AIN2, matching pins.py slot 1.)
#
# slot1: the NEW two-wiper endless-pot board (MCP3002 + counter). The previous
# stock board (ADS + 4-GPIO mux, `spark_pots_legacy`) is still available by
# setting the driver name by hand; see CONFIG_REVISION below.
DEFAULT_SLOTS = {
    "slot0": {"driver": "spark_main"},
    "slot1": {"driver": "spark_pots_new"},
}

# Bumped when the stock hardware changes. An older seeded file is migrated
# once; a driver name the user chose by hand is never rewritten afterwards
# (the revision is stored, so the migration runs only for revision < this).
CONFIG_REVISION = 2


def load():
    """Return the merged config dict (defaults <- /user/daughterboard.json)."""
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy of defaults
    try:
        with open(CONFIG_PATH) as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    if "adc_chip" in user:
        cfg["adc_chip"] = user["adc_chip"]
    for slot, entry in (user.get("slots") or {}).items():
        cfg["slots"][slot] = entry
    return cfg


def save(cfg):
    """Persist config to /user/daughterboard.json."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


def seed_default():
    """Write the stock Spark daughterboard config to /user/daughterboard.json
    if the file does not exist yet (first boot / fresh flash). Idempotent —
    an existing file is never touched. Call from _boot.py before
    registry.start() so the drivers actually come up on a new board."""
    try:
        open(CONFIG_PATH).close()
        # Already exists: upgrade it if it was seeded by an older stock
        # revision. Without this, a board that booted once keeps the old driver
        # name forever and the new pot board silently does nothing.
        _migrate()
        return
    except OSError:
        pass
    save({"adc_chip": DEFAULTS["adc_chip"], "slots": DEFAULT_SLOTS,
          "revision": CONFIG_REVISION})
    print("cynthia: wrote default daughterboard.json (spark_main + spark_pots_new)")


def _migrate():
    """One-time upgrade of a config seeded by an older stock revision."""
    import json
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    if data.get("revision", 1) >= CONFIG_REVISION:
        return
    slots = data.get("slots")
    if not isinstance(slots, dict):
        slots = {}
        data["slots"] = slots
    slot1 = slots.get("slot1")
    if not isinstance(slot1, dict):
        slot1 = {}
    # Only swap the OLD STOCK driver. Anything else is a deliberate choice.
    if slot1.get("driver", "spark_pots_legacy") == "spark_pots_legacy":
        slot1 = dict(slot1)
        slot1["driver"] = "spark_pots_new"
        slots["slot1"] = slot1
        print("cynthia: daughterboard.json slot1 -> spark_pots_new (new pot board)")
    data["revision"] = CONFIG_REVISION
    save(data)


def driver_for(cfg, slot_id):
    """Driver name configured for `slot_id`, or None."""
    entry = cfg["slots"].get("slot%d" % slot_id)
    if entry:
        return entry.get("driver")
    return None


def probe_i2c(i2c):
    """Best-effort I2C scan -> list of responding addresses (autodetect aid)."""
    try:
        return i2c.scan()
    except OSError:
        return []