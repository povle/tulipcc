# cynthia/instruments/juno.py
# Juno-106 instrument for CYNTHIA.
#
# Port of ~/projects/synth/src/InstrumentJuno.cpp + JunoUI.cpp.
# Uses amy.juno.JunoPatch for synth setup and parameter updates.
#
# Pot mapping (from InstrumentJuno::updatePot):
#   0: lfo_rate       1: dco_lfo (PWM depth)  2: dco_noise
#   3: vcf_freq       4: vcf_res              5: vcf_env
#   6: env_a          7: env_d                8: env_s
#   9: env_r         10: vca_level           11: dco_sub

import amy
from amy.juno import JunoPatch
from ..patchdata import JUNO_NAMES

NUM_PATCHES = 128
SYNTH = 1
NUM_VOICES = 6

# Pot -> JunoPatch parameter name
# Matching InstrumentJuno.cpp updatePot() 16-channel layout:
#   0: dco_pwm (duty cycle)
#   1: saw (saw_level in C++) — but JunoPatch uses bool saw
#   2: dco_sub (sub level)
#   3: hpf (HPF cutoff, 0-3)
#   4: lfo_rate (LFO rate)
#   5: vcf_lfo (LFO depth -> VCF mod)
#   6: Not mapped (LFO target in C++)
#   7: vcf_freq (filter cutoff)
#   8: vcf_res (resonance)
#   9: env_a
#  10: env_d
#  11: env_s
#  12: env_r
#  13: Not mapped directly (delay level, AMY echo)
#  14: Not mapped directly (delay time)
#  15: dco_noise (noise level)
# Channels 13/14 require amy.echo() calls; 6 is target-only.
POT_MAP = [
    "dco_pwm", "dco_sub", "dco_noise",
    "vcf_freq", "vcf_res", "vcf_env",
    "env_a", "env_d", "env_s",
    "env_r", "vca_level", "dco_sub",
]
# Full 16-channel mapping (channels 0-15 reachable from hardware)
POT_MAP_16 = [
    "dco_pwm",      # 0: Duty Cycle
    None,           # 1: Saw level (binary, set via patch)
    "dco_sub",      # 2: Sub Level
    None,           # 3: HPF (stored as self.hpf, update_cho handles)
    "lfo_rate",     # 4: LFO Rate
    "vcf_lfo",      # 5: LFO Depth (maps to VCF mod depth)
    None,           # 6: LFO Target (read-only, passed to lfo)
    "vcf_freq",     # 7: Filter Cutoff
    "vcf_res",      # 8: Resonance
    "env_a",        # 9: Attack
    "env_d",        # 10: Decay
    "env_s",        # 11: Sustain
    "env_r",        # 12: Release
    None,           # 13: Delay Level → amy.echo() via config_echo
    None,           # 14: Delay Time → amy.echo()
    None,           # 15: Glide → portamento, noise for Juno
]
# Friendly knob names for the overlay
POT_NAMES = [
    "Duty PWM",     # 0
    "Saw",          # 1
    "Sub",          # 2
    "HPF",          # 3
    "LFO Rate",     # 4
    "LFO Depth",    # 5
    "LFO Target",   # 6
    "Cutoff",       # 7
    "Resonance",    # 8
    "Attack",       # 9
    "Decay",        # 10
    "Sustain",      # 11
    "Release",      # 12
    "Delay Lvl",    # 13
    "Delay Time",   # 14
    "Glide",        # 15
]


