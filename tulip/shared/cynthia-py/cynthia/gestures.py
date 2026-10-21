# cynthia/gestures.py
# One place for every "hold this to do that" gesture.
#
# Before this, holds were scattered: the calibration chord kept its own timer in
# the poll, the local-settings shortcut inspected the joystick inside the
# long-press button handler, and each new idea added another ad-hoc branch with
# its own progress drawing. Every hold is the same shape — "while this condition
# is true, wait N ms, then fire exactly once" — so it lives here.
#
# Single-button presses are NOT gestures: the C input task already turns a
# 800 ms hold into a `long_press` event, and `runtime._handle_event` maps those.
# Gestures cover the things an event cannot express: simultaneous buttons
# (chords) and conditions on an analog axis.

import time


class HoldGesture:
    """Fire `action` once after `detect()` has stayed true for `hold_ms`.

    `progress()` is 0..1 while engaged, so the UI can draw a bar; it returns 0
    when idle and 1 when the action has fired (until the condition is released).
    """

    def __init__(self, name, detect, hold_ms, action, label=None,
                 enabled=None):
        self.name = name
        self._detect = detect
        self.hold_ms = hold_ms
        self._action = action
        # label is a callable so the text follows the active locale.
        self.label = label
        self._enabled = enabled
        self._start = None
        self._fired = False

    # -- state ------------------------------------------------------------
    def reset(self):
        self._start = None
        self._fired = False

    def active(self):
        return self._start is not None and not self._fired

    def progress(self, now=None):
        if self._start is None:
            return 0.0
        if self._fired:
            return 1.0
        if now is None:
            now = time.ticks_ms()
        held = time.ticks_diff(now, self._start)
        if held < 0:
            held = 0
        return min(1.0, held / float(self.hold_ms))

    def poll(self, now=None):
        """Advance the gesture. Returns its progress (0..1)."""
        if self._enabled is not None and not self._enabled():
            self.reset()
            return 0.0
        try:
            on = self._detect()
        except Exception:
            on = False
        if not on:
            self.reset()
            return 0.0
        if now is None:
            now = time.ticks_ms()
        if self._start is None:
            self._start = now
            return 0.0
        if self._fired:
            return 1.0
        if time.ticks_diff(now, self._start) >= self.hold_ms:
            # Latch BEFORE calling out: an action that opens a screen or blocks
            # must not be able to re-enter this gesture on the next poll.
            self._fired = True
            try:
                self._action()
            except Exception as e:
                print("cynthia: gesture %s failed:" % self.name, e)
            return 1.0
        return self.progress(now)


class GestureSet:
    """Poll a list of gestures and expose the one currently being held."""

    def __init__(self, gestures):
        self.gestures = list(gestures)

    def add(self, gesture):
        self.gestures.append(gesture)
        return gesture

    def poll(self, now=None):
        for g in self.gestures:
            g.poll(now)

    def active(self):
        """The gesture being held, or None (for the progress overlay)."""
        for g in self.gestures:
            if g.active():
                return g
        return None

    def reset(self):
        for g in self.gestures:
            g.reset()
