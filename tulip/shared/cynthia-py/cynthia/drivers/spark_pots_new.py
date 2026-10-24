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
#   IO_A_2 = ADDR_CLK             -> slot pin A  (net name on the board)
#   IO_G_2 = ADDR_SHDN            -> slot pin G  (board's net name; the driver
#                                   drives it as an active-HIGH reset: idle 0,
#                                   10us high = address 0)
#   IO_B_2 = MCP3002 DIN  (MOSI)  -> slot pin B
#   IO_C_2 = MCP3002 DOUT (MISO)  -> slot pin C
#   IO_D_2 = MCP3002 CLK  (SCK)   -> slot pin D
#   IO_F_2 = MCP3002 ~CS          -> slot pin F
# Slot 1 (0-based) = mainboard DB_2 = GPIO48/47/21/11/12/13/14 for A..G
# (tulip/amyboard/boards/CYNTHIA/pins.h). Nothing else claims A or G: the pot
# board's own NeoPixels are on E, and spark_main lives on slot 0.
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
#
# A wiper is non-linear at the ends of its 340-degree track, so the outer ~10%
# of its span is not trustworthy as an angle source. 60 counts (6%) was too
# tight: a wiper reading 961 is 62 counts from the rail — inside its own
# non-linear region — yet counted as "good", so when its partner was railed the
# code trusted the bad one and produced a stable-but-wrong angle.
DEADZONE_RAW = 100

# While BOTH wipers are on-track they describe the same physical angle, 180 deg
# apart, so their two estimates must agree. Mechanical play and wiper tolerance
# are a few degrees; a disagreement of tens of degrees means the channels are
# reading different sensors (bad mux address bit, floating/crosstalk channel,
# broken wiper). Such samples are rejected rather than reconciled into a
# plausible but wrong angle, which is what made a bad pot jump.
#
# Done in the ANGLE domain on purpose: the raw-count relationship is +-541.6
# depending on which side of the wrap the wiper is on, so a modulo-1023 test is
# wrong near 0/360.
# 20 deg: above the measured noise/spike amplitude so a normal turn is never
# falsely rejected, far below the 140-175 deg disagreement a genuinely
# mis-paired channel shows.
PAIR_TOL_DEG = 20.0

# A counter that gains or loses a step shifts every later address in that scan,
# so the data stored for sensor N actually belongs to a neighbour. Which way it
# goes is diagnostic, and the two muxes being wired in different orders is what
# makes it readable:
#
#   extra step, mid-sweep  -> pots 10..16 ALL show the previous pot's value
#   missed step, mid-sweep -> pots 10..16 ALL show the next pot's value
#   either, at the SWEEP START -> a MIXED pattern (some pots show the previous
#                              neighbour, some the next, some unchanged)
#
# (Measured, not assumed - see the harness notes in PORT_NOTES.md.)
# Because the two muxes disagree for addresses 0..7, one shifted address
# mis-pairs the sensors it touches there, so a slip that starts in the first
# half shows up as several inconsistent pairs at once - a far louder signal than
# the single-pair noise the per-pot check deals with. Above this many bad pairs
# the whole scan is treated as address-corrupt and thrown away rather than
# allowed to poison the values.
SHIFT_BAD_PAIRS = 3
SCAN_RETRIES = 3

# Hard cap on how long the on-device self-test may block. One raw sweep is ~3ms
# at the default 100us mux settle, but mux_settle and spi_baud are both tunable
# at runtime and the settle dominates: at 1000us settle + 100kHz SPI a sweep is
# ~22ms, and a hand-set settle of tens of ms turns the whole test into minutes
# of silence that looks exactly like a hang. The test therefore scales its own
# work down to fit, and refuses outright if even the minimum cannot fit.
SELFTEST_BUDGET_MS = 2000
SELFTEST_MIN_SWEEPS = 4

# The pair check cannot see a slip in the SECOND half of the sweep: for
# addresses 8..15 both muxes select the same sensor (see MUX0_MAP/MUX1_MAP), so
# a one-step shift still yields a perfectly consistent pair - it is just filed
# under the neighbouring pot, which is exactly the "value jumps to the previous
# pot" symptom. Those addresses are therefore re-read and compared.
VERIFY_TOL_RAW = 30
VERIFY_BAD_MAX = 1
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
# Measured on the sample board: the raw counts drift about +-10 (3.3 deg), so
# peak-to-peak is ~6.6 deg. A 5 deg threshold sat inside that and emitted
# spurious deltas while the knobs were untouched; 7 deg clears it while still
# tracking a 90-degree turn to within 0.001 turns. Lower this
# (rt.set_pot_deadband) once the panel is reflowed if the signal is clean -
# every degree here is one degree of end-of-turn lag.
DELTA_DEADBAND_DEG = 7.0

