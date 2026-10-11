# cynthia/drivers/spark_pots_new.py
# Spark pot board (new) driver (§10.2).
#
# Port of /Users/pasha/projects/synth/angle_test/angle_test.ino (the working
# Arduino prototype).
#
# Hardware:
#   - 16 endless (360 deg) pots (Alps RDC803101A), each with TWO wipers
#     (phases) offset 180 deg. Each wiper spans 340 deg of electrical travel;
#     together they cover the full 360 deg. Absolute angle is recovered by
#     mapping each wiper to its 340 deg segment and picking/averaging the one
#     outside its end-of-travel dead zone.
#   - 74HC393D 4-bit counter drives the address lines of TWO multiplexers.
#     The muxes are wired differently, so at a given counter address the two
#     ADC channels come from DIFFERENT sensors:
#       mux_0 (-> MCP3002 CH0, phase 0): addr 0..15 = sensors 8,7,6,5,4,3,2,1,16,15,14,13,12,11,10,9
#       mux_1 (-> MCP3002 CH1, phase 1): addr 0..15 = sensors 1,2,3,4,5,6,7,8,16,15,14,13,12,11,10,9
#     Therefore the full 16-address cycle is scanned and stored first, and
#     angles are computed afterwards.
#   - MCP3002 2-ch 10-bit ADC over hardware SPI (GPIO matrix, §3.1).
#
# Slot pin assignments (from angle_test.ino / Board.h, slot 2):
#   IO_A_2 = counter CLK          -> slot pin A
#   IO_G_2 = counter RST (high)   -> slot pin G
#   IO_B_2 = MCP3002 DIN  (MOSI)  -> slot pin B
#   IO_C_2 = MCP3002 DOUT (MISO)  -> slot pin C
#   IO_D_2 = MCP3002 CLK  (SCK)   -> slot pin D
#   IO_F_2 = MCP3002 ~CS          -> slot pin F
#
# §8 discipline: hardware SPI, pre-allocated buffers, zero allocation in the
# scan path, paced by the caller (never busy-loop).

import machine
import time

NUM_POTS = 16

# Each wiper covers 340 deg electrically (datasheet: 340 deg 1-phase,
# 360 deg 2-phase). The two wipers are offset 180 deg.
WIPER_RANGE_DEG = 340.0
# Raw-counts threshold for deadzone detection (wiper near the ADC rails).
DEADZONE_RAW = 60
# Mux/counter settle times (from the prototype).
MUX_SETTLE_US = 100
ADC_SETTLE_US = 20

# MUX0_MAP[addr] = sensor number (1-based) routed to MCP3002 CH0 (phase 0)
# MUX1_MAP[addr] = sensor number (1-based) routed to MCP3002 CH1 (phase 1)
MUX0_MAP = (8, 7, 6, 5, 4, 3, 2, 1, 16, 15, 14, 13, 12, 11, 10, 9)
MUX1_MAP = (1, 2, 3, 4, 5, 6, 7, 8, 16, 15, 14, 13, 12, 11, 10, 9)

# MCP3002 control bytes: single-ended, MSB first.
_CMD_CH0 = 0x68
_CMD_CH1 = 0x78


