# cynthia/ads.py
# Mainboard ADS1015/1115 manager (§5.3) — selective polling.
#
# One shared ADS (I2C_0, addr 0x48), 4 channels:
#   AIN0 = slot0.adc.A   AIN1 = slot0.adc.B   AIN2 = slot1.adc.A   AIN3 = slot1.adc.B
#
# Only channels at least one driver subscribed to are polled — fewer active
# channels = higher per-channel rate. If nothing is subscribed, poll() idles
# (no I2C traffic). Drivers subscribe in start(), unsubscribe in stop().
#
# ads1015 (12-bit, up to 3300 SPS) vs ads1115 (16-bit, up to 860 SPS): same
# I2C register map; the data-rate config and result interpretation differ
# (ADS1015 result is 12-bit left-justified in the 16-bit register -> >>4).
# Chip is configured via daughterboard.json ("adc_chip"), default ads1115.
#
# §8.4: the poll source is swappable — values live in Handle.value, updated
# by poll(); a C FreeRTOS task could update the same handles later without
# changing any driver API.

from . import pins

# ADS1x15 registers
_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# CONFIG register fields
_MUX = {0: 0x4000, 1: 0x5000, 2: 0x6000, 3: 0x7000}  # AINx vs GND
_MODE_CONTINUOUS = 0x0000
_MODE_SINGLE = 0x0100   # single-shot (matches Arduino ADS1115_MODE_SINGLE)
_PGA_4096 = 0x0400      # +/-4.096V range
_OS_SINGLE = 0x8000     # start a single conversion

# Data rates (SPS) -> CONFIG DR bits
_DR_ADS1115 = {8: 0x0000, 16: 0x0020, 32: 0x0040, 64: 0x0060,
               128: 0x0080, 250: 0x00A0, 475: 0x00C0, 860: 0x00E0}
_DR_ADS1015 = {128: 0x0000, 250: 0x0020, 490: 0x0040, 920: 0x0060,
               1600: 0x0080, 2400: 0x00A0, 3300: 0x00C0}


class Handle:
    """Subscription handle; .value is kept fresh by ADSManager.poll()."""
    __slots__ = ("channel", "owner", "value")

    def __init__(self, channel, owner):
        self.channel = channel
        self.owner = owner
        self.value = 0.0