# Rotation per scan that no hand can produce. At 50Hz a 90-degree step would be
# 25 turns/second. Anything bigger is a sensing glitch (typically the two wipers
# being reconciled 180 degrees apart), so it is dropped and counted instead of
# slammed into a parameter. `debug()` exposes the count.
GLITCH_DEG = 90.0


def _median3(a, b, c):
    """Median of three, tolerating missing history (None -> return c).

    A single corrupted ADC read is the dominant failure on this board (a stale
    wiper count, or both channels railed for one scan). The median of the last
    three samples discards it without adding latency to real movement: a ramp
    passes through unchanged, a one-sample spike disappears.
    """
    if a is None or b is None:
        return c
    if a > b:
        a, b = b, a
    if b > c:
        b = c
    if a > b:
        b = a
    return b


def _ticks_diff(a, b):
    """Wrap-safe tick difference."""
    return time.ticks_diff(a, b)


def _circ_diff(a, b):
    """Shortest angular distance between two angles in degrees (0..180)."""
    d = (a - b) % 360.0
    if d > 180.0:
        d = 360.0 - d
    return d

# MUX0_MAP[addr] = sensor number (1-based) routed to MCP3002 CH0 (phase 0)
# MUX1_MAP[addr] = sensor number (1-based) routed to MCP3002 CH1 (phase 1)
MUX0_MAP = (8, 7, 6, 5, 4, 3, 2, 1, 16, 15, 14, 13, 12, 11, 10, 9)
MUX1_MAP = (1, 2, 3, 4, 5, 6, 7, 8, 16, 15, 14, 13, 12, 11, 10, 9)

