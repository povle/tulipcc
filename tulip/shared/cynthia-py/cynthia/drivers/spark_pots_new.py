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

# Minimum gap between full scans. One scan blocks ~3.8ms in SPI settles; the UI
# poll's budget is ~10ms, so scanning every poll burned ~37% of it. 20ms (50Hz)
# is far faster than a hand can turn a pot and leaves the poll responsive.
SCAN_PERIOD_MS = 20
# Pot movement that justifies rewriting the LED strip (see _update_leds_auto).
LED_EPSILON = 0.004

# Endless knobs: rotation is reported as DELTAS, not an absolute position.
#
# Absolute mapping is wrong for a 360-degree pot on an absolute parameter: the
# knob's physical angle has nothing to do with the patch's value, so touching it
# snapped the parameter to the knob, and turning past the wrap point slammed it
# from max to min. Deltas fix both: the value only moves by the amount the knob
# turns.
#
# Emitted in 2-degree steps. That is the hysteresis that keeps ADC/wiper noise
# from accumulating into a slow drift while the knob sits still: the anchor only
# advances when a step is emitted, so noise below the threshold is discarded
# rather than integrated.
DELTA_DEADBAND_DEG = 2.0

# Rotation per scan that no hand can produce. At 50Hz a 90-degree step would be
# 25 turns/second. Anything bigger is a sensing glitch (typically the two wipers
# being reconciled 180 degrees apart), so it is dropped and counted instead of
# slammed into a parameter. `debug()` exposes the count.
GLITCH_DEG = 90.0


