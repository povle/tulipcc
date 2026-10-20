# cynthia/ble_midi.py
# BLE-MIDI peripheral (§16 "operating roles"): CYNTHIA appears to a phone/iPad/DAW
# as a standard Bluetooth LE MIDI device, so the 28 keys, pots, joystick and
# buttons play the host, and the host's notes play CYNTHIA's synth.
#
# This is the MicroPython port of the original Arduino InstrumentBLE.cpp. The
# radio half maps 1:1; the *routing* half deliberately differs, because the C++
# version hard-coded which CC each pot sent. Here the firmware already has one
# MIDI bus (cynthia/midi.py, §6) that both internal controls and external MIDI
# travel on, so BLE simply taps that bus in both directions:
#
#   internal controls --inject()--> AMY (as always)
#                             \--> _forwarder --> BLE notify --> host
#   host write --> parse --> tulip.midi_local() --> AMY
#
# A sketch cannot tell a phone's note from a key press, and adding BLE changes
# nothing about how instruments are written.
#
# Protocol notes (BLE-MIDI, Apple spec 2014):
#   service        03b80e5a-ede8-4b33-a751-6ce34ec4c700
#   characteristic 7772e5db-3868-4112-a1a9-f2669d106bf3
# Every notification is prefixed with a header byte (0x80 | timestamp_high) and
# a timestamp-low byte. 0x80/0x80 (timestamp 0) is legal and means "now", which
# is what real-time event forwarding should send.
#
# WHY RAW `bluetooth`, NOT aioble
# -------------------------------
# The first version used aioble, which is asyncio-based. `_boot.py` created a
# task with `uasyncio.create_task()` — but nothing ever RUNS that event loop on
# CYNTHIA (the UI is driven by an AMY TulipSequence callback, not asyncio), so
# the advertising coroutine was scheduled and never executed. The radio stayed
# silent while our own state said "Advertising", which is exactly the "it says
# it works but nothing appears" symptom.
#
# The raw `bluetooth` API is callback-driven, needs no event loop, and mirrors
# the C++ implementation (BLEDevice/BLEServer + callbacks) far more directly.

import micropython
import time

SERVICE_UUID = "03b80e5a-ede8-4b33-a751-6ce34ec4c700"
CHAR_UUID = "7772e5db-3868-4112-a1a9-f2669d106bf3"

_HEADER = 0x80   # header + timestamp-high (bit7 set, no high bits)
_TS = 0x80       # timestamp low = 0 (send "now")

# GAP/GATT event codes (not exported by the bluetooth module to save space).
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21

# Advertised connection interval hint (units of 1.25ms): 500us..500us is not
# allowed; use a 7.5ms..15ms range, which keeps note latency low.
_ADV_INTERVAL_US = 100000

# Guards against echoing the host's own MIDI back to it: an incoming write is
# injected into the shared bus, which calls every forwarder — including ours.
_echo_guard = [False]

_state = {
    "enabled": False,
    "connected": False,
    "advertising": False,
    "name": "CYNTHIA",
    "error": None,
    "sent": 0,
    "received": 0,
    # Bumped on every successful connection, so the UI can notice a new link
    # without polling the whole status dict (which allocates) each frame.
    "conn_seq": 0,
}

_ble = None
_conn = None
_handle = None
_irq_set = False


def status():
    """Snapshot for the settings screen / REPL diagnostics."""
    return dict(_state)


def is_connected():
    """Cheap bool: avoid building a status dict on every UI poll."""
    return _state["connected"]


def connection_seq():
    """Cheap counter: changes when a central connects."""
    return _state["conn_seq"]


def is_available():
    """True if this firmware build has the `bluetooth` module (CYNTHIA does)."""
    try:
        import bluetooth  # noqa: F401
        return True
    except ImportError:
        return False


# --- advertising ----------------------------------------------------------

