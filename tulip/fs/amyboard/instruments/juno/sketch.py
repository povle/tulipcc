# CYNTHIA instrument: Juno-106
# 128 built-in patches, 6-voice polyphony, full pot editing.
# Port of ~/projects/synth/src/InstrumentJuno.cpp + JunoUI.cpp.
import amy
from cynthia.instruments.juno import JunoInstrument

_engine = JunoInstrument()
_engine.start()

def cynthia_instrument():
    """Return the instrument engine for the CYNTHIA runtime (§17 hook)."""
    return _engine

def loop(tick):
    pass

# Do not edit. Set automatically by the knobs on AMYboard Online.
_auto_generated_knobs = """
"""