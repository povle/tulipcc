# CYNTHIA instrument: DX7
# 128 built-in patches (AMY patches 128-255), 8-voice FM.
# Port of ~/projects/synth/src/InstrumentDX7.cpp. Pots are locked (no
# front-panel editing on the DX7); display shows patch name + algorithm.
import amy
from cynthia.instruments.dx7 import DX7Instrument

_engine = DX7Instrument()
_engine.start()

def cynthia_instrument():
    """Return the instrument engine for the CYNTHIA runtime (§17 hook)."""
    return _engine

def loop(tick):
    pass

# Do not edit. Set automatically by the knobs on AMYboard Online.
_auto_generated_knobs = """
"""