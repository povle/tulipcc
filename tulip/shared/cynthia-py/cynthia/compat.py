# cynthia/compat.py
# amyboardworld sketch compatibility for CYNTHIA.
#
# Goal: any sketch from amyboard.com (the amyboardworld library) should run on
# CYNTHIA with no edits. Those sketches are written against AMYboard's Python
# surface, which differs from CYNTHIA's hardware in four ways:
#
#   1. Display canvas. amyboard.Display is a virtual 128x128 GS4 (4-bit
#      grayscale) framebuffer. CYNTHIA's panel is 128x64 1-bit. We keep the
#      128x128 GS4 canvas so sketch coordinates stay valid, then crop to the
#      top 64 rows and threshold to 1-bit on show() — the C blit in
#      cynthia_ui.c does this fast enough for a 33fps refresh.
#   2. No SD card, no PCM9211, no GP8413. Helpers that probe those must no-op
#      instead of raising.
#   3. No CV hardware. amyboard.cv_out()/cv_in() must be harmless.
#   4. Encoder/buttons are the on-board ones, not I2C seesaw peripherals.
#
# Everything here is additive: no amyboard.py behavior is changed.

import tulip

# --- Display -----------------------------------------------------------------

class CynthiaDisplay:
    """amyboard.Display-compatible view of CYNTHIA's 128x64 SH1106 panel.

    Presents the 128x128 GS4 canvas that amyboardworld sketches draw into
    (same WIDTH/HEIGHT/LINEHEIGHT and the same drawing methods), then crops
    and thresholds on show(). A sketch that draws at y=100 simply has that
    content clipped — identical to what an out-of-range draw does on AMYboard.
    """

    WIDTH = 128
    HEIGHT = 128          # the *virtual* canvas height sketches assume
    LINEHEIGHT = 12       # amyboard.Display's text row height

    # Crop/threshold defaults: show the top 64 rows, mid-gray as the cut.
    PANEL_ROWS = 64
    THRESHOLD = 8

    def __init__(self, sh1106):
        import framebuf
        self._panel = sh1106                       # cynthia.display.SH1106
        self._buf = bytearray(self.WIDTH * self.HEIGHT // 2)   # 8KB GS4
        self._fb = framebuf.FrameBuffer(self._buf, self.WIDTH, self.HEIGHT,
                                        framebuf.GS4_HMSB)
        self._threshold = self.THRESHOLD

    # -- amyboard.Display surface --

    @property
    def buffer(self):
        """The GS4 canvas bytes (sketches read this to make sprites)."""
        return self._buf

    @property
    def available(self):
        return self._panel is not None

    @property
    def width(self):
        return self.WIDTH

    @property
    def height(self):
        return self.HEIGHT

    def text(self, string, x, y, col=255):
        self._fb.text(string, x, y, col)

    def fill(self, col):
        self._fb.fill(col)

    def fill_rect(self, x, y, w, h, col):
        self._fb.fill_rect(x, y, w, h, col)

    def line(self, x1, y1, x2, y2, col):
        self._fb.line(x1, y1, x2, y2, col)

    def hline(self, x, y, w, col):
        self._fb.hline(x, y, w, col)

    def vline(self, x, y, h, col):
        self._fb.vline(x, y, h, col)

    def pixel(self, x, y, col=None):
        if col is None:
            return self._fb.pixel(x, y)
        self._fb.pixel(x, y, col)

    def rect(self, x, y, w, h, col):
        self._fb.rect(x, y, w, h, col)

    def scroll(self, dx, dy):
        self._fb.scroll(dx, dy)

    def clear(self):
        self.fill(0)
        self.show()

    def message(self, message, row=0, inverse=False):
        """One text row of the virtual canvas (amyboard.Display API)."""
        top_y = self.LINEHEIGHT * row
        bg, fg = (255, 0) if inverse else (0, 255)
        self.fill_rect(0, top_y, self.WIDTH, self.LINEHEIGHT, bg)
        self.text(message, 0, top_y, fg)
        self.show()

    def show(self, full=False):
        """Crop to the panel, threshold GS4 -> 1-bit, push over I2C."""
        if self._panel is None:
            return
        if hasattr(tulip, "cynthia_blit_gs4"):
            tulip.cynthia_blit_gs4(self._buf, self._panel.buffer,
                                   self.PANEL_ROWS, self._threshold)
        else:
            self._blit_python()
        self._panel.show()

    refresh = show    # amyboard.Display alias

    def _blit_python(self):
        """Fallback blit (only used off-device / if the C binding is absent)."""
        buf = self._buf
        dst = self._panel.buffer
        rows = self.PANEL_ROWS
        th = self._threshold
        for page in range((rows + 7) // 8):
            base = page * 128
            y0 = page * 8
            for x in range(128):
                acc = 0
                for bit in range(8):
                    y = y0 + bit
                    if y >= rows:
                        break
                    idx = (x + y * 128) >> 1
                    v = (buf[idx] & 0x0F) if (x & 1) else (buf[idx] >> 4)
                    if v >= th:
                        acc |= (1 << bit)
                dst[base + x] = acc


# --- hardware-facing helpers -------------------------------------------------

def install(amyboard_module, sh1106=None):
    """Patch an imported `amyboard` module in place with CYNTHIA-safe shims.

    Called once at boot (see _boot.py). Every patch is additive: names that
    already work are left alone, and the originals are kept for callers that
    want a real device.
    """
    ab = amyboard_module

    # 1. Display: bind the compat view if a panel is present.
    if sh1106 is not None:
        try:
            ab.display = CynthiaDisplay(sh1106)
        except Exception as e:
            print("cynthia.compat: display shim failed:", e)

    # 2. No SD card on CYNTHIA -> mount_sd() is already a no-op (amyboard.py).
    #    Sketches that catch OSError still work; nothing to patch.

    # 3. CV helpers: AMYboard-only hardware. Force the no-op path so sketches
    #    calling cv_out()/cv_in() on CYNTHIA don't raise on a missing GP8413.
    def _cv_out_nop(volts, channel=0):
        return None
    def _cv_in_nop(channel=0):
        return 0.0
    def _set_cv_out_nop(channel=0, synth=1):
        return None
    ab.cv_out = _cv_out_nop
    ab.cv_in = _cv_in_nop
    ab.set_cv_out = _set_cv_out_nop

    # 4. init_display(): sketches call this to (re)create the display. Route it
    #    to the compat view so a re-init doesn't try SH1107/SSD1327 probing
    #    (CYNTHIA's SH1106 is 128x64 and answers at the same 0x3c address, so
    #    the AMYboard probe would bind the wrong driver to the right panel).
    def _init_display(rotate=None):
        if sh1106 is not None:
            ab.display = CynthiaDisplay(sh1106)
        return ab.display
    ab.init_display = _init_display

    # 5. environment_transfer_done(): the web editor's "Write to AMYboard" sends
    #    the sketch with zT, then calls this to restart the running sketch. We
    #    keep that behaviour untouched and additionally file the same bytes into
    #    the instrument library, so a sketch written in the browser shows up in
    #    CYNTHIA's instrument menu. This is the "no website changes" path: the
    #    editor still writes only /user/current/sketch.py, and the device does
    #    the filing. See cynthia/library.py. Wrapped, not replaced, so a future
    #    amyboard.py change to the original is preserved.
    _original_transfer_done = getattr(ab, "environment_transfer_done", None)

    def _environment_transfer_done(*args):
        try:
            _archive_current()
        except Exception as e:
            print("cynthia.compat: library archive failed:", e)
        if _original_transfer_done is not None:
            return _original_transfer_done(*args)
        return None

    ab.environment_transfer_done = _environment_transfer_done

    return ab


# --- instrument library ------------------------------------------------------

# The one path the web editor writes; also the sketch that is currently running.
CURRENT_SKETCH = "/user/current/sketch.py"


def _archive_current():
    """Copy the just-received /user/current/sketch.py into the instrument
    library (idempotent — see cynthia.library.archive)."""
    from . import library
    try:
        with open(CURRENT_SKETCH, "rb") as f:
            text = f.read()
    except OSError:
        return None
    return library.archive(text, filename="sketch.py")
