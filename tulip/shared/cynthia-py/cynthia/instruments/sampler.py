# cynthia/instruments/sampler.py
# Sampler instrument for CYNTHIA.
#
# Port of ~/projects/synth/src/InstrumentSampler.cpp, with one deliberate
# difference: the capture happens in C (cynthia_mic.c, because the mic is on its
# own I2S bus that AMY's single-pin-set config cannot reach), but the sample is
# loaded through AMY's *Python* sampler API - `amy.load_sample_bytes` - rather
# than the Arduino's `pcm_load`. That API handles the wire framing, the preset
# numbering and the loop points for us.
#
# Controls (matching the Arduino):
#   knob 1 -> gain 0..7x            (AMY synth volume)
#   knob 2 -> trim start 0..90%     (reloads after the knob stops)
#   knob 3 -> trim end 10..100%
#   button 2 -> record
#
# The sample plays pitched across the keyboard on synth 3, at preset 1024 with C4
# as its root note - the Arduino uses the same values, and 1024 is the first
# user PCM preset in AMY.

import time

import amy
import tulip

SYNTH = 3
PRESET = 1024          # first user PCM preset (AMY loads here)
NUM_VOICES = 8
ROOT = 60              # C4: the recorded pitch, as the Arduino assumes
MIC_RATE = 44100
DEFAULT_SECONDS = 2
GAIN_MAX = 7.0
TRIM_DEBOUNCE_MS = 150
RECORD_BUTTON = 2


class SamplerInstrument:
    """Records the mic into AMY's memory PCM and plays it across the keys.

    Implements the PatchProvider protocol (§12.1): count/name/select/current.
    There is one "patch", the recording.
    """

    def __init__(self):
        self.patch_index = 0
        self._started = False
        self._buf = None            # recorded PCM (16-bit mono LE), kept for trims
        self._length = 0            # samples
        self._rate = MIC_RATE
        self._gain = 3.5
        self._trim_start = 0.0
        self._trim_end = 1.0
        self._trim_pending = False
        self._trim_time = 0
        self._recording = False
        self._status = "press 2 to record"

    # --- PatchProvider (§12.1) ---

    def count(self):
        return 1

    def name(self, i):
        if self._length:
            return "Sample %.1fs" % (self._length / float(self._rate))
        return "Sampler"

    def current(self):
        return 0

    def select(self, i):
        # One patch: re-selecting just re-points the synth at the sample.
        self._setup_synth()

    # --- lifecycle ---

    def start(self):
        self._started = True
        self._setup_synth()

    def stop(self):
        self._started = False
        try:
            amy.send(synth=SYNTH, vel=0.0)
        except Exception:
            pass

    def _setup_synth(self):
        """Point synth 3 at the sample. The Arduino's setupSynthVoices()."""
        try:
            amy.send(osc=0, wave=amy.PCM, preset=PRESET)
            amy.send(synth=SYNTH, patch=PRESET, num_voices=NUM_VOICES,
                     volume=self._gain)
        except Exception as e:
            print("sampler: synth setup failed:", e)

    # --- recording ---

    def record(self, seconds=DEFAULT_SECONDS):
        """Capture `seconds` from the mic and load it as the sample."""
        if not hasattr(tulip, "cynthia_mic_record"):
            self._status = "no mic support in this build"
            return 0
        self._recording = True
        self._status = "recording..."
        try:
            n = tulip.cynthia_mic_record(int(seconds))
            buf = tulip.cynthia_mic_bytes() if n else None
        except Exception as e:
            self._recording = False
            self._status = "mic failed"
            print("sampler: mic capture failed:", e)
            return 0
        self._recording = False
        if not n or not buf:
            self._status = "captured nothing (mic wired?)"
            return 0
        info = None
        try:
            info = tulip.cynthia_mic_info()
        except Exception:
            pass
        if info and info[1]:
            self._rate = info[1]
        self._buf = buf
        self._length = n
        self._trim_start = 0.0
        self._trim_end = 1.0
        self._load()
        self._status = "%.2fs" % (n / float(self._rate))
        return n

    def _load(self):
        """(Re)load the trimmed region into AMY's memory PCM."""
        if not self._buf or not self._length:
            return
        ls = int(self._trim_start * self._length)
        le = int(self._trim_end * self._length)
        if le <= ls:
            le = min(self._length, ls + 1)
        try:
            # 16-bit mono little-endian, so two bytes per sample.
            amy.load_sample_bytes(self._buf[ls * 2:le * 2], preset=PRESET,
                                  midinote=ROOT, loopstart=0, loopend=0,
                                  sr=self._rate)
        except Exception as e:
            self._status = "load failed"
            print("sampler: load_sample_bytes failed:", e)
            return
        self._setup_synth()

    def _maybe_reload(self):
        """Reload a trimmed region once the knob has been still for a moment.

        Trims are destructive - the whole sample is re-sent over the wire - so
        reloading on every knob step would flood AMY and the poll. The Arduino
        debounces the same way (TRIM_DEBOUNCE_MS)."""
        if not self._trim_pending:
            return
        if time.ticks_diff(time.ticks_ms(), self._trim_time) < TRIM_DEBOUNCE_MS:
            return
        self._trim_pending = False
        self._load()

    # --- knobs ---

    def update_pot(self, channel, value):
        self._maybe_reload()
        if channel == 0:
            self._gain = value * GAIN_MAX
            try:
                amy.send(synth=SYNTH, volume=self._gain)
            except Exception:
                pass
        elif channel == 1:
            self._trim_start = value * 0.9
            self._trim_pending = True
            self._trim_time = time.ticks_ms()
        elif channel == 2:
            self._trim_end = 0.1 + value * 0.9
            self._trim_pending = True
            self._trim_time = time.ticks_ms()

    def pot_name(self, channel):
        return ("Gain", "Trim in", "Trim out")[channel] \
            if 0 <= channel < 3 else "Ch %d" % channel

    def get_pot(self, channel):
        """Parameter read-back, so the header shows the real value."""
        if channel == 0:
            return self._gain / GAIN_MAX
        if channel == 1:
            return self._trim_start / 0.9
        if channel == 2:
            return (self._trim_end - 0.1) / 0.9
        return None

    # --- buttons ---

    def on_button(self, button_id):
        if button_id == RECORD_BUTTON:
            self.record()

    # --- display ---

    def draw_ui(self, disp, y_offset):
        """Status + a bar showing how much sample there is (and the trim)."""
        from ..display import FONT_5X7, FONT_TENFATGUYS, FONT_4X6
        try:
            self._maybe_reload()
        except Exception:
            pass
        title = "SAMPLER"
        disp.draw_str(FONT_TENFATGUYS, 0, y_offset + 12, title, 1)
        line = self._status
        disp.draw_str(FONT_5X7, 0, y_offset + 26, line[:25], 1)
        # trim bar: the kept region, with the discarded head/tail left empty
        y = y_offset + 34
        w = 128
        disp.rect(0, y, w, 7, 1)
        if self._length:
            a = int(w * self._trim_start)
            b = int(w * self._trim_end)
            if b > a:
                disp.fill_rect(a + 1, y + 1, max(1, b - a - 1), 5, 1)
        disp.draw_str(FONT_4X6, 0, y + 12, "gain %.1fx" % self._gain, 1)
        if self._length:
            disp.draw_str(FONT_4X6, 60, y + 12,
                          "%.2fs %dHz" % (self._length / float(self._rate),
                                          self._rate), 1)