def _uuid_bytes(uuid_str):
    """128-bit UUID string -> 16 bytes in BLE (little-endian) order.

    Done by hand rather than `bytes(bluetooth.UUID(...))`: that relies on the
    UUID object supporting the buffer protocol, and the advertising payload is
    the one thing that must be exactly right for iOS to list the device.
    """
    h = uuid_str.replace("-", "")
    out = bytearray(16)
    for i in range(16):
        out[15 - i] = int(h[i * 2:i * 2 + 2], 16)
    return bytes(out)


def _adv_payloads(name):
    """Build (adv_data, resp_data) for a BLE-MIDI peripheral.

    iOS only lists a peripheral that carries the 128-bit MIDI service UUID in
    its advertising data (it filters scan results by service), and takes the
    name from the scan response. Flags must come first.
    """
    svc = _uuid_bytes(SERVICE_UUID)
    adv = bytearray()
    adv += b"\x02\x01\x06"                 # len 2, type 0x01 (flags), value: LE general discoverable
    adv += bytes((len(svc) + 1, 0x07))     # len, type 0x07 (complete list of 128-bit UUIDs)
    adv += svc
    nm = name.encode() if hasattr(name, "encode") else bytes(name)
    if len(nm) > 20:
        nm = nm[:20]
    resp = bytearray()
    resp += bytes((len(nm) + 1, 0x09))     # len, type 0x09 (complete local name)
    resp += nm
    return adv, resp


def _advertise():
    if _ble is None or not _state["enabled"]:
        return
    try:
        adv, resp = _adv_payloads(_state["name"])
        _ble.gap_advertise(_ADV_INTERVAL_US, adv_data=adv, resp_data=resp,
                           connectable=True)
        _state["advertising"] = True
        _state["error"] = None
    except Exception as e:
        _state["advertising"] = False
        _state["error"] = repr(e)
        print("cynthia: BLE advertise failed:", e)


# --- IRQ ------------------------------------------------------------------

def _irq(event, data):
    global _conn
    if event == _IRQ_CENTRAL_CONNECT:
        conn_handle, _addr_type, _addr = data
        _conn = conn_handle
        _state["connected"] = True
        _state["advertising"] = False
        _state["conn_seq"] += 1
        print("cynthia: BLE MIDI connected")
    elif event == _IRQ_CENTRAL_DISCONNECT:
        _conn = None
        _state["connected"] = False
        print("cynthia: BLE MIDI disconnected — advertising again")
        # iOS drops the link when it backgrounds the app; keep advertising so
        # re-pairing does not need a reboot (the C++ version did the same).
        _advertise()
    elif event == _IRQ_GATTS_WRITE:
        conn_handle, attr_handle = data
        if attr_handle != _handle:
            return
        try:
            payload = bytes(_ble.gatts_read(attr_handle))
        except Exception:
            return
        # Do NOT call into AMY from the NimBLE task: hand the bytes to the VM
        # thread. cynthia_input.c uses mp_sched_schedule for the same reason.
        try:
            micropython.schedule(_deliver, payload)
        except Exception:
            _deliver(payload)


def _deliver(payload):
    """Parse a BLE-MIDI packet and feed AMY's normal MIDI path."""
    try:
        _inject_incoming(payload)
    except Exception as e:
        _state["error"] = repr(e)


# --- MIDI parsing ---------------------------------------------------------

def _inject_incoming(data):
    """Parse a BLE-MIDI notification into AMY's MIDI parser."""
    if data is None or len(data) < 3:
        return
    i = 0
    # BLE-MIDI packets begin with header (bit7 set) + timestamp (bit7 set).
    if data[0] & 0x80:
        i = 1
    if i < len(data) and data[i] & 0x80:
        i += 1
    status = 0
    need = 0
    got = 0
    msg = [0, 0, 0]
    from . import midi as _midi
    while i < len(data):
        b = data[i]
        i += 1
        if b & 0x80:
            # System real-time (0xF8..0xFF) is a COMPLETE one-byte message and
            # must not become the new running status: it may appear anywhere,
            # including between the bytes of a channel message.
            if b >= 0xF8:
                _state["received"] += 1
                _echo_guard[0] = True
                try:
                    _midi.inject(data=bytes((b,)))
                except Exception as e:
                    _state["error"] = repr(e)
                finally:
                    _echo_guard[0] = False
                continue
            # System common (0xF0..0xF7) has no running status and variable
            # length; BLE-MIDI hosts send none of it for control, so drop it
            # rather than guess a length that would eat the next message.
            if b >= 0xF0:
                status = 0
                need = 0
                got = 0
                continue
            status = b
            msg[0] = b
            got = 1
            # 0xC0 program change and 0xD0 channel pressure take one data byte.
            hi = b & 0xF0
            need = 1 if (hi == 0xC0 or hi == 0xD0) else 2
            continue
        if not status:
            continue
        msg[got] = b
        got += 1
        if got >= need + 1:
            _state["received"] += 1
            _echo_guard[0] = True
            try:
                _midi.inject(data=bytes((msg[0], msg[1], msg[2]) if need == 2
                                        else (msg[0], msg[1])))
            except Exception as e:
                _state["error"] = repr(e)
            finally:
                _echo_guard[0] = False
            got = 1   # running status: keep the status byte


