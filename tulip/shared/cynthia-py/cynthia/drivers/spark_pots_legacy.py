# cynthia/drivers/spark_pots_legacy.py
# Spark pot board (legacy) driver (§10.3).
#
# Port of the legacy pot scan in /Users/pasha/projects/synth/src/Hardware.cpp
# (HardwareClass::adsTask + readPotMuxChannel).
#
# Hardware:
#   - 16 regular pots multiplexed into the MAINBOARD ADS1115 (one slot ADC
#     channel). The multiplexer address lines are driven directly by 4 GPIO
#     (binary):
#       MUX_S0 = IO_A_2 -> slot pin A
#       MUX_S1 = IO_B_2 -> slot pin B
#       MUX_S2 = IO_C_2 -> slot pin C
#       MUX_S3 = IO_D_2 -> slot pin D
#     and the mux output feeds the slot's analog pin A (ADS AIN2 for slot 2;
#     Hardware.h: ADS1115_MUX_SIG_CHANNEL = ADC_A_2).
#   - Scan: set the 4-bit address, let the mux settle (~100us), read the ADS
#     channel, normalize to the 3.3V pot supply, EMA-smooth per channel.
#
# I2C-bound: one ADS conversion per pot per scan (~2ms at 860SPS), so a full
# 16-pot scan is ~30-40ms. Poll at ~25-50Hz (§10.3: "I2C-bound, ~50-100Hz"
# is optimistic for all 16 channels; the paced scheduler keeps it honest).

import machine
import time
import tulip

NUM_POTS = 16
MUX_SETTLE_US = 100      # from adsTask: delayMicroseconds(100) after address change
ADS_SETTLE_MS = 2        # from readADS1115Raw: delay(2) at 860SPS
POT_VREF = 3.3           # pot supply; prototype normalizes voltage / 3.3
EMA_ALPHA = 0.2          # prototype POT_EMA_ALPHA (override via cfg "ema")


class Driver:
    def __init__(self, slot, cfg_entry):
        self.slot = slot
        self.cfg = cfg_entry
        self._s = [None, None, None, None]   # MUX_S0..S3
        self._ads = None
        self._ads_ch = 0
        self._ema = self.cfg.get("ema", EMA_ALPHA)
        self.values = [0.0] * NUM_POTS       # smoothed, 0..1
        self._init = [False] * NUM_POTS      # first-read flag per channel
        self._cursor = 0                     # round-robin pot index for poll()

    # --- lifecycle ---

    def start(self):
        if not hasattr(tulip, "cynthia_knobs"):
            self._s[0] = self.slot.pin("A", machine.Pin.OUT)
            self._s[1] = self.slot.pin("B", machine.Pin.OUT)
            self._s[2] = self.slot.pin("C", machine.Pin.OUT)
            self._s[3] = self.slot.pin("D", machine.Pin.OUT)
            from .. import ads
            self._ads = ads.get()
            # The mux output is wired to this slot's analog pin A.
            self._ads_ch = self.slot.adc["A"]

    def stop(self):
        if not hasattr(tulip, "cynthia_knobs"):
            self.slot.release()

    # --- scan ---

    def _set_mux(self, ch):
        self._s[0].value((ch >> 0) & 1)
        self._s[1].value((ch >> 1) & 1)
        self._s[2].value((ch >> 2) & 1)
        self._s[3].value((ch >> 3) & 1)

    def _read_pot(self, ch):
        """Read + EMA-update one pot channel."""
        self._set_mux(ch)
        time.sleep_us(MUX_SETTLE_US)
        v = self._ads.convert_once(self._ads_ch, settle_ms=ADS_SETTLE_MS,
                                   vref=POT_VREF)
        if not self._init[ch]:
            self.values[ch] = v
            self._init[ch] = True
        else:
            self.values[ch] = self._ema * v + (1.0 - self._ema) * self.values[ch]

    def scan(self):
        """One full 16-pot scan; updates self.values (smoothed 0..1).

        Blocks ~32ms (16 x 2ms conversions) — fine for an explicit REPL call,
        but the paced poll uses poll() which reads one pot per call.
        """
        if hasattr(tulip, "cynthia_knobs"):
            self.values = list(tulip.cynthia_knobs())
            return
        if not self._ads:
            return
        for ch in range(NUM_POTS):
            self._read_pot(ch)

    def poll(self, now_ms=None):
        """Read ONE pot per poll (round-robin) so the paced surface loop is
        never blocked for a full 16-pot scan (~32ms). At ~96Hz polling a full
        sweep of all 16 knobs completes in ~160ms — imperceptible for knobs,
        and each poll costs only one ~2ms ADS conversion."""
        if hasattr(tulip, "cynthia_knobs"):
            self.values = list(tulip.cynthia_knobs())
            return
        if not self._ads:
            return
        self._read_pot(self._cursor)
        self._cursor = (self._cursor + 1) % NUM_POTS

    def knobs(self):
        """16 knob positions 0..1 (plain pots — passthrough, no takeover)."""
        if hasattr(tulip, "cynthia_knobs"):
            return list(tulip.cynthia_knobs())
        return self.values