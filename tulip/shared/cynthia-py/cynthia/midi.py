# cynthia/midi.py
# The central MIDI event bus for internal controls (§6).
#
# The keyboard and joystick-in-play-mode are exposed to AMY as MIDI SOURCES:
# their events are translated into raw MIDI bytes and injected via
# tulip.midi_local(), which feeds convert_midi_bytes_to_messages() — the
# exact same parser path as external USB/UART MIDI. Internal and external
# MIDI are indistinguishable downstream, so every amyworld sketch works
# with the keyboard automatically.
#
# Conventions (AMYboard): channel 0 (MIDI ch 1) = main/lead voice,
# channel 9 = drums. Keyboard defaults to channel 0, configurable.
#
# §16: because everything rides this one path, future BLE/USB-controller
# modes can forward internal MIDI out by tapping inject().

import tulip

# Pre-allocated 3-byte buffer: zero allocation in the hot path (§8 T1).
_buf = bytearray(3)
# Last-emitted message tuple, so repeated identical sends (the joystick path
# runs ~96Hz) allocate nothing at all. tulip.midi_local() only accepts bytes
# (upstream API), so we snapshot lazily and cache on change.
_last_msg = None
_last_bytes = None

# Keyboard defaults (§6)
_channel = 0        # MIDI channel index (0 = ch 1)
_velocity = 100     # fixed note-on velocity (no velocity sensing)
_base_note = 48     # C3; 28 keys -> notes 48..75
_octave_shift = 0   # firmware note offset, clamped (-4..+4 octaves)


def channel(ch=None):
    """Get/set the MIDI channel index (0..15) internal controls send on."""
    global _channel
    if ch is not None:
        _channel = ch & 0x0F
    return _channel


def velocity(v=None):
    global _velocity
    if v is not None:
        _velocity = max(1, min(127, v))
    return _velocity


def base_note(n=None):
    global _base_note
    if n is not None:
        _base_note = max(0, min(127, n))
    return _base_note


def octave_shift(o=None):
    global _octave_shift
    if hasattr(tulip, "cynthia_octave_shift"):
        if o is not None:
            _octave_shift = tulip.cynthia_octave_shift(max(-4, min(4, o)))
        else:
            _octave_shift = tulip.cynthia_octave_shift()
        return _octave_shift
    if o is not None:
        _octave_shift = max(-4, min(4, o))
    return _octave_shift


def inject(data=None, n=None):
    """Inject the message in _buf (when data is None) or `data`.

    tulip.midi_local() accepts only `bytes` (upstream API), so _buf is
    snapshotted to bytes lazily. The snapshot is cached on the message
    value: repeated identical sends — the joystick path calls this at
    ~96Hz for pitch bend and mod wheel — allocate nothing at all.
    """
    global _last_msg, _last_bytes
    if data is None:
        msg = (_buf[0], _buf[1], _buf[2]) if n is None else tuple(_buf[:n])
        if msg == _last_msg:
            return
        _last_msg = msg
        _last_bytes = bytes(msg)
        tulip.midi_local(_last_bytes)
    else:
        tulip.midi_local(data)


def note_on(key, vel=None, ch=None):
    """Key index (0-based from the keyboard's lowest key) -> MIDI note-on."""
    if vel is None:
        vel = _velocity
    if ch is None:
        ch = _channel
    note = _base_note + key + 12 * _octave_shift
    if note < 0:
        note = 0
    elif note > 127:
        note = 127
    _buf[0] = 0x90 | (ch & 0x0F)
    _buf[1] = note
    _buf[2] = vel & 0x7F
    inject()


def note_off(key, ch=None):
    if ch is None:
        ch = _channel
    note = _base_note + key + 12 * _octave_shift
    if note < 0:
        note = 0
    elif note > 127:
        note = 127
    _buf[0] = 0x80 | (ch & 0x0F)
    _buf[1] = note
    _buf[2] = 0
    inject()


def all_notes_off(ch=None):
    """Panic: CC123 all-notes-off on the keyboard channel."""
    if ch is None:
        ch = _channel
    _buf[0] = 0xB0 | (ch & 0x0F)
    _buf[1] = 123
    _buf[2] = 0
    inject()


def pitch_bend(value, ch=None):
    """value -1.0..1.0 -> 14-bit pitch bend (0xE0), center = 0x2000."""
    if ch is None:
        ch = _channel
    v = int((max(-1.0, min(1.0, value)) + 1.0) * 8191.5)
    if v > 16383:
        v = 16383
    _buf[0] = 0xE0 | (ch & 0x0F)
    _buf[1] = v & 0x7F
    _buf[2] = (v >> 7) & 0x7F
    inject()


def cc(controller, value, ch=None):
    """Send a Control Change (joystick Y -> CC1 modulation, etc.)."""
    if ch is None:
        ch = _channel
    _buf[0] = 0xB0 | (ch & 0x0F)
    _buf[1] = controller & 0x7F
    _buf[2] = max(0, min(127, int(value)))
    inject()


def program_change(program, ch=None):
    """Patch navigation rides MIDI too (§12): emit Program Change so it goes
    through AMY's bank-aware PC handler, same as an external controller."""
    if ch is None:
        ch = _channel
    _buf[0] = 0xC0 | (ch & 0x0F)
    _buf[1] = program & 0x7F
    _buf[2] = 0
    inject(n=2)