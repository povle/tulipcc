# cynthia/drivers/spark_main.py
# Spark main daughterboard driver (§10.1).
#
# Hardware (from the working Arduino firmware, ~/projects/synth/src/Hardware.h
# — the authoritative source for this board revision):
#   - 28-key keyboard, mechanical switches wired DIRECTLY into 2x MCP23017
#     I2C GPIO expanders (no matrix). Keys are NOT linearly mapped: the
#     Arduino keyPins table below is the exact wiring.
#   - 4 control buttons on MCP 0x20 PA4..PA7 (octave-, octave+, patch prev,
#     patch next).
#   - Keys + buttons are scanned in the paced surface poll (~100Hz, 1 tick).
#     IMPORTANT: the MCP23017s AND the ADS1115 share ONE I2C bus on this
#     board, and machine.I2C is not safe to drive from two FreeRTOS tasks at
#     once — so no dedicated key thread. At 100Hz polling, key latency is
#     <=10ms, which is imperceptible.
#   - Analog joystick (2 axes) -> mainboard ADS AIN0/AIN1 (slot 0 A/B);
#     joystick button -> IO_G_1.
#   - SH1106 128x64 OLED on the display I2C bus (SDA=5/SCL=4), address 0x3C.

import machine
import time
import tulip
from .. import midi

# --- MCP23017 registers ---
_IODIRA, _IPOLA, _GPINTENA, _DEFVALA, _INTCONA = 0x00, 0x02, 0x04, 0x06, 0x08
_IOCON, _GPPUA, _INTFA, _INTCAPA, _GPIOA = 0x0A, 0x0C, 0x0E, 0x10, 0x12
_OLATA = 0x14

# --- Slot pin assignments (Hardware.h) ---
# KB_INTERRUPT_1 = IO_C_1 -> MCP 0x20 INT (INTA+INTB joined)
# KB_INTERRUPT_2 = IO_B_1 -> MCP 0x21 INT (INTA+INTB joined)
# BTN_JOYSTICK   = IO_G_1
INT1_LETTER = "C"
INT2_LETTER = "B"
JOY_BTN_LETTER = "G"
MCP_ADDR_1 = 0x20     # keyboard + buttons
MCP_ADDR_2 = 0x21     # keyboard

NUM_KEYS = 28
DEBOUNCE_MS = 20
LONG_PRESS_MS = 800

# --- Key wiring: Arduino keyPins table (Hardware.h), MIDI note A2 = 45 ---
# (mcp addr, MCP pin number) per note, notes 45..72 (A2..C5).
_KEY_PINS = [
    (0x20, 8), (0x20, 9), (0x20, 10), (0x20, 12), (0x20, 11),   # 45..49: A2 A#2 B2 C3 C#3
    (0x20, 14), (0x20, 13), (0x20, 0), (0x20, 2), (0x20, 15),   # 50..54: D3 D#3 E3 F3 F#3
    (0x21, 8), (0x20, 1), (0x21, 10), (0x20, 3), (0x21, 12),    # 55..59: G3 G#3 A3 A#3 B3 (G3=0x21:PB0, G#3=0x20:PA1, A3=0x21:PB2, A#3=0x20:PA3, B3=0x21:PB4)
    (0x21, 14), (0x21, 9), (0x21, 0), (0x21, 11), (0x21, 2),    # 60..64: C4 C#4 D4 D#4 E4
    (0x21, 4), (0x21, 13), (0x21, 6), (0x21, 15), (0x21, 3),    # 65..69: F4 F#4 G4 G#4 A4
    (0x21, 1), (0x21, 5), (0x21, 7),                            # 70..72: A#4 B4 C5
]
_BASE_NOTE = 45

# --- Buttons: Hardware.h buttonKeyPins (all on MCP 0x20) ---
# Order here defines the event index used by cynthia.runtime:
#   0 = octave down, 1 = octave up, 2 = patch prev, 3 = patch next
_BTN_PINS = [
    (0x20, 5),   # Octave Down (PA5)
    (0x20, 4),   # Octave Up (PA4)
    (0x20, 7),   # patch prev (PA7, "BTN2")
    (0x20, 6),   # patch next (PA6, "BTN3")
]


def _build_masks():
    """addr -> 16-bit mask of key pins / button pins (bit n = GPAn/B n)."""
    key_masks = {}
    btn_masks = {}
    for i, (addr, pin) in enumerate(_KEY_PINS):
        key_masks[addr] = key_masks.get(addr, 0) | (1 << pin)
    for i, (addr, pin) in enumerate(_BTN_PINS):
        btn_masks[addr] = btn_masks.get(addr, 0) | (1 << pin)
    return key_masks, btn_masks


_KEY_MASKS, _BTN_MASKS = _build_masks()

# (addr, pin) -> note number, for decoding
_PIN_TO_NOTE = {}
for _i, (_a, _p) in enumerate(_KEY_PINS):
    _PIN_TO_NOTE[(_a, _p)] = _BASE_NOTE + _i
_PIN_TO_BTN = {}
for _i, (_a, _p) in enumerate(_BTN_PINS):
    _PIN_TO_BTN[(_a, _p)] = _i


