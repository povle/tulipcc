# cynthia/connector.py
# Slot abstraction for the CYNTHIA daughterboard connectors (§3.3).
#
# Each of the 2 FFC connectors ("slots") exposes:
#   - 7 general-purpose GPIOs labeled A..G  -> slot.gpio["A"]..["G"]
#   - 2 analog inputs A, B on the shared mainboard ADS -> slot.adc["A"/"B"]
#     (an ADS channel index; conversion via cynthia.ads)
#   - both shared I2C buses -> slot.i2c[0], slot.i2c[1]
#   - hardware SPI over any chosen slot GPIOs (ESP32-S3 GPIO matrix)
#     -> slot.spi(sck=..., mosi=..., miso=..., cs=...)
#
# Drivers talk to Slot objects via LOGICAL names only; only cynthia/pins.py
# knows GPIO numbers. Everything user-facing (keyboard, LEDs, pots, display)
# is a DAUGHTERBOARD; the mainboard owns the DAC, the ADS, the battery
# monitor, and the raw slot GPIOs.

import machine
from . import pins

# Shared machine.I2C instances, created lazily (both buses reach both slots).
_i2c_buses = {}


def i2c_bus(bus, scl, sda, freq=400000):
    """Return the shared machine.I2C for `bus`, creating it on first use."""
    if bus not in _i2c_buses:
        _i2c_buses[bus] = machine.I2C(bus, scl=machine.Pin(scl), sda=machine.Pin(sda), freq=freq)
    return _i2c_buses[bus]


class Slot:
    """One daughterboard connector (slot 0 = DB_1, slot 1 = DB_2)."""

    def __init__(self, slot_id):
        if slot_id not in pins.SLOT_GPIOS:
            raise ValueError("invalid slot %r (have 0..%d)" % (slot_id, pins.NUM_SLOTS - 1))
        self.id = slot_id
        self.gpio = {}   # letter -> machine.Pin, created lazily on first use
        # ADS channel indices for this slot's analog pins (mainboard ADS 0..3)
        self.adc = dict(pins.ADC_CHANNELS[slot_id])
        self.i2c = {
            0: i2c_bus(0, pins.I2C_SCL, pins.I2C_SDA),
            1: i2c_bus(1, pins.I2C_DISPLAY_SCL, pins.I2C_DISPLAY_SDA),
        }

    def pin(self, letter, mode=machine.Pin.IN, pull=None):
        """Return (and configure) the machine.Pin for GPIO `letter` (A..G).

        Each pin is configured once; subsequent calls return the same object.
        """
        letter = letter.upper()
        if letter not in pins.SLOT_GPIOS[self.id]:
            raise ValueError("slot %d has no pin %r" % (self.id, letter))
        if letter not in self.gpio:
            self.gpio[letter] = machine.Pin(pins.SLOT_GPIOS[self.id][letter], mode, pull)
        return self.gpio[letter]

    def spi(self, sck, mosi, miso=None, cs=None, baudrate=1000000, polarity=0, phase=0):
        """Hardware SPI over chosen slot GPIOs (ESP32-S3 GPIO matrix, §3.1)."""
        kwargs = dict(baudrate=baudrate, polarity=polarity, phase=phase,
                      sck=self.pin(sck, machine.Pin.OUT),
                      mosi=self.pin(mosi, machine.Pin.OUT))
        if miso is not None:
            kwargs["miso"] = self.pin(miso, machine.Pin.IN)
        spi = machine.SPI(1, **kwargs)
        if cs is not None:
            cs_pin = self.pin(cs, machine.Pin.OUT)
            cs_pin.value(1)
            spi.cs = cs_pin
        return spi

    def release(self):
        """Release all GPIOs this slot configured (called on detach)."""
        self.gpio.clear()

    def __repr__(self):
        return "Slot(%d)" % self.id