class Driver:
    def __init__(self, slot, cfg_entry):
        self.slot = slot
        self.cfg = cfg_entry
        self._clk = None      # counter CLK
        self._rst = None      # counter RST
        self._cs = None       # MCP3002 ~CS
        self._spi = None
        # Pre-allocated scan state (zero-alloc, §8).
        self._phase0 = [0] * NUM_POTS
        self._phase1 = [0] * NUM_POTS
        self._tx = bytearray(2)
        self._rx = bytearray(2)
        # Knob values 0..1 (angle / 360), exposed via knobs().
        self.values = [0.0] * NUM_POTS
        # NeoPixels (optional, §10.2) — only if configured.
        self._leds = None
        self._led_mode = "auto"

    # --- lifecycle ---

    def start(self):
        self._clk = self.slot.pin("A", machine.Pin.OUT)
        self._rst = self.slot.pin("G", machine.Pin.OUT)
        self._cs = self.slot.pin("F", machine.Pin.OUT)
        self._clk.value(0)
        self._rst.value(0)
        self._cs.value(1)
        self._spi = self.slot.spi(sck="D", mosi="B", miso="C", baudrate=1000000)
        self._reset_counter()
        time.sleep_ms(10)  # supply ramp for muxes/ADC (from the prototype)
        led_cfg = self.cfg.get("leds")
        if led_cfg:
            import neopixel
            n = led_cfg.get("count", NUM_POTS)
            self._leds = neopixel.NeoPixel(
                self.slot.pin(led_cfg.get("pin", "E"), machine.Pin.OUT), n)

    def stop(self):
        self.slot.release()

    # --- MCP3002 ---

    def _read_adc(self, channel):
        """Read one MCP3002 channel (0/1) -> 10-bit raw. Matches the
        prototype's dummy-then-real transaction + parse for 0x68/0x78."""
        cmd = _CMD_CH0 if channel == 0 else _CMD_CH1
        self._tx[0] = cmd
        self._tx[1] = 0x00
        # Dummy transaction to clear internal state and settle the mux.
        self._cs.value(0)
        self._spi.write(self._tx)
        self._cs.value(1)
        time.sleep_us(ADC_SETTLE_US)
        # Real transaction.
        self._cs.value(0)
        self._spi.write_readinto(self._tx, self._rx)
        self._cs.value(1)
        # b0 contains 6 leading zeros + B9, B8; b1 contains B7..B0.
        return ((self._rx[0] & 0x03) << 8) | self._rx[1]

    # --- 74HC393D counter ---

    def _reset_counter(self):
        self._rst.value(1)
        time.sleep_us(10)
        self._rst.value(0)
        time.sleep_us(MUX_SETTLE_US)

    def _pulse_counter(self):
        # 74HC393 counts on the falling edge of CLK.
        self._clk.value(1)
        time.sleep_us(10)
        self._clk.value(0)
        time.sleep_us(MUX_SETTLE_US)

    # --- angle math (ported from computeAngle) ---

    def _compute_angle(self, raw0, raw1):
        ang0 = (raw0 / 1023.0) * WIPER_RANGE_DEG           # 0..340
        ang1 = (raw1 / 1023.0) * WIPER_RANGE_DEG + 180.0   # 180..520
        if ang1 >= 360.0:
            ang1 -= 360.0
        # Do the two angles agree (within 2 deg, wrap-aware)?
        diff = ang1 - ang0
        if diff > 180.0:
            diff -= 360.0
        elif diff < -180.0:
            diff += 360.0
        if diff < 2.0 and diff > -2.0:
            return ang0 + diff * 0.5
        # Disagree: one wiper is in its deadzone. A dead wiper reads near the
        # ADC rails.
        w0_rails = raw0 < DEADZONE_RAW or raw0 > (1023 - DEADZONE_RAW)
        w1_rails = raw1 < DEADZONE_RAW or raw1 > (1023 - DEADZONE_RAW)
        if w0_rails and not w1_rails:
            return ang1
        if w1_rails and not w0_rails:
            return ang0
        if not w0_rails and not w1_rails:
            # Neither near the rails: use the wiper farther from its deadzone.
            dist0 = (340.0 - ang0) if ang0 < 340.0 else 0.0
            if 160.0 <= ang1 <= 180.0:
                dist1 = 0.0
            else:
                d160 = ang1 - 160.0
                if d160 < 0:
                    d160 = -d160
                d180 = ang1 - 180.0
                if d180 < 0:
                    d180 = -d180
                dist1 = d160 if d160 < d180 else d180
            return ang0 if dist0 > dist1 else ang1
        return ang0  # both near rails (shouldn't happen); fall back

    # --- scan ---

    def scan(self):
        """One full 16-address scan; updates self.values (0..1 per pot).

        Call at a paced rate (§8). Takes a few ms (32 SPI reads + settles).
        """
        self._reset_counter()  # address 0
        for addr in range(NUM_POTS):
            ch1 = self._read_adc(1)
            ch0 = self._read_adc(0)
            self._phase0[MUX0_MAP[addr] - 1] = ch0
            self._phase1[MUX1_MAP[addr] - 1] = ch1
            if addr < NUM_POTS - 1:
                self._pulse_counter()
        for i in range(NUM_POTS):
            ang = self._compute_angle(self._phase0[i], self._phase1[i])
            self.values[i] = ang / 360.0
        if self._leds and self._led_mode == "auto":
            self._update_leds_auto()

    def poll(self, now_ms=None):
        self.scan()

    def knobs(self):
        """16 knob positions 0..1 (absolute angle / 360). Endless knobs —
        surface.sync_knobs() handles takeover (§9.1)."""
        return self.values

    # --- NeoPixels (§10.2) ---

    def _update_leds_auto(self):
        # LED i tracks pot i: map position to hue across a pleasant range
        # (0.0 warm/red -> 1.0 cool/blue), constant brightness.
        for i in range(len(self._leds)):
            v = self.values[i] if i < NUM_POTS else 0.0
            self._leds[i] = _hue_to_rgb(0.0 + v * 0.66)
        self._leds.write()

    def set_led_mode(self, mode):
        """'auto' (track pots) or 'manual' (explicit set_leds)."""
        self._led_mode = mode
        if mode == "auto" and self._leds:
            self._update_leds_auto()

    def set_leds(self, colors):
        """Explicit per-LED (r, g, b); switches to manual mode."""
        if not self._leds:
            return
        self._led_mode = "manual"
        for i, c in enumerate(colors):
            if i >= len(self._leds):
                break
            self._leds[i] = c
        self._leds.write()


def _hue_to_rgb(h):
    """h in 0..1 -> (r, g, b) 0..255, full saturation, constant brightness."""
    i = int(h * 6.0)
    f = h * 6.0 - i
    q = int(255 * (1.0 - f))
    t = int(255 * f)
    i = i % 6
    if i == 0:
        return (255, t, 0)
    if i == 1:
        return (q, 255, 0)
    if i == 2:
        return (0, 255, t)
    if i == 3:
        return (0, q, 255)
    if i == 4:
        return (t, 0, 255)
    return (255, 0, q)