class Driver:
    def __init__(self, slot, cfg_entry):
        self.slot = slot
        self.cfg = cfg_entry
        self._i2c = slot.i2c[0]
        self._key_state = {}        # note -> bool pressed
        self._btn_state = [0] * 4   # raw pressed state per button idx
        self._joy_btn = None
        self._joy_x = None          # ADS handles
        self._joy_y = None
        self._btn_press_ms = [0] * 4
        self._btn_long_fired = [False] * 4
        self._display = None        # SH1106, created lazily by display()
        self.events = []            # UI-plane events for the mode mgr

    # --- MCP23017 setup ---

    def _mcp_init(self, addr):
        i2c = self._i2c
        # All 16 pins inputs, pull-ups on, interrupt-on-change, INTOD open-drain.
        i2c.writeto_mem(addr, _IODIRA, b"\xFF\xFF")
        i2c.writeto_mem(addr, _GPPUA, b"\xFF\xFF")
        i2c.writeto_mem(addr, _IOCON, b"\x40\x40")   # open-drain INT, mirror
        i2c.writeto_mem(addr, _GPINTENA, b"\xFF\xFF")
        i2c.writeto_mem(addr, _INTCONA, b"\x00\x00")  # compare vs previous
        # Read INTCAP once to clear any latched interrupt.
        i2c.readfrom_mem(addr, _INTCAPA, 4)

    # --- lifecycle ---

    def start(self):
        if not hasattr(tulip, "cynthia_button_event"):
            self._mcp_init(MCP_ADDR_1)
            self._mcp_init(MCP_ADDR_2)
            self._joy_btn = self.slot.pin(JOY_BTN_LETTER, machine.Pin.IN, machine.Pin.PULL_UP)
            # Joystick axes (Hardware.h): JOY_Y = ADC_A_1 = AIN0 (slot A),
            # JOY_X = ADC_B_1 = AIN1 (slot B).
            from .. import ads
            mgr = ads.get()
            if mgr:
                self._joy_y = mgr.subscribe(self.slot.adc["A"], owner=self)
                self._joy_x = mgr.subscribe(self.slot.adc["B"], owner=self)

    def stop(self):
        if not hasattr(tulip, "cynthia_button_event"):
            from .. import ads
            mgr = ads.get()
            if mgr:
                if self._joy_x:
                    mgr.unsubscribe(self._joy_x)
                if self._joy_y:
                    mgr.unsubscribe(self._joy_y)
            self.slot.release()

    # --- poll (called by the paced surface loop at ~100Hz, §8) ---
    def poll(self, now_ms):
        if hasattr(tulip, "cynthia_button_event"):
            while True:
                ev = tulip.cynthia_button_event()
                if not ev:
                    break
                btn_idx, kind_code = ev
                kind_str = "long_press" if kind_code == 1 else "short_press"
                self._emit(("button", btn_idx, kind_str))
            return

        self._scan_inputs()
        self._poll_buttons(now_ms)

    def _read_expander(self, addr):
        """16-bit GPIO state for one MCP23017 (bit n = GPAn/Bn)."""
        b = self._i2c.readfrom_mem(addr, _GPIOA, 2)
        return b[0] | (b[1] << 8)

    def _scan_inputs(self):
        """Read both expanders once, diff keys and buttons."""
        try:
            bits = {
                MCP_ADDR_1: self._read_expander(MCP_ADDR_1),
                MCP_ADDR_2: self._read_expander(MCP_ADDR_2),
            }
        except OSError:
            return  # bus hiccup; try again next poll
        # Keys (active-low with pull-ups).
        for (addr, pin), note in _PIN_TO_NOTE.items():
            pressed = 0 if (bits[addr] >> pin) & 1 else 1
            if self._key_state.get(note, 0) != pressed:
                self._key_state[note] = pressed
                if pressed:
                    midi.note_on(note)
                else:
                    midi.note_off(note)
        # Buttons (all on 0x20).
        b0 = bits[MCP_ADDR_1]
        for (addr, pin), idx in _PIN_TO_BTN.items():
            pressed = 0 if (b0 >> pin) & 1 else 1
            if self._btn_state[idx] != pressed:
                self._btn_state[idx] = pressed
                self._control_button(idx, pressed)

    def _control_button(self, idx, pressed):
        if pressed:
            self._btn_press_ms[idx] = time.ticks_ms()
            self._btn_long_fired[idx] = False
        else:
            if not self._btn_long_fired[idx]:
                self._emit(("button", idx, "short_press"))

    def _poll_buttons(self, now_ms):
        for idx in range(4):
            if self._btn_state[idx] and not self._btn_long_fired[idx]:
                held = time.ticks_diff(now_ms, self._btn_press_ms[idx])
                if held >= LONG_PRESS_MS:
                    self._btn_long_fired[idx] = True
                    self._emit(("button", idx, "long_press"))

    def _emit(self, ev):
        self.events.append(ev)

    def joystick(self):
        """(x, y, btn) — x/y 0..1 from the ADS, btn True = pressed."""
        if hasattr(tulip, "cynthia_joystick"):
            return tulip.cynthia_joystick()
        x = self._joy_x.value if self._joy_x else 0.5
        y = self._joy_y.value if self._joy_y else 0.5
        btn = not self._joy_btn.value() if self._joy_btn else False
        return (x, y, btn)

    def display(self):
        """Return the SH1106 display instance (created on first call).

        The SH1106 sits on I2C bus 1 (SDA=5/SCL=4) per pins.py; fall back to
        bus 0 for builds that wire it with the MCP23017s.
        """
        if self._display is None:
            from ..display import SH1106
            for bus in (1, 0):
                try:
                    self._display = SH1106(i2c=self.slot.i2c[bus])
                    break
                except Exception as e:
                    if bus == 0:
                        print("spark_main: no display found:", e)
        return self._display