# Counter address that selects each sensor (1-based) on each mux. A pot's two
# wipers live at DIFFERENT addresses because the muxes are wired in different
# orders, which is why single-pot mode has to seek twice.
_ADDR0 = tuple(MUX0_MAP.index(s) for s in range(1, NUM_POTS + 1))
_ADDR1 = tuple(MUX1_MAP.index(s) for s in range(1, NUM_POTS + 1))

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
        self._drx = bytearray(2)
        # |real - dummy| of the last conversion: how much the reading moves
        # between two samples taken 20us apart. Large values mean the node is
        # still settling (or is noisy); zero means the ADC/pot are quiet.
        self._last_delta = 0
        self._sd0 = [0] * NUM_POTS
        self._sd1 = [0] * NUM_POTS
        # Count of sweeps thrown away because the counter appeared to slip.
        self._scan_retries = 0
        self._v0 = [0] * NUM_POTS      # second-pass raws (reallocated never)
        self._v1 = [0] * NUM_POTS
        self._verify = bool(cfg_entry.get("verify_sweeps", True))
        self._verify_tol = int(cfg_entry.get("verify_tol", VERIFY_TOL_RAW))
        self._verify_fallbacks = 0
        # Knob values 0..1 (angle / 360), exposed via knobs().
        self.values = [0.0] * NUM_POTS
        # Rotation accumulation for relative mode (see DELTA_DEADBAND_DEG).
        self._anchor = [None] * NUM_POTS   # last angle a delta was emitted at
        # Last two raw samples per wiper, for the median filter.
        self._r0p1 = [None] * NUM_POTS
        self._r0p2 = [None] * NUM_POTS
        self._r1p1 = [None] * NUM_POTS
        self._r1p2 = [None] * NUM_POTS
        self._delta = [0.0] * NUM_POTS     # pending turns since last read
        self._has_delta = False
        self._glitches = 0
        self._stale = [False] * NUM_POTS   # waiting for a valid pair again
        self._pair_err = [0] * NUM_POTS   # rejected pair count per pot
        # Last raw pair actually used for each accepted angle (for debug output).
        self._used0 = [0] * NUM_POTS
        self._used1 = [0] * NUM_POTS
        # Odd-indexed pots are mounted on the back of the board and turn the
        # other way; flip their deltas so every knob feels the same.
        self._reverse_odd = bool(cfg_entry.get("reverse_odd_pots", True))
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
        # Scan pacing, adjustable at runtime (the pot scope exposes it): None
        # means stopped, which parks the mux so a channel can be probed with a
        # meter while it is not being switched.
        self._scan_period_ms = SCAN_PERIOD_MS
        self._scan_saved_ms = SCAN_PERIOD_MS
        # None = visit every address; an index = only poll that pot.
        self._single_pot = None
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
        # Timing knobs, overridable per board in daughterboard.json: if the
        # panel is glitchy, try a slower SPI clock (signal integrity over the
        # FFC) or a longer mux settle (analog settling) before changing hardware.
        try:
            self._mux_settle_us = int(self.cfg.get("mux_settle_us", MUX_SETTLE_US))
        except (TypeError, ValueError):
            self._mux_settle_us = MUX_SETTLE_US
        try:
            self._spi_baud = int(self.cfg.get("spi_baudrate", 1000000))
        except (TypeError, ValueError):
            self._spi_baud = 1000000
        self._spi = self.slot.spi(sck="D", mosi="B", miso="C", baudrate=self._spi_baud)
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
        # Dummy transaction to clear internal state and settle the mux. Its
        # result is kept, not discarded: the difference between it and the real
        # conversion is a direct measurement of residual settling.
        self._cs.value(0)
        self._spi.write_readinto(self._tx, self._drx)
        self._cs.value(1)
        time.sleep_us(ADC_SETTLE_US)
        # Real transaction.
        self._cs.value(0)
        self._spi.write_readinto(self._tx, self._rx)
        self._cs.value(1)
        # b0 contains 6 leading zeros + B9, B8; b1 contains B7..B0.
        dummy = ((self._drx[0] & 0x03) << 8) | self._drx[1]
        real = ((self._rx[0] & 0x03) << 8) | self._rx[1]
        self._last_delta = real - dummy
        return real

    # --- 74HC393D counter ---

    def _reset_counter(self):
        self._rst.value(1)
        time.sleep_us(10)
        self._rst.value(0)
        time.sleep_us(self._mux_settle_us)

    def _pulse_counter(self):
        # 74HC393 counts on the falling edge of CLK.
        self._clk.value(1)
        time.sleep_us(10)
        self._clk.value(0)
        time.sleep_us(self._mux_settle_us)

    def _seek(self, addr):
        """Reset the counter and step to `addr`, settling only at the target.

        Passing THROUGH addresses costs nothing electrically (nothing reads
        them), so intermediate steps skip the settle and the caller settles
        once before reading. That is what makes single-pot polling cheap.
        """
        self._reset_counter()
        for _ in range(addr):
            self._clk.value(1)
            time.sleep_us(10)
            self._clk.value(0)
            time.sleep_us(10)
        time.sleep_us(self._mux_settle_us)

    # --- angle math (ported from computeAngle) ---

    def _compute_angle(self, raw0, raw1):
        ang0 = (raw0 / 1023.0) * WIPER_RANGE_DEG           # 0..340
        ang1 = (raw1 / 1023.0) * WIPER_RANGE_DEG + 180.0   # 180..520
        if ang1 >= 360.0:
            ang1 -= 360.0
        # A dead wiper reads near the ADC rails.
        w0_rails = raw0 < DEADZONE_RAW or raw0 > (1023 - DEADZONE_RAW)
        w1_rails = raw1 < DEADZONE_RAW or raw1 > (1023 - DEADZONE_RAW)
        # Wrap-aware disagreement between the two estimates of the same angle.
        diff = ang1 - ang0
        if diff > 180.0:
            diff -= 360.0
        elif diff < -180.0:
            diff += 360.0
        # Both wipers on-track: a large disagreement cannot be mechanical play,
        # so the pair is reading different sensors. Reject before reconciling.
        if not w0_rails and not w1_rails:
            if diff > PAIR_TOL_DEG or diff < -PAIR_TOL_DEG:
                return None
        # Do the two angles agree closely (within 2 deg)?
        if diff < 2.0 and diff > -2.0:
            return ang0 + diff * 0.5
        # Disagree: one wiper is in its deadzone.
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
        # Both wipers in (or near) their dead zones. At a genuine boundary angle
        # only ONE wiper is dead, so this is a corrupt pair, not a position.
        return None

    # --- scan ---

    def scan(self):
        """One scan: either every address, or just the selected pot.

        Call at a paced rate (§8). Full mode is 32 SPI reads + settles (~3.4ms);
        single-pot mode touches only that pot's two addresses.
        """
        if self._single_pot is None:
            self._scan_all()
        else:
            self._scan_one(self._single_pot)
        if self._leds and self._led_mode == "auto":
            self._update_leds_auto()

    def _read_all_raw(self, reset=True):
        """One sweep of every address, into _phase0/_phase1. No derived state.

        Address contract: reset to 0, then EXACTLY 15 pulses, so the counter
        walks 0..15 and never wraps - the last pulse is deliberately skipped and
        the counter PARKS at 15. Reaching address 0 again therefore depends
        entirely on _reset_counter() at the top of the next sweep; nothing in
        the sweep itself returns the counter to 0. A stop()/scan() sequence that
        never re-resets would start the next sweep at 15.

        `reset=False` sweeps from wherever the counter is - used by the
        self-test to build a deliberate offset, never by the scan path.
        """
        if reset:
            self._reset_counter()  # address 0
        for addr in range(NUM_POTS):
            ch1 = self._read_adc(1)
            ch0 = self._read_adc(0)
            self._phase0[MUX0_MAP[addr] - 1] = ch0
            self._sd0[MUX0_MAP[addr] - 1] = self._last_delta
            self._phase1[MUX1_MAP[addr] - 1] = ch1
            self._sd1[MUX1_MAP[addr] - 1] = self._last_delta
            if addr < NUM_POTS - 1:
                self._pulse_counter()

    def _pair_ok(self, raw0, raw1):
        """Side-effect-free version of the pair test (see _compute_angle)."""
        w0 = raw0 < DEADZONE_RAW or raw0 > (1023 - DEADZONE_RAW)
        w1 = raw1 < DEADZONE_RAW or raw1 > (1023 - DEADZONE_RAW)
        if w0 and w1:
            return False          # at a real boundary only ONE wiper is dead
        if w0 or w1:
            return True           # the live wiper carries the angle
        a0 = (raw0 / 1023.0) * WIPER_RANGE_DEG
        a1 = ((raw1 / 1023.0) * WIPER_RANGE_DEG + 180.0) % 360.0
        d = a1 - a0
        if d > 180.0:
            d -= 360.0
        elif d < -180.0:
            d += 360.0
        return -PAIR_TOL_DEG <= d <= PAIR_TOL_DEG

    def _bad_pair_count(self):
        n = 0
        for i in range(NUM_POTS):
            if not self._pair_ok(self._phase0[i], self._phase1[i]):
                n += 1
        return n

    def _verify_upper(self):
        """Re-read addresses 8..15 and count how many disagree with pass one.

        A slipped counter changes every read after the extra or missed edge, so
        two passes over the same addresses cannot agree while a pot is static.
        This is the only check that can catch a slip in the half where the two
        muxes happen to select the same sensor.
        """
        self._seek(8)
        for addr in range(8, NUM_POTS):
            ch1 = self._read_adc(1)
            ch0 = self._read_adc(0)
            self._v0[MUX0_MAP[addr] - 1] = ch0
            self._v1[MUX1_MAP[addr] - 1] = ch1
            if addr < NUM_POTS - 1:
                self._pulse_counter()
        bad = 0
        for i in range(8, NUM_POTS):
            d0 = self._phase0[i] - self._v0[i]
            d1 = self._phase1[i] - self._v1[i]
            if d0 < 0:
                d0 = -d0
            if d1 < 0:
                d1 = -d1
            if d0 > self._verify_tol or d1 > self._verify_tol:
                bad += 1
            else:
                # Both passes agree: average them, which also halves the noise.
                self._phase0[i] = (self._phase0[i] + self._v0[i]) // 2
                self._phase1[i] = (self._phase1[i] + self._v1[i]) // 2
        return bad

    def _scan_all(self):
        # Read, validate the whole sweep, and only then commit it. A missed
        # counter step shifts the data by one sensor: the pair check catches
        # that in the first half, and the second half is re-read and compared.
        for _attempt in range(SCAN_RETRIES):
            self._read_all_raw()
            if self._bad_pair_count() > SHIFT_BAD_PAIRS:
                self._scan_retries += 1
                continue
            if self._verify and self._verify_upper() > VERIFY_BAD_MAX:
                self._scan_retries += 1
                continue
            for i in range(NUM_POTS):
                self._update_pot(i)
            return True
        # Every attempt looked address-corrupt. Commit the last sweep anyway
        # rather than nothing: a board that trips the checks every time would
        # otherwise freeze all 16 pots, which is a worse failure than a jump the
        # 3-sample median absorbs. `verify_fallbacks()` counts these.
        self._verify_fallbacks += 1
        for i in range(NUM_POTS):
            self._update_pot(i)
        return False

    def _scan_one(self, n):
        """Poll a single pot: seek to each of its two addresses and read it.

        The mux only ever visits those two positions, so a probe on that
        channel sees a stable selection instead of a 50Hz sweep. Other pots keep
        their last values.
        """
        self._seek(_ADDR0[n])
        self._phase0[n] = self._read_adc(0)
        self._sd0[n] = self._last_delta
        self._seek(_ADDR1[n])
        self._phase1[n] = self._read_adc(1)
        self._sd1[n] = self._last_delta
        self._update_pot(n)

    def _update_pot(self, i):
        r0 = self._phase0[i]
        r1 = self._phase1[i]
        m0 = _median3(self._r0p2[i], self._r0p1[i], r0)
        m1 = _median3(self._r1p2[i], self._r1p1[i], r1)
        self._r0p2[i] = self._r0p1[i]
        self._r0p1[i] = r0
        self._r1p2[i] = self._r1p1[i]
        self._r1p1[i] = r1

        ang = self._compute_angle(m0, m1)
        resync = False
        if ang is None:
            # Pair rejected: hold the last good angle so the knob does not
            # jump, and count it. A climbing count for one pot is a hardware
            # fault report, not noise.
            self._pair_err[i] += 1
            self._stale[i] = True
            ang = self.values[i] * 360.0
        elif self._stale[i]:
            # RECOVERING from a bad stretch. Do not treat the catch-up as a
            # glitch: holding here left the anchor behind, so every later
            # sample also looked like a jump and the pot stayed stuck while
            # the real value moved on (the "wiper stops tracking" symptom).
            # Re-seed instead, and emit no delta for the jump.
            self._stale[i] = False
            resync = True
            self._used0[i] = m0
            self._used1[i] = m1
        elif self._anchor[i] is not None:
            # Temporal plausibility: nothing a hand can do moves 90 deg in
            # one 20ms scan.
            dd = ang - self.values[i] * 360.0
            if dd > 180.0:
                dd -= 360.0
            elif dd < -180.0:
                dd += 360.0
            if dd >= GLITCH_DEG or dd <= -GLITCH_DEG:
                self._glitches += 1
                self._stale[i] = True
                ang = self.values[i] * 360.0
            else:
                self._used0[i] = m0
                self._used1[i] = m1
        else:
            self._used0[i] = m0
            self._used1[i] = m1

        self.values[i] = ang / 360.0

        if resync:
            # Position jumped while the sensor was bad: take the new angle
            # as the reference but do NOT turn it into parameter movement.
            self._anchor[i] = ang
            return

        anchor = self._anchor[i]
        if anchor is None:
            self._anchor[i] = ang
            return
        d = ang - anchor
        if d > 180.0:
            d -= 360.0
        elif d < -180.0:
            d += 360.0
        if d >= self._deadband or d <= -self._deadband:
            turns = d / 360.0
            # Odd-indexed pots sit on the back of the board, so their
            # rotation is mechanically inverted. Flip the DELTA rather than
            # the angle: the angle stays the physical position (what the
            # scope and the pair checks reason about).
            if self._reverse_odd and (i & 1):
                turns = -turns
            self._delta[i] += turns
            self._has_delta = True
            self._anchor[i] = ang

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
        period = self._scan_period_ms
        if period is None:
            return
        last = self._last_scan_ms
        if last is not None and _ticks_diff(now_ms, last) < period:
            return
        self._last_scan_ms = now_ms
        self.scan()

    def knobs(self):
        """16 knob positions 0..1 (absolute angle / 360), for the knob map."""
        return self.values

    def set_spi_baud(self, baud):
        """Change the SPI clock live.

        The MCP3002 samples for 1.5 clock periods, so the clock rate IS the
        sample window: 1.5us at 1MHz, 3.75us at 400kHz, 15us at 100kHz. If a
        wiper's source impedance is high, the sample capacitor cannot charge
        from the newly selected channel in time and the reading is pulled toward
        whatever the mux was showing before - which is exactly a disturbance
        introduced by moving the mux rather than by the pot itself.
        """
        baud = int(baud)
        if baud < 10000:
            baud = 10000
        self._spi_baud = baud
        if self._spi is not None:
            try:
                self._spi.init(baudrate=baud)
            except Exception:
                try:
                    self._spi.deinit()
                except Exception:
                    pass
                self._spi = self.slot.spi(sck="D", mosi="B", miso="C",
                                          baudrate=baud)
        return baud

    def spi_baud(self):
        return self._spi_baud

    def verify_fallbacks(self):
        """Sweeps committed despite failing verification (noisy board / slips
        on every attempt). A rising count means the checks are not buying
        anything here; see also scan_retries()."""
        return self._verify_fallbacks

    def scan_retries(self):
        """Sweeps discarded because too many pairs disagreed (address slip)."""
        return self._scan_retries

    # --- on-device self-test -------------------------------------------------
    #
    # Everything here allocates and blocks; it is a diagnostic, never part of
    # the scan path. The point is to answer "where is the wrong value?" with
    # only the panel attached - no scope, no host.

    def _capture(self, reset=True, extra=0):
        """One raw sweep as per-pot angles (None where the pair was rejected).

        No median, no anchors, no deltas: safe to call between normal scans.
        `extra` pulses the counter after the reset, which builds a deliberate
        whole-sweep offset - the controlled version of a counter slip.
        """
        if reset:
            self._reset_counter()
        for _ in range(extra):
            self._pulse_counter()
        self._read_all_raw(reset=False)
        out = []
        for i in range(NUM_POTS):
            out.append(self._compute_angle(self._phase0[i], self._phase1[i]))
        return out

    @staticmethod
    def _classify(ref, got, tol=6.0):
        """Compare two captures pot by pot.

        `prev`/`next` count pots now reading the neighbouring pot's angle - the
        fingerprint of an address slip. `other` counts pots whose pair was
        rejected or that match nothing, which is what a sensing fault
        (railed/pinned wiper, dead channel) looks like instead.
        """
        same = prev = nxt = other = 0
        for i in range(NUM_POTS):
            g = got[i]
            if g is None:
                other += 1
                continue
            r = ref[i]
            if r is not None and _circ_diff(g, r) <= tol:
                same += 1
                continue
            rp = ref[(i - 1) % NUM_POTS]
            rn = ref[(i + 1) % NUM_POTS]
            if rp is not None and _circ_diff(g, rp) <= tol:
                prev += 1
            elif rn is not None and _circ_diff(g, rn) <= tol:
                nxt += 1
            else:
                other += 1
        return {"same": same, "prev": prev, "next": nxt, "other": other}

    def _consistency(self):
        """Pots whose committed angle does not match the raw pair it came from.

        values[i] is always _compute_angle(m0, m1)/360 with m0/m1 also stored in
        _used0/_used1, so recomputing from _used0/_used1 must reproduce it. A
        mismatch means values[] has diverged from the measurement - the wrong
        number being shown for the right reading - which is a software fault,
        not a counter or wiring fault. Returns [(pot 1-based, shown, from raws)].
        """
        bad = []
        for i in range(NUM_POTS):
            got = self._compute_angle(self._used0[i], self._used1[i])
            if got is None:
                continue          # never used a pair yet (or held a reject)
            want = self.values[i] * 360.0
            if _circ_diff(got, want) > 1.0:
                bad.append((i + 1, round(want, 1), round(got, 1)))
        return bad

    @staticmethod
    def _aliases(cap, tol=6.0):
        """Pot pairs currently showing the same angle - candidate aliasing."""
        out = []
        for i in range(NUM_POTS):
            a = cap[i]
            if a is None:
                continue
            for j in range(i + 1, NUM_POTS):
                b = cap[j]
                if b is not None and _circ_diff(a, b) <= tol:
                    out.append((i + 1, j + 1))
        return out

    def sweep_us(self):
        """Modelled duration of one raw sweep in microseconds.

        One address costs two ADC reads (each = two SPI transactions of 16
        bit-times plus ADC_SETTLE_US) plus one counter pulse (10us high plus the
        mux settle). Used only to warn how long the self-test will block - the
        74HC393's settle is the dominant term and it is tunable at runtime.
        """
        per_read = (2 * 8 * 1000000.0) / self._spi_baud + ADC_SETTLE_US
        return int(16 * (2 * per_read + self._mux_settle_us + 10))

    def _consensus_capture(self, n=5):
        """The capture the board agrees with itself about.

        One reference is not enough: if the counter slips during the reference
        sweep, every later comparison comes out inverted (the clean sweeps look
        like the faulty ones). Take `n` and keep the one matching the most of
        the others. Returns (capture, agreements, n).
        """
        caps = []
        for _ in range(n):
            caps.append(self._capture())
        best = caps[0]
        best_score = -1
        for i in range(n):
            score = 0
            for j in range(n):
                if i != j and self._classify(caps[i], caps[j])["same"] == NUM_POTS:
                    score += 1
            if score > best_score:
                best_score = score
                best = caps[i]
        return best, best_score, n

    def _reseed(self):
        """Re-seed the filter and the anchor from a clean sweep and drop any
        pending rotation, so running a diagnostic cannot itself look like a turn.
        """
        self._read_all_raw()
        for i in range(NUM_POTS):
            self._r0p2[i] = self._r0p1[i] = self._phase0[i]
            self._r1p2[i] = self._r1p1[i] = self._phase1[i]
            self._used0[i] = self._phase0[i]
            self._used1[i] = self._phase1[i]
            ang = self._compute_angle(self._phase0[i], self._phase1[i])
            if ang is None:
                ang = self.values[i] * 360.0
            self.values[i] = ang / 360.0
            self._anchor[i] = ang
            self._stale[i] = False
            self._delta[i] = 0.0
        self._has_delta = False

    def selftest(self, sweeps=24, stress=40, tol=6.0,
                 budget_ms=SELFTEST_BUDGET_MS, progress=None):
        """Diagnose the pot path on-device and return a report dict.

        `progress(msg)` is called at every phase boundary. This blocks for
        hundreds of ms (seconds if the mux settle / SPI clock were tuned up), so
        a caller that reports to a human should pass one.

        Leave the panel untouched: every comparison is against a reference
        capture, so a hand on a knob looks like a fault. Runs, in order:

          1. values[] vs the raws it came from   (software layer)
          2. `sweeps` untouched sweeps, classified (counter layer)
          3. deliberate +1 offset and a parked-15 sweep (what a slip LOOKS like
             on this board - the positive control)
          4. `stress` sweeps' worth of raw counter traffic, then a re-read
             (is the counter line happy under load?)

        A clean board gives {"clean": sweeps, "prev": 0, "next": 0, ...} with an
        empty mismatched_values, while the positive controls still report
        `prev`/`next` - that is the test proving itself.
        """
        def say(msg):
            if progress is not None:
                progress(msg)

        out = {}
        out["scan"] = (self._spi_baud, self._mux_settle_us, self._verify,
                       self._verify_tol)

        # Fit the requested work into the budget BEFORE doing any of it.
        per_sweep_us = self.sweep_us()
        per_stress_us = 17 * (self._mux_settle_us + 10)
        # Fit to 80% of the budget: sweep_us() models a raw sweep and does not
        # include the verification re-read that scan() also performs, so it
        # reads ~15% low (measured against a sleep/bit-time model of the whole
        # run).
        room = int(budget_ms * 8 // 10) * 1000 - 8 * per_sweep_us
        want_sweeps = int(sweeps)
        want_stress = int(stress)
        run_sweeps = min(want_sweeps, max(0, room // per_sweep_us))
        room -= run_sweeps * per_sweep_us
        run_stress = min(want_stress, max(0, room // per_stress_us))
        out["budget_ms"] = int(budget_ms)
        out["scaled"] = (run_sweeps, want_sweeps, run_stress, want_stress)
        out["sweep_us"] = per_sweep_us
        if run_sweeps < SELFTEST_MIN_SWEEPS:
            out["error"] = (
                "one sweep costs %d us at settle %d us / spi %d Hz, so even %d"
                " sweeps blow the %d ms budget. Fix the timing knobs first"
                " (rt.pot_settle() / rt.set_pot_spi_baud()); at this cost a scan"
                " cannot keep up with its own pacing either."
                % (per_sweep_us, self._mux_settle_us, self._spi_baud,
                   SELFTEST_MIN_SWEEPS, int(budget_ms)))
            return out

        say("checking values[] against the raws it came from")
        # Inspect the LIVE state first: a scan recomputes values[] from the
        # raws, which would erase exactly the divergence we are looking for.
        out["mismatched_values"] = self._consistency()
        out["pots"] = self.debug()
        self.scan()

        say("taking 5 reference sweeps (they must agree with each other)")
        ref, agree, nref = self._consensus_capture()
        out["reference_agreement"] = (agree, nref - 1)
        out["reference"] = [None if a is None else round(a, 1) for a in ref]
        out["aliases"] = self._aliases(ref, tol)
        # Must be taken IMMEDIATELY after a reset sweep: that one leaves the
        # counter parked at 15, so sweeping without a reset reads address-1 for
        # every address. Taken later it would start at 0 by accident (a +1
        # capture ends wrapped) and look falsely clean.
        out["ctrl_parked15"] = self._classify(ref, self._capture(reset=False),
                                              tol)

        say("census: %d untouched sweeps, %d ms each" % (
            run_sweeps, per_sweep_us // 1000))
        clean = 0
        classes = {}
        examples = []
        for _ in range(run_sweeps):
            got = self._capture()
            c = self._classify(ref, got, tol)
            if c["same"] == NUM_POTS:
                clean += 1
            else:
                key = "%d/%d/%d" % (c["prev"], c["next"], c["other"])
                classes[key] = classes.get(key, 0) + 1
                if len(examples) < 3:
                    c["pots"] = [None if a is None else round(a, 1)
                                 for a in got]
                    examples.append(c)
        out["sweeps"] = run_sweeps
        out["clean"] = clean
        out["classes"] = classes       # "prev/next/other" -> how many sweeps
        out["examples"] = examples

        say("positive controls: +1 offset and a parked counter")
        # Positive control: a deliberate whole-sweep +1 offset, the controlled
        # version of a counter that took an extra edge.
        out["ctrl_plus1"] = self._classify(ref, self._capture(extra=1), tol)

        say("stress: %d rounds of reset+16 pulses" % run_stress)
        for _ in range(run_stress):
            self._reset_counter()
            for _ in range(NUM_POTS):
                self._pulse_counter()
        out["stress"] = run_stress
        out["stress_after"] = self._classify(ref, self._capture(), tol)

        out["retries"] = self._scan_retries
        out["fallbacks"] = self._verify_fallbacks
        out["pair_errors"] = [self._pair_err[i] for i in range(NUM_POTS)]
        say("restoring the filter state")
        self._reseed()
        return out

    def settle_delta(self, pot):
        """(d0, d1) = |real - dummy| of the last conversion of each wiper."""
        return (self._sd0[pot], self._sd1[pot])

    def set_mux_settle(self, us):
        """Settle time after a mux address change, live (diagnostic)."""
        us = int(us)
        if us < 0:
            us = 0
        self._mux_settle_us = us
        return us

    def mux_settle(self):
        return self._mux_settle_us

    def set_single_pot(self, pot):
        """Poll only `pot` (0-based), or None to scan every address.

        In single-pot mode the mux only ever seeks that pot's two addresses, so
        a probe on its channel sees a stable selection instead of a 50Hz sweep.
        """
        if pot is None:
            self._single_pot = None
        else:
            pot = int(pot)
            if pot < 0 or pot >= NUM_POTS:
                pot = 0
            self._single_pot = pot
        return self._single_pot

    def single_pot(self):
        return self._single_pot

    def set_scan_period(self, ms):
        """Scan every `ms` milliseconds, or None to stop scanning entirely.

        Stopping leaves the mux counter where it is, so a scope/meter can look
        at a settled channel instead of one that switches every few ms.
        """
        if ms is None:
            if self._scan_period_ms is not None:
                self._scan_saved_ms = self._scan_period_ms
            self._scan_period_ms = None
        else:
            ms = int(ms)
            if ms < 1:
                ms = 1
            self._scan_period_ms = ms
            self._scan_saved_ms = ms
        return self._scan_period_ms

    def scan_period(self):
        return self._scan_period_ms

    def scan_enabled(self):
        return self._scan_period_ms is not None

    def scan_once(self):
        """Force one scan regardless of the pacing/stop state."""
        self.scan()
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
                        self._used0[i], self._used1[i],
                        round(self.values[i] * 360.0, 1), self._pair_err[i]))
        return out

    def pair_errors(self, pot):
        """Pairs rejected for `pot` (see PAIR_OFFSET_RAW)."""
        return self._pair_err[pot]

    def timing(self):
        """(spi_baudrate, mux_settle_us) currently in use."""
        return (self._spi_baud, self._mux_settle_us)

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