class ADSManager:
    def __init__(self, i2c, addr=pins.ADS1115_ADDR, chip="ads1115", rate=None):
        self.i2c = i2c
        self.addr = addr
        self.chip = chip
        self._subs = {}       # channel -> list[Handle]
        self._order = []      # subscribed channels in round-robin order
        self._rr = 0
        self._started = False  # a conversion is in flight (poll pipeline)
        self._last_ch = 0
        if rate is None:
            # Arduino reference runs the ADS1115 at 860 SPS (delay(2) settle).
            rate = 860 if chip == "ads1115" else 1600
        dr_table = _DR_ADS1115 if chip == "ads1115" else _DR_ADS1015
        self._dr_bits = dr_table.get(rate, 0x0080)
        self._shift = 4 if chip == "ads1015" else 0
        self._buf2 = bytearray(2)   # pre-allocated, zero-alloc poll path (§8)
        # Probe the chip once at construction. Some CYNTHIA boards have no
        # ADS populated (or it lives at a different address); a missing chip
        # must not raise ENODEV on every poll and take the whole surface down
        # — the keyboard/buttons live on the same poll loop. If the chip is
        # absent, mark ourselves disabled and print what IS on the bus so it
        # is diagnosable; poll()/convert_once() then no-op.
        self._present = self._probe()
        if not self._present:
            try:
                found = self.i2c.scan()
            except OSError:
                found = []
            print("cynthia.ads: no ADS1x15 at 0x%02x (bus scan: %s) — "
                  "analog inputs disabled" % (addr,
                  [hex(a) for a in found]))
        else:
            print("cynthia.ads: ADS1x15 found at 0x%02x (%s)" % (addr, chip))

    def _probe(self):
        """True if the ADS acknowledges its address. i2c.scan() is more
        reliable than a 0-byte write (some chips NAK zero-length writes)."""
        try:
            return self.addr in self.i2c.scan()
        except OSError:
            return False

    def subscribe(self, channel, owner=None):
        """Subscribe to ADS channel 0..3; returns a Handle with .value."""
        if channel not in (0, 1, 2, 3):
            raise ValueError("ADS channel must be 0..3")
        h = Handle(channel, owner)
        if channel not in self._subs:
            self._subs[channel] = []
            self._order.append(channel)
        self._subs[channel].append(h)
        return h

    def unsubscribe(self, handle):
        subs = self._subs.get(handle.channel)
        if subs and handle in subs:
            subs.remove(handle)
            if not subs:
                del self._subs[handle.channel]
                self._order.remove(handle.channel)
                if self._order:
                    self._rr %= len(self._order)
                else:
                    self._rr = 0

    def _read_raw(self, channel, settle_ms):
        """Start a single-shot conversion on `channel`, wait, return signed raw."""
        if not self._present:
            return 0
        import time
        cfg = _OS_SINGLE | _MUX[channel] | _PGA_4096 | self._dr_bits | _MODE_SINGLE | 0x0003
        self._buf2[0] = cfg >> 8
        self._buf2[1] = cfg & 0xFF
        self.i2c.writeto_mem(self.addr, _REG_CONFIG, self._buf2)
        time.sleep_ms(settle_ms)
        self.i2c.readfrom_mem_into(self.addr, _REG_CONVERSION, self._buf2)
        raw = (self._buf2[0] << 8) | self._buf2[1]
        if raw >= 0x8000:
            raw -= 0x10000
        return raw >> self._shift

    def _norm(self, raw, vref):
        """Signed raw counts -> volts/vref, clamped 0..1. PGA is +/-4.096V,
        so full-scale positive raw = 4.096V (matches the Arduino prototype's
        `raw * 0.125mV / vref` normalization)."""
        full = 0x7FFF >> self._shift
        v = (raw / full) * 4.096 / vref
        if v < 0.0:
            v = 0.0
        elif v > 1.0:
            v = 1.0
        return v

    def convert_once(self, channel, settle_ms=2, vref=3.3):
        """Synchronous single-shot conversion on `channel` -> 0..1.

        For drivers that must interleave mux-address changes with reads
        (spark_pots_legacy) and so can't use the subscription poller.
        Blocks ~settle_ms (conversion time at the configured data rate).
        `vref` is the pot supply voltage (3.3V on the legacy pot board).
        Returns 0.0 when no ADS is present (degraded mode).
        """
        if not self._present:
            return 0.0
        return self._norm(self._read_raw(channel, settle_ms), vref)

    def poll(self):
        """One round-robin conversion step. Call at a paced rate (§8, never
        busy-loop). Idles (no I2C) when nothing is subscribed.

        Reads the PREVIOUS conversion's result (started by the prior poll
        call), then kicks off the next one — so a paced poll at <= the data
        rate never waits on I2C, and values are at most one poll stale.
        Normalizes against 4.096V like the Arduino prototype does for the
        joystick axes (0..1 over 0..4.096V).
        No-ops when no ADS is present (degraded mode).
        """
        if not self._present:
            return
        if not self._order:
            return
        # §8: the poll drives the keyboard/buttons via the same loop; a
        # transient I2C glitch must never raise out of here and kill it.
        try:
            ch = self._order[self._rr % len(self._order)]
            self._rr += 1
            if self._started:
                # Read the result of the conversion started on the previous poll.
                self.i2c.readfrom_mem_into(self.addr, _REG_CONVERSION, self._buf2)
                raw = (self._buf2[0] << 8) | self._buf2[1]
                if raw >= 0x8000:
                    raw -= 0x10000
                v = self._norm(raw >> self._shift, 4.096)
                for h in self._subs[self._last_ch]:
                    h.value = v
            # Start the next conversion.
            cfg = _OS_SINGLE | _MUX[ch] | _PGA_4096 | self._dr_bits | _MODE_SINGLE | 0x0003
            self._buf2[0] = cfg >> 8
            self._buf2[1] = cfg & 0xFF
            self.i2c.writeto_mem(self.addr, _REG_CONFIG, self._buf2)
            self._started = True
            self._last_ch = ch
        except OSError:
            # Bus hiccup; skip this poll step. Values stay at last good read.
            self._started = False


# Module-level singleton, created by setup() once the I2C bus exists.
_manager = None


def setup(chip="ads1115"):
    """Create the shared ADSManager (call after connector init).

    Board revisions differ on which I2C bus the ADS sits on: the Arduino
    reference (Hardware.h) has it on the display bus (SDA=5/SCL=4) together
    with the SH1106, while pins.h documents it on bus 0 (SDA=17/SCL=16) with
    the MCP23017s. Probe both buses and use whichever acknowledges 0x48.
    """
    global _manager
    from . import connector
    if _manager is None:
        buses = [
            connector.i2c_bus(0, pins.I2C_SCL, pins.I2C_SDA),
            connector.i2c_bus(1, pins.I2C_DISPLAY_SCL, pins.I2C_DISPLAY_SDA),
        ]
        i2c = None
        for b in buses:
            try:
                if pins.ADS1115_ADDR in b.scan():
                    i2c = b
                    break
            except OSError:
                continue
        if i2c is None:
            i2c = buses[0]  # fall back; manager will report itself absent
        _manager = ADSManager(i2c, chip=chip)
    return _manager


def get():
    return _manager