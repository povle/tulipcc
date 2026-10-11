# cynthia — CYNTHIA mainboard support package.
#
# The CYNTHIA mainboard is a headless ESP32-S3 synth core: PCM5102A DAC
# over I2S, an ADS1115 ADC on I2C, battery monitoring, and two FFC
# daughterboard slots. Everything user-facing (keyboard, LEDs, pots) is a
# daughterboard; see cynthia.registry / daughterboard.json.

from . import pins
from .connector import Slot