def _ticks_diff(a, b):
    """Wrap-safe tick difference."""
    return time.ticks_diff(a, b)

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
        # Rotation accumulation for relative mode (see DELTA_DEADBAND_DEG).
        self._anchor = [None] * NUM_POTS   # last angle a delta was emitted at
        self._delta = [0.0] * NUM_POTS     # pending turns since last read
        self._has_delta = False
        self._glitches = 0
        # Per-board override for noise hysteresis; see debug()/set_delta_deadband.
        try:
            self._deadband = float(cfg_entry.get("delta_deadband_deg",
                                                 DELTA_DEADBAND_DEG))
        except (TypeError, ValueError):
            self._deadband = DELTA_DEADBAND_DEG
        # Last values the LED strip was told about. A 16-LED WS2812 write is
        # ~0.5ms of blocking, so pushing every scan (50Hz) wasted 2.5% of the
        # frame to redraw colours that had not changed.
        self._led_shown = [None] * NUM_POTS
        self._last_scan_ms = None
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
            # LEDs are optional. A missing module, a wrong pin or an unpopulated
            # strip must leave the pots working, so this is best-effort: losing
            # the whole driver over a decorative feature is a bad trade.
            try:
                import neopixel
                n = led_cfg.get("count", NUM_POTS)
                self._leds = neopixel.NeoPixel(
                    self.slot.pin(led_cfg.get("pin", "E"), machine.Pin.OUT), n)
            except Exception as e:
                self._leds = None
                print("cynthia: pot-board LEDs unavailable:", e)

    def stop(self):
        # Release the SPI host too: machine.SPI(id) cannot be created twice, so
        # a stop()/start() pair (instrument switch, driver reload) would fail
        # with "SPI host already in use" if the peripheral stayed claimed.
        if self._spi is not None:
            try:
                self._spi.deinit()
            except Exception:
                pass
            self._spi = None
        for i in range(NUM_POTS):
            self._anchor[i] = None
            self._delta[i] = 0.0
        self._has_delta = False
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
            #
            # Wiper 0's dead zones are at BOTH ends of its 340 deg travel
            # (raw near 0 and near 1023), so its distance is the nearer of the
            # two. Only measuring from the 340 end made a wiper sitting just
            # off raw=0 look maximally safe and picked its noisy value.
            dist0 = ang0
            if 340.0 - ang0 < dist0:
                dist0 = 340.0 - ang0
            # Wiper 1 is offset by 180 deg, so its dead zones land at 160 and
            # 180 after normalisation.
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
            # Relative mode: accumulate wrap-aware rotation in degrees, emitted
            # in DELTA_DEADBAND_DEG steps from an anchor that only moves when a
            # step is emitted.
            anchor = self._anchor[i]
            if anchor is None:
                self._anchor[i] = ang
            else:
                d = ang - anchor
                if d > 180.0:
                    d -= 360.0
                elif d < -180.0:
                    d += 360.0
                if d >= GLITCH_DEG or d <= -GLITCH_DEG:
                    # Not a hand movement: reject, but keep the anchor where it
                    # was so the next sane sample is measured from the last
                    # good position.
                    self._glitches += 1
                elif d >= self._deadband or d <= -self._deadband:
                    self._delta[i] += d / 360.0
                    self._has_delta = True
                    self._anchor[i] = ang
        if self._leds and self._led_mode == "auto":
            self._update_leds_auto()

    def poll(self, now_ms=None):
        """Scan at a PACED rate, not on every call.

        A full scan is 16 addresses x (mux settle + 2 ADC reads) = ~3.8ms of
        BLOCKING time.sleep_us settles. surface.poll() runs from the UI poll at
        ~96Hz, whose whole budget is ~10ms, so scanning every call burned ~37% of
        the frame in busy-waiting — and starved the same poll that feeds the
        software watchdog. Pots are mechanical; nobody can turn 16 of them faster
        than ~50Hz. Pace it so a scan costs at most a fraction of the budget.
        """
        if now_ms is None:
            import time
            now_ms = time.ticks_ms()
        last = self._last_scan_ms
        if last is not None and _ticks_diff(now_ms, last) < SCAN_PERIOD_MS:
            return
        self._last_scan_ms = now_ms
        self.scan()

    def knobs(self):
        """16 knob positions 0..1 (absolute angle / 360), for the knob map."""
        return self.values

    def debug(self):
        """Per-pot (index, raw0, raw1, angle_deg) for diagnosing a bad board.

        raw0/raw1 are the two wiper ADC counts (0..1023) and angle_deg is the
        reconciled 0..360 position. If the raws are stable but the angle jumps,
        the reconciler is at fault (software); if the raws themselves jump, it is
        the sensing (hardware/noise/mux).
        """
        out = []
        for i in range(NUM_POTS):
            out.append((i, self._phase0[i], self._phase1[i],
                        round(self.values[i] * 360.0, 1)))
        return out

    def set_delta_deadband(self, deg):
        """Change the rotation hysteresis at runtime (diagnostic aid)."""
        self._deadband = float(deg)
        return self._deadband

    def delta_deadband(self):
        return self._deadband

    def glitch_count(self):
        """Samples rejected as impossible movement (see GLITCH_DEG)."""
        return self._glitches

    def knob_deltas(self):
        """Rotation since the last call: [(pot_index, turns)]. Deltas are
        consumed. Returns () (no allocation) when nothing turned."""
        if not self._has_delta:
            return ()
        out = []
        for i in range(NUM_POTS):
            d = self._delta[i]
            if d:
                out.append((i, d))
                self._delta[i] = 0.0
        self._has_delta = False
        return out

    # --- NeoPixels (§10.2) ---

    def _update_leds_auto(self):
        # LED i tracks pot i: map position to hue across a pleasant range
        # (0.0 warm/red -> 1.0 cool/blue), constant brightness. Only rewrite
        # when a pot moved past the threshold; see _led_shown above.
        changed = False
        for i in range(len(self._leds)):
            v = self.values[i] if i < NUM_POTS else 0.0
            shown = self._led_shown[i] if i < NUM_POTS else None
            if shown is not None:
                d = v - shown
                if d < 0:
                    d = -d
                if d < LED_EPSILON:
                    continue
            rgb = _hue_to_rgb(v * 0.66)
            self._leds[i] = rgb
            if i < NUM_POTS:
                self._led_shown[i] = v
            changed = True
        if changed:
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