class JunoInstrument:
    """Juno-106 instrument with 128 built-in patches.

    Implements the PatchProvider protocol (§12.1):
        count() -> int
        name(i) -> str
        select(i) -> None
        current() -> int
    """

    def __init__(self):
        self.patch_index = 0
        self.patch = None
        self._started = False

    # --- PatchProvider protocol (§12.1) ---

    def count(self):
        return NUM_PATCHES

    def name(self, i):
        if 0 <= i < len(JUNO_NAMES):
            return JUNO_NAMES[i]
        return "Patch %d" % i

    def select(self, i):
        self.set_patch(i)

    def current(self):
        return self.patch_index

    def init(self):
        """Set up the synth with the current patch. Can be called
        at any time — initializes or re-initializes cleanly."""
        self.patch = JunoPatch.from_patch_number(self.patch_index)
        self.patch.set_synth(SYNTH)
        # Set up the synth voice structure
        amy.send(synth=SYNTH, num_voices=NUM_VOICES, patch=self.patch_index)
        # init_AMY() assigns the osc indices and sends per-osc setup commands.
        self.patch.init_AMY()
        self._started = True

    def start(self):
        if not self._started:
            self.init()

    def stop(self):
        if self._started:
            amy.send(synth=SYNTH, reset='')
            self._started = False

    def set_patch(self, idx):
        """Load a new patch by index (0-127). Works even before init()."""
        self.patch_index = idx % NUM_PATCHES
        if not self._started:
            try:
                self.patch = JunoPatch.from_patch_number(self.patch_index)
                self.patch.set_synth(SYNTH)
            except Exception:
                pass
            return
        amy.send(synth=SYNTH, patch=self.patch_index)
        self.patch = JunoPatch.from_patch_number(self.patch_index)
        self.patch.set_synth(SYNTH)
        self.patch.init_AMY()

    def get_patch_name(self):
        if self.patch_index < len(JUNO_NAMES):
            return JUNO_NAMES[self.patch_index]
        return "Patch %d" % self.patch_index

    def save_patch_state(self):
        """Return dict of current knob/state to persist as a user patch."""
        if self.patch is None:
            return {}
        p = self.patch
        return {
            "patch_index": self.patch_index,
            "lfo_rate": p.lfo_rate,
            "lfo_delay_time": p.lfo_delay_time,
            "dco_lfo": p.dco_lfo,
            "dco_pwm": p.dco_pwm,
            "dco_noise": p.dco_noise,
            "vcf_freq": p.vcf_freq,
            "vcf_res": p.vcf_res,
            "vcf_env": p.vcf_env,
            "vcf_lfo": p.vcf_lfo,
            "vcf_kbd": p.vcf_kbd,
            "vca_level": p.vca_level,
            "env_a": p.env_a,
            "env_d": p.env_d,
            "env_s": p.env_s,
            "env_r": p.env_r,
            "dco_sub": p.dco_sub,
            "stop_16": p.stop_16,
            "stop_8": p.stop_8,
            "stop_4": p.stop_4,
            "pulse": p.pulse,
            "saw": p.saw,
            "pwm_manual": p.pwm_manual,
            "vcf_neg": p.vcf_neg,
            "vca_gate": p.vca_gate,
            "chorus": p.chorus,
            "hpf": p.hpf,
            "portamento": p.portamento,
        }

    def load_patch_state(self, state):
        """Restore instrument state from a user patch dict."""
        if not state:
            return
        idx = state.get("patch_index", 0)
        self.patch_index = idx % NUM_PATCHES
        if not self._started:
            self.init()
            return
        # Reload the base patch.
        amy.send(synth=SYNTH, patch=self.patch_index)
        self.patch = JunoPatch.from_patch_number(self.patch_index)
        self.patch.set_synth(SYNTH)
        # Apply all the knobs from the saved state.
        for key, val in state.items():
            if key == "patch_index":
                continue
            if hasattr(self.patch, key):
                setattr(self.patch, key, val)
        self.patch.init_AMY()

    def pot_name(self, channel):
        """Human-readable name for pot channel."""
        if 0 <= channel < len(POT_NAMES):
            return POT_NAMES[channel]
        return "Pot %d" % channel

    def update_pot(self, channel, value):
        """Update a pot by channel (0-15). Handles Juno-specific mapping
        matching InstrumentJuno.cpp updatePot()."""
        if self.patch is None:
            return
        if channel >= len(POT_MAP_16):
            return
        param = POT_MAP_16[channel]
        if param is None:
            # Channels without a direct JunoPatch parameter.
            if channel == 1:
                # Saw level: JunoPatch uses a bool 'saw' — binary.
                # Map the continuous value (>0.5 = on).
                new_saw = value > 0.5
                if new_saw != self.patch.saw:
                    self.patch.saw = new_saw
                    self.patch.update_dco()
            elif channel == 3:
                # HPF (0-3 quantized)
                new_hpf = int(round(value * 3.0))
                if new_hpf != self.patch.hpf:
                    self.patch.hpf = new_hpf
                    self.patch.update_cho()
            elif channel == 6:
                # LFO Target: no direct mapping in JunoPatch.
                pass
            elif channel == 13:
                # Delay level -> amy.echo()
                import amy
                if value < 0.05:
                    amy.echo(level=0)
                else:
                    amy.echo(level=value, max_delay_ms=3000,
                              feedback=value * 0.8)
            elif channel == 14:
                # Delay time
                import amy
                if value < 0.05:
                    amy.echo(level=0)
                else:
                    amy.echo(delay_ms=int(value * 2000), max_delay_ms=3000)
            elif channel == 15:
                # Glide/portamento -> dco_noise for Juno original.
                # Just set dco_noise and update.
                self.patch.dco_noise = value
                self.patch.update_dco()
            return
        self.patch.set_param(param, value)

    def on_button(self, button_id):
        """Handle instrument-specific button presses."""
        # Juno has no instrument-specific buttons in the original firmware
        pass

    # --- Display UI (ported from JunoUI.cpp) ---

    def draw_ui(self, disp, y_offset):
        """Draw the Juno UI on the display.

        Layout matching JunoUI.cpp:
        - Patch name wrapped with FONT_TENFATGUYS at (0, y_offset + 11)
        - 5 column header boxes (DCO, HPF, VCF, LFO, ENV) with FONT_5X7 inverted text
        - Column widths: [33, 17, 22, 22, 33]
        - Micro labels in FONT_4X6
        """
        if not self.patch:
            return

        from ..display import FONT_5X7, FONT_4X6, FONT_TENFATGUYS

        # --- PATCH NAME ---
        patch_name = self.name(self.patch_index)
        if "\n" in patch_name:
            line1, line2 = patch_name.split("\n", 1)
            disp.draw_str(FONT_TENFATGUYS, 0, y_offset + 11, line1, 1)
            disp.draw_str(FONT_TENFATGUYS, 0, y_offset + 29, line2, 1)
        else:
            disp.draw_str(FONT_TENFATGUYS, 0, y_offset + 11, patch_name, 1)

        header_box_h = 8
        graph_height = 25
        header_y = y_offset + 14
        graph_top = header_y + header_box_h
        graph_bottom = graph_top + graph_height

        # Column widths: DCO=33, HPF=17, VCF=22, LFO=22, ENV=33
        col_widths = [33, 17, 22, 22, 33]
        labels = ["DCO", "HPF", "VCF", "LFO", "ENV"]

        # Draw column headers (inverted boxes)
        x = 0
        for i in range(5):
            disp.fill_rect(x + 1, header_y, col_widths[i] - 1, header_box_h, 1)
            disp.vline(x, header_y + header_box_h, graph_height, 1)
            # Label text (inverted color 0) in FONT_5X7
            # Match C++: setCursor(x + colWidths[i]/2 - 6, headerY + 7)
            label_x = x + col_widths[i] // 2 - 6
            disp.draw_str(FONT_5X7, label_x, header_y + 7, labels[i], 0)
            x += col_widths[i]
        disp.vline(x, header_y + header_box_h, graph_height, 1)

        # Horizontal bottom line at y=58 (screenHeight - 6 = 58)
        disp.hline(0, 58, 128, 1)
        # --- Column 1: DCO (31px) ---
        self._draw_dco(disp, 0, col_widths[0], graph_top, graph_bottom)

        # --- Column 2: HPF (17px) ---
        self._draw_hpf(disp, col_widths[0], col_widths[1], graph_top, graph_bottom)

        # --- Column 3: VCF (22px) ---
        self._draw_vcf(disp, col_widths[0] + col_widths[1], col_widths[2], graph_top, graph_bottom)

        # --- Column 4: LFO (22px) ---
        self._draw_lfo(disp, col_widths[0] + col_widths[1] + col_widths[2], col_widths[3], graph_top, graph_bottom)

        # --- Column 5: ENV (33px) ---
        self._draw_env(disp, col_widths[0] + col_widths[1] + col_widths[2] + col_widths[3], col_widths[4], graph_top, graph_bottom)

    def _draw_dco(self, disp, x, width, top, bottom):
        """Draw DCO column: PWM waveform on top, saw/sub/noise bars below."""
        split_y = top + 8
        disp.hline(x + 1, split_y + 1, width - 1, 1)

        # PWM waveform (top section)
        duty_left = x + 2
        duty_top = top + 1
        duty_right = x + width - 2
        duty_bottom = split_y - 1
        duty_width = duty_right - duty_left
        duty_height = duty_bottom - duty_top

        # Get PWM amount from patch
        pwm = getattr(self.patch, 'dco_pwm', 0)
        pulse_en = getattr(self.patch, 'pulse', False)

        if not pulse_en or pwm < 0.02:
            disp.hline(duty_left, duty_bottom, duty_width, 1)
        else:
            active_pwm = (pwm - 0.02) / 0.98
            pulse_width = max(1, int(duty_width * active_pwm))
            pulse_start = duty_left + (duty_width - pulse_width) // 2
            pulse_end = pulse_start + pulse_width

            disp.hline(duty_left, duty_bottom, pulse_start - duty_left, 1)
            disp.vline(pulse_start, duty_top, duty_height + 1, 1)
            disp.hline(pulse_start, duty_top, pulse_width, 1)
            disp.vline(pulse_end, duty_top, duty_height, 1)
            disp.hline(pulse_end, duty_bottom, duty_right - pulse_end, 1)

        # Bars: saw, sub, noise (bottom section)
        bar_width = 5
        bar_spacing = (width - 2 - bar_width * 3) // 4
        bar_max_h = bottom - split_y - 5

        saw_level = 1.0 if getattr(self.patch, 'saw', False) else 0.0
        sub_level = getattr(self.patch, 'dco_sub', 0)
        noise_level = getattr(self.patch, 'dco_noise', 0)

        # Saw bar (diagonal)
        saw_x = x + 1 + bar_spacing
        saw_h = int(saw_level * bar_max_h)
        disp.line(saw_x, bottom - 3, saw_x + bar_width, bottom - 3 - saw_h, 1)
        disp.vline(saw_x + bar_width, bottom - 2 - saw_h, saw_h, 1)

        # Sub bar (U-shape)
        sub_x = saw_x + bar_width + bar_spacing
        sub_h = int(sub_level * bar_max_h)
        disp.vline(sub_x, bottom - 2 - sub_h, sub_h, 1)
        disp.hline(sub_x, bottom - 3 - sub_h, bar_width + 1, 1)
        disp.vline(sub_x + bar_width, bottom - 2 - sub_h, sub_h, 1)

        # Noise bar (filled)
        noise_x = sub_x + bar_width + bar_spacing
        noise_h = max(1, int(noise_level * (bar_max_h + 1)))
        disp.fill_rect(noise_x, bottom - 2 - noise_h, bar_width, noise_h, 1)

    def _draw_hpf(self, disp, x, width, top, bottom):
        """Draw HPF column: 4-position slider with triangles."""
        center_x = x + width // 2
        slider_top = top + 3
        slider_bottom = bottom - 3

        # Draw slider track
        self._draw_slider(disp, center_x, slider_top, slider_bottom)

        # HPF position (0-3, inverted for display)
        hpf = getattr(self.patch, 'hpf', 0)
        line_idx = 3 - hpf
        step = (slider_bottom - slider_top) // 3
        tip_y = slider_top + line_idx * step - 1

        # Draw triangles pointing at position
        self._draw_hpf_triangles(disp, center_x, tip_y)

    def _draw_vcf(self, disp, x, width, top, bottom):
        """Draw VCF column: dual slider for freq (F) and resonance (R)."""
        center_x = x + width // 2
        slider_top = top + 3
        slider_bottom = bottom - 4

        self._draw_slider(disp, center_x, slider_top, slider_bottom)

        vcf_freq = getattr(self.patch, 'vcf_freq', 0)
        vcf_res = getattr(self.patch, 'vcf_res', 0)

        freq_pos = 1.0 - vcf_freq
        freq_y = int(slider_top + freq_pos * (slider_bottom - slider_top) + 0.5)

        res_pos = 1.0 - vcf_res
        res_y = int(slider_top + res_pos * (slider_bottom - slider_top) + 0.5)

        self._draw_dual_slider(disp, center_x, freq_y, res_y, 'F', 'R', bottom, top)

    def _draw_lfo(self, disp, x, width, top, bottom):
        """Draw LFO column: dual slider for rate (F) and VCF depth (A)."""
        center_x = x + width // 2
        slider_top = top + 3
        slider_bottom = bottom - 4

        self._draw_slider(disp, center_x, slider_top, slider_bottom)

        lfo_rate = getattr(self.patch, 'lfo_rate', 0)
        vcf_lfo = getattr(self.patch, 'vcf_lfo', 0)

        rate_pos = 1.0 - lfo_rate
        rate_y = int(slider_top + rate_pos * (slider_bottom - slider_top) + 0.5)

        amp_pos = 1.0 - vcf_lfo
        amp_y = int(slider_top + amp_pos * (slider_bottom - slider_top) + 0.5)

        self._draw_dual_slider(disp, center_x, rate_y, amp_y, 'F', 'A', bottom, top)

    def _draw_env(self, disp, x, width, top, bottom):
        """Draw ENV column: ADSR envelope curve."""
        graph_left = x + 2
        graph_right = x + width - 2
        graph_width = graph_right - graph_left
        graph_top = top + 2
        graph_bottom = bottom - 4
        graph_height = graph_bottom - graph_top

        env_a = getattr(self.patch, 'env_a', 0)
        env_d = getattr(self.patch, 'env_d', 0)
        env_s = getattr(self.patch, 'env_s', 0)
        env_r = getattr(self.patch, 'env_r', 0)

        max_seg_w = graph_width / 3.1

        attack_w = max(1, int(env_a * max_seg_w))
        decay_w = max(1, int(env_d * max_seg_w))
        release_w = max(1, int(env_r * max_seg_w))
        sustain_w = max(1, int(graph_width - (attack_w + decay_w + release_w)))

        attack_x = graph_left
        attack_y = graph_bottom
        decay_x = attack_x + attack_w
        decay_y = graph_top
        sustain_x = decay_x + decay_w
        sustain_y = graph_top + int((1.0 - env_s) * graph_height)
        release_start_x = sustain_x + sustain_w
        release_start_y = sustain_y
        end_x = graph_right
        end_y = graph_bottom

        disp.line(attack_x, attack_y, decay_x, decay_y, 1)
        disp.line(decay_x, decay_y, sustain_x, sustain_y, 1)
        disp.line(sustain_x, sustain_y, release_start_x, release_start_y, 1)
        disp.line(release_start_x, release_start_y, end_x, end_y, 1)

    # --- Drawing helpers ---

    def _draw_slider(self, disp, center_x, top, bottom):
        """Draw vertical slider track with 4 horizontal lines."""
        height = bottom - top
        disp.vline(center_x, top, height + 1, 1)
        step = height // 3
        for i in range(3):
            y = top + i * step
            w = 7 if i == 0 else 5
            disp.hline(center_x - w // 2, y, w, 1)
        disp.hline(center_x - 3, bottom, 7, 1)

    def _draw_hpf_triangles(self, disp, center_x, tip_y):
        """Draw HPF indicator triangles (pointing inward)."""
        # Left triangle (pointing right)
        disp.pixel(center_x - 4, tip_y + 1, 1)
        disp.pixel(center_x - 5, tip_y, 1)
        disp.pixel(center_x - 5, tip_y + 2, 1)
        disp.pixel(center_x - 6, tip_y - 1, 1)
        disp.pixel(center_x - 6, tip_y + 3, 1)

        # Right triangle (pointing left)
        disp.pixel(center_x + 4, tip_y + 1, 1)
        disp.pixel(center_x + 5, tip_y, 1)
        disp.pixel(center_x + 5, tip_y + 2, 1)
        disp.pixel(center_x + 6, tip_y - 1, 1)
        disp.pixel(center_x + 6, tip_y + 3, 1)

    def _draw_dual_slider(self, disp, center_x, left_y, right_y, left_label, right_label, bottom, top):
        """Draw dual slider with two triangles and labels using FONT_4X6."""
        from ..display import FONT_4X6

        # Left triangle (pointing right)
        disp.pixel(center_x - 4, left_y, 1)
        disp.pixel(center_x - 5, left_y - 1, 1)
        disp.pixel(center_x - 5, left_y, 1)
        disp.pixel(center_x - 5, left_y + 1, 1)

        # Left label with clamp
        label_y = max(min(bottom - 2, left_y + 3), top + 6)
        disp.draw_str(FONT_4X6, center_x - 9, label_y, left_label, 1)

        # Right triangle (pointing left)
        disp.pixel(center_x + 4, right_y, 1)
        disp.pixel(center_x + 5, right_y - 1, 1)
        disp.pixel(center_x + 5, right_y, 1)
        disp.pixel(center_x + 5, right_y + 1, 1)

        # Right label with clamp
        label_y = max(min(bottom - 2, right_y + 3), top + 6)
        disp.draw_str(FONT_4X6, center_x + 7, label_y, right_label, 1)