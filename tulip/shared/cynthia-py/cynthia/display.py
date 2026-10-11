# cynthia/display.py
# SH1106 128x64 OLED driver for the CYNTHIA mainboard.
# The display sits on I2C bus 0 (same bus as ADS1115), address 0x3C.
# Uses MicroPython framebuf in MONO_VLSB format (same as SSD1306/SH1106).
#
# The original Arduino firmware uses U8G2_SH1106_128X64_NONAME_F_HW_I2C
# with rotation U8G2_R2 (180°). We replicate that here.

import framebuf
from machine import I2C, Pin
from . import pins

import tulip

# U8G2 Font Constants
FONT_5X7 = 0            # u8g2_font_5x7_tr (status bar, headers, labels)
FONT_4X6 = 1            # u8g2_font_4x6_tr (micro labels, slider annotations)
FONT_7X13 = 2           # u8g2_font_7x13_tr (sampler / status values)
FONT_TENFATGUYS = 3     # u8g2_font_tenfatguys_tf (patch names, bold titles)
FONT_TENTHINNERGUYS = 4 # u8g2_font_tenthinnerguys_tf (instrument grid menu labels)
FONT_RESOLEDBOLD = 5    # u8g2_font_resoledbold_tr (synth name in status bar header)
FONT_SPLEEN12X24 = 6    # u8g2_font_spleen12x24_mu (large readout numbers)
# SH1106 I2C address (0x3C or 0x3D)
SH1106_ADDR = 0x3C

# Display dimensions
WIDTH = 128
HEIGHT = 64

# SH1106 has a 132-wide internal RAM; the visible 128 columns start at offset 2
_COL_OFFSET = 2

# I2C command/data prefixes
_CMD = 0x00
_DATA = 0x40