def forward(msg):
    """Forward an already-injected message to the host.

    Used for events the C input task injected directly (`cynthia_midi_tap`):
    those never pass through cynthia.midi.inject(), so the forwarder list never
    sees them. No echo risk here — the source is a physical control.
    """
    _notify(msg)


# --- internal MIDI -> host -------------------------------------------------

def _on_internal_midi(msg):
    """Tap for cynthia.midi.inject(): forward an internally-generated message."""
    if _echo_guard[0]:
        # This message originated from the host itself; sending it back would
        # double every note (and, with a fast host, loop).
        return
    _notify(msg)


def _notify(msg):
    if not _state["connected"] or _ble is None or _conn is None or _handle is None:
        return
    n = len(msg)
    pkt = bytearray(2 + n)
    pkt[0] = _HEADER
    pkt[1] = _TS
    for i in range(n):
        pkt[2 + i] = msg[i]
    try:
        _ble.gatts_notify(_conn, _handle, pkt)
        _state["sent"] += 1
    except Exception as e:
        _state["error"] = repr(e)


# --- lifecycle ------------------------------------------------------------

def start(name=None, task_runner=None):
    """Begin advertising BLE-MIDI. Idempotent.

    `task_runner` is accepted for backwards compatibility and ignored: the raw
    API needs no task (see the module docstring).
    """
    global _ble, _handle, _irq_set
    if _state["enabled"] and _state["advertising"]:
        return True
    if name:
        _state["name"] = name
    if not is_available():
        _state["error"] = "bluetooth module not in this firmware build"
        print("cynthia: BLE MIDI unavailable:", _state["error"])
        return False
    try:
        import bluetooth

        if _ble is None:
            _ble = bluetooth.BLE()
        _ble.active(True)
        try:
            _ble.config(gap_name=_state["name"])
        except Exception:
            pass
        if not _irq_set:
            _ble.irq(_irq)
            _irq_set = True

        # Register once; re-registering on every start() would leak handles.
        if _handle is None:
            svc = bluetooth.UUID(SERVICE_UUID)
            ch = bluetooth.UUID(CHAR_UUID)
            flags = (bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
                     | bluetooth.FLAG_WRITE_NO_RESPONSE | bluetooth.FLAG_NOTIFY)
            ((_handle,),) = _ble.gatts_register_services(((svc, ((ch, flags),)),))

        _state["enabled"] = True
        _state["error"] = None
        _advertise()

        # Tap the internal MIDI bus.
        from . import midi as _midi
        _midi.add_forwarder(_on_internal_midi)
        return True
    except Exception as e:
        _state["enabled"] = False
        _state["advertising"] = False
        _state["error"] = repr(e)
        print("cynthia: BLE MIDI start failed:", e)
        return False


def stop():
    """Stop advertising and drop the connection."""
    global _conn
    _state["enabled"] = False
    _state["advertising"] = False
    _state["connected"] = False
    _conn = None
    try:
        from . import midi as _midi
        _midi.remove_forwarder(_on_internal_midi)
    except Exception:
        pass
    if _ble is not None:
        try:
            _ble.gap_advertise(None)
        except Exception:
            pass
