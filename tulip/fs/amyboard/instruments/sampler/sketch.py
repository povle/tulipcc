# CYNTHIA instrument: Sampler
# Records the ICS-43434 mic into AMY's memory PCM and plays it pitched across
# the keyboard with 8 voices. Port of ~/projects/synth/src/InstrumentSampler.cpp,
# loading through AMY's Python sampler API (amy.load_sample_bytes) rather than
# the Arduino's C pcm_load.
#
# Knob 1 = gain 0..7x, knob 2 = trim in, knob 3 = trim out, button 2 = record.
import amy
from cynthia.instruments.sampler import SamplerInstrument

_engine = SamplerInstrument()
_engine.start()

def cynthia_instrument():
    """Return the instrument engine for the CYNTHIA runtime (§17 hook)."""
    return _engine

def loop(tick):
    pass

# Do not edit. Set automatically by the knobs on AMYboard Online.
_auto_generated_knobs = """
"""