class SH1106:
    """SH1106 128x64 OLED display driver with framebuf backing."""

    def __init__(self, i2c=None, addr=SH1106_ADDR, rotate=0):
        """Initialize the SH1106 display.

        Args:
            i2c: machine.I2C instance (created if None)
            addr: I2C address (default 0x3C)
            rotate: rotation (0 or 2 for 180°). Default 0: on the actual
            CYNTHIA panel the rotate=2 command pair renders upside down
            (U8G2's SH1106 driver inverts the remap/COM-scan convention
            relative to the SSD1306, so our pairs land flipped).
        """
        self.addr = addr
        self.rotate = rotate
        self.width = WIDTH
        self.height = HEIGHT

        if i2c is None:
            i2c = I2C(0, scl=Pin(pins.I2C_SCL), sda=Pin(pins.I2C_SDA), freq=400000)
        self.i2c = i2c

        # Framebuffer: MONO_VLSB is the native SH1106 format
        self.buffer = bytearray(WIDTH * HEIGHT // 8)
        self.fb = framebuf.FrameBuffer(self.buffer, WIDTH, HEIGHT, framebuf.MONO_VLSB)

        self._init_display()

    def _write_cmd(self, *cmds):
        """Write one or more command bytes."""
        for cmd in cmds:
            self.i2c.writeto(self.addr, bytes([_CMD, cmd]))

    def _init_display(self):
        """Send the SH1106 initialization sequence."""
        self._write_cmd(0xAE)  # Display off
        self._write_cmd(0xD5, 0xF0)  # Clock divide (matches Arduino: sendF("ca", 0xD5, 0xF0))
        self._write_cmd(0xA8, 0x3F)  # Multiplex ratio (64-1)
        self._write_cmd(0xD3, 0x00)  # Display offset
        self._write_cmd(0x40)  # Start line 0
        self._write_cmd(0xAD, 0x8B)  # SH1106 DC-DC pump control (on)
        self._write_cmd(0x20, 0x00)  # Memory mode: horizontal
        if self.rotate == 2:
            self._write_cmd(0xA1)  # Segment remap (for 180° rotation)
            self._write_cmd(0xC8)  # COM scan direction (for 180° rotation)
        else:
            self._write_cmd(0xA0)  # Segment normal
            self._write_cmd(0xC0)  # COM scan normal
        self._write_cmd(0xDA, 0x12)  # COM pins
        self._write_cmd(0x81, 0x80)  # Contrast
        self._write_cmd(0xD9, 0xF1)  # Pre-charge
        self._write_cmd(0xDB, 0x40)  # VCOMH
        self._write_cmd(0xA4)  # Entire display ON (resume from RAM)
        self._write_cmd(0xA6)  # Normal display (not inverted)
        self._write_cmd(0xAF)  # Display on

    def show(self):
        """Push the framebuffer to the display."""
        # SH1106 requires page-by-page writes with column offset
        for page in range(8):  # 64 rows / 8 = 8 pages
            self._write_cmd(
                0xB0 | page,  # Set page address
                0x00 | (_COL_OFFSET & 0x0F),  # Lower column address
                0x10 | ((_COL_OFFSET >> 4) & 0x0F),  # Upper column address
            )
            # Send 128 bytes of data for this page
            offset = page * WIDTH
            self.i2c.writeto(self.addr, bytes([_DATA]) + self.buffer[offset:offset + WIDTH])

    # --- framebuf passthrough methods ---

    def fill(self, col):
        self.fb.fill(col)

    def pixel(self, x, y, col=None):
        if col is None:
            return self.fb.pixel(x, y)
        self.fb.pixel(x, y, col)

    def hline(self, x, y, w, col):
        self.fb.hline(x, y, w, col)

    def vline(self, x, y, h, col):
        self.fb.vline(x, y, h, col)

    def line(self, x1, y1, x2, y2, col):
        self.fb.line(x1, y1, x2, y2, col)

    def rect(self, x, y, w, h, col):
        self.fb.rect(x, y, w, h, col)

    def fill_rect(self, x, y, w, h, col):
        self.fb.fill_rect(x, y, w, h, col)

    def text(self, string, x, y, col=1):
        self.fb.text(string, x, y, col)

    def scroll(self, dx, dy):
        self.fb.scroll(dx, dy)

    def clear(self):
        """Clear the display and push to hardware."""
        self.fill(0)
        self.show()

    # --- U8G2 Font Rendering ---

    def draw_str(self, font_idx, x, y, string, col=1):
        """Draw string with U8G2 font at baseline (x, y)."""
        if hasattr(tulip, "cynthia_draw_str"):
            return tulip.cynthia_draw_str(self.buffer, font_idx, x, y, str(string), col)
        self.text(str(string), x, max(0, y - 7), col)
        return len(str(string)) * 8

    def str_width(self, font_idx, string):
        """Return advance width in pixels for string using U8G2 font."""
        if hasattr(tulip, "cynthia_str_width"):
            return tulip.cynthia_str_width(font_idx, str(string))
        return len(str(string)) * 8

    def draw_header(self, octave=0, battery_v=None, inst_name="CYNTHIA",
                     joystick_xy=None):
        """Draw standard Cynthia status bar header:
        OCT:%+d on left, inst_name centered in resoledbold_tr, joystick pos or battery on right,
        divider line at y=10.
        If joystick_xy is given, display "J: X.XX Y.XX" instead of battery.
        """
        # Left: Octave
        oct_str = "OCT:%+d" % octave
        self.draw_str(FONT_5X7, 2, 7, oct_str, 1)

        # Center: Instrument Name in resoledbold_tr
        name_str = str(inst_name)
        text_w = self.str_width(FONT_RESOLEDBOLD, name_str)
        start_x = max(0, (WIDTH - text_w) // 2)
        self.draw_str(FONT_RESOLEDBOLD, start_x, 7, name_str, 1)

        # Right: joystick position (temp) or battery
        if joystick_xy is not None:
            jx, jy = joystick_xy
            joy_str = "J:%.2f" % (jy)
            # Show Y prominently, X as smaller
            self.draw_str(FONT_5X7, 100, 7, joy_str, 1)
        else:
            if battery_v is None:
                if hasattr(tulip, "cynthia_battery_voltage"):
                    battery_v = tulip.cynthia_battery_voltage()
                else:
                    battery_v = 4.12
            batt_str = "%.2fV" % battery_v
            self.draw_str(FONT_5X7, 100, 7, batt_str, 1)

        # Horizontal divider line at y=10
        self.hline(0, 10, WIDTH, 1)

    def draw_instrument_grid(self, instruments, cursor_row=0, cursor_col=0):
        """Draw 2x3 grid instrument browser menu matching original Cynthia C++ firmware.
        Cells: 42x23, starting at MENU_Y=12. Active cell inverted box.
        """
        cell_w = 42
        cell_h = 23
        menu_y = 12

        for row in range(2):
            for col in range(3):
                idx = row * 3 + col
                x = col * cell_w
                y = menu_y + row * cell_h
                is_cursor = (row == cursor_row and col == cursor_col)

                name = instruments[idx] if idx < len(instruments) else "---"
                text_w = self.str_width(FONT_TENTHINNERGUYS, name)
                text_x = x + (cell_w - text_w) // 2
                text_y = y + (cell_h + 9) // 2

                if is_cursor:
                    self.fill_rect(x + 1, y + 1, cell_w - 2, cell_h - 2, 1)
                    self.draw_str(FONT_TENTHINNERGUYS, text_x, text_y, name, 0)
                else:
                    self.rect(x, y, cell_w, cell_h, 1)
                    self.draw_str(FONT_TENTHINNERGUYS, text_x, text_y, name, 1)