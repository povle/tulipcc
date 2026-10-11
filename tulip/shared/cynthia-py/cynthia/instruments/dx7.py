# cynthia/instruments/dx7.py
# DX7 instrument for CYNTHIA.
#
# Port of ~/projects/synth/src/InstrumentDX7.cpp.
# DX7 patches live at AMY built-in patch numbers 128..255 (index + 128).
# The original firmware locks the pots (DX7 has no front-panel editing);
# the display shows the patch name and the algorithm diagram.

import amy
from ..patchdata import DX7_PATCHES, DX7_ALGOS

NUM_PATCHES = 128
SYNTH = 1
NUM_VOICES = 8
PATCH_OFFSET = 128


class DX7Instrument:
    """DX7 instrument with 128 built-in patches (AMY patches 128-255).

    Implements the PatchProvider protocol (§12.1):
        count() -> int
        name(i) -> str
        select(i) -> None
        current() -> int
    """

    def __init__(self):
        self.patch_index = 0
        self._started = False

    # --- PatchProvider protocol (§12.1) ---

    def count(self):
        return NUM_PATCHES

    def name(self, i):
        if 0 <= i < len(DX7_PATCHES):
            return DX7_PATCHES[i][0]
        return "Patch %d" % i

    def select(self, i):
        self.set_patch(i)

    def current(self):
        return self.patch_index

    def init(self):
        amy.send(synth=SYNTH, num_voices=NUM_VOICES,
                 patch=self.patch_index + PATCH_OFFSET)
        self._started = True

    def start(self):
        if not self._started:
            self.init()

    def stop(self):
        amy.send(synth=SYNTH, reset='')
        self._started = False

    def set_patch(self, idx):
        """Load a new patch by index (0-127)."""
        self.patch_index = idx % NUM_PATCHES
        if self._started:
            amy.send(synth=SYNTH, patch=self.patch_index + PATCH_OFFSET)

    def get_patch_name(self):
        if self.patch_index < len(DX7_PATCHES):
            return DX7_PATCHES[self.patch_index][0]
        return "Patch %d" % self.patch_index

    def get_algo(self):
        """Algorithm number (1-32) for the current patch."""
        if self.patch_index < len(DX7_PATCHES):
            return DX7_PATCHES[self.patch_index][1]
        return 1

    def update_pot(self, channel, value):
        # DX7 pots are locked in the original firmware — no live editing.
        pass

    def on_button(self, button_id):
        # Patch navigation is handled by the runtime (buttons 2/3).
        pass

    # --- Display UI (ported from InstrumentDX7::drawUI + draw_algo) ---

    def draw_ui(self, disp, y_offset):
        """Draw patch name (large) and the algorithm diagram."""
        from ..display import FONT_SPLEEN12X24

        # Patch name in FONT_SPLEEN12X24 matching InstrumentDX7::drawUI
        # setCursor(0, y_offset + 18) in C++
        patch_name = self.get_patch_name()
        disp.draw_str(FONT_SPLEEN12X24, 0, y_offset + 18, patch_name, 1)

        algo_num = self.get_algo()
        if 1 <= algo_num <= 32:
            top, bottom, carriers = DX7_ALGOS[algo_num - 1]
        else:
            top, bottom, carriers = [], [], ()
        self._draw_algo(disp, top, bottom, carriers, 9)

    def _draw_algo(self, disp, top_connections, bottom_connections, carriers, bottom_offset):
        from ..display import FONT_TENFATGUYS

        base_y = 63

        # 1. Draw operators (6 boxes, 16x16, spaced 21px).
        # In C++:
        # textCenteredAtX(u8g2, x_0+8, y_0 + 13, buffer) in u8g2_font_tenfatguys_tf
        # if is_carrier: drawBox with XOR mode (color=2)
        # else: drawFrame
        for op in range(6):
            x_0 = 2 + op * 21
            w = 16
            h = 16
            y_0 = base_y - bottom_offset - h

            num = str(op + 1)
            num_w = disp.str_width(FONT_TENFATGUYS, num)
            text_x = x_0 + 8 - num_w // 2
            text_y = y_0 + 13

            is_carrier = op in carriers
            if is_carrier:
                # Fill box white, draw black number on top
                disp.fill_rect(x_0, y_0, w, h, 1)
                disp.draw_str(FONT_TENFATGUYS, text_x, text_y, num, 0)
            else:
                disp.rect(x_0, y_0, w, h, 1)
                disp.draw_str(FONT_TENFATGUYS, text_x, text_y, num, 1)
        # 2. Top connections (modulator outputs above the operators).
        for a, b in top_connections:
            x_a = 2 + a * 21
            y_hline = base_y - bottom_offset - 16 - 4

            disp.vline(x_a + 8, y_hline, 3, 1)

            if a != b:
                x_b = 2 + b * 21
                disp.vline(x_b + 8, y_hline, 3, 1)
                start_x = min(x_a, x_b)
                width = abs(x_b - x_a) + 1
                disp.hline(start_x + 8, y_hline, width, 1)
            else:
                # Feedback loop
                disp.hline(x_a + 17, base_y - bottom_offset - 8, 2, 1)
                disp.vline(x_a + 18, y_hline, 12, 1)
                disp.hline(x_a + 8, y_hline, 11, 1)

        # 3. Bottom connections (carrier outputs below the operators).
        for a, b in bottom_connections:
            x_a = 2 + a * 21
            y_hline = base_y - bottom_offset + 3

            disp.vline(x_a + 8, y_hline - 2, 3, 1)

            if a != b:
                x_b = 2 + b * 21
                disp.vline(x_b + 8, y_hline - 2, 3, 1)
                start_x = min(x_a, x_b)
                width = abs(x_b - x_a) + 1
                disp.hline(start_x + 8, y_hline, width, 1)
            else:
                # Feedback loop
                disp.hline(x_a + 17, base_y - bottom_offset - 8, 2, 1)
                disp.vline(x_a + 18, y_hline - 12, 12, 1)
                disp.hline(x_a + 8, y_hline, 11, 1)