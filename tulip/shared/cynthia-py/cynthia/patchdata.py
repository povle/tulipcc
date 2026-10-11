# cynthia/patchdata.py
# Patch metadata for the CYNTHIA instruments.
#
# AMY ships 391 built-in patches: Juno-106 at indices 0-127, DX7 at 128+.
# amy.send(synth=N, patch=idx) plays them. This module holds the display
# metadata (names, DX7 algorithm diagrams) that AMY does not expose.
#
# DX7 algorithms are transcribed from ~/projects/synth/src/dx7_algos.h.
# Each algo is (top, bottom, carriers) where top/bottom are lists of
# (a, b) operator connections (a==b means feedback on operator a).
# Encoded compactly as hex-digit pairs to save flash: "552345" -> [(5,5),(2,3),(4,5)].

def _decode(s):
    """Decode a hex-pair connection string into a list of (a, b) tuples."""
    return [(int(s[i], 16), int(s[i + 1], 16)) for i in range(0, len(s), 2)]


# DX7 algorithms, index 0 = Algorithm 1. (top, bottom, carriers)
DX7_ALGOS = [
    (_decode("552345"), _decode("0234"), (0, 2)),        # 1
    (_decode("112345"), _decode("0234"), (0, 2)),        # 2
    (_decode("013455"), _decode("031245"), (0, 3)),      # 3
    (_decode("013435"), _decode("031245"), (0, 3)),      # 4
    (_decode("01234555"), _decode("0224"), (0, 2, 4)),   # 5
    (_decode("012345"), _decode("022445"), (0, 2, 4)),   # 6
    (_decode("552345"), _decode("0224"), (0, 2)),        # 7
    (_decode("332345"), _decode("0224"), (0, 2)),        # 8
    (_decode("112345"), _decode("0224"), (0, 2)),        # 9
    (_decode("0112223454"), _decode("03"), (0, 3)),      # 10
    (_decode("0112553454"), _decode("03"), (0, 3)),      # 11
    (_decode("01112324"), _decode("0225"), (0, 2)),      # 12
    (_decode("01552324"), _decode("0225"), (0, 2)),      # 13
    (_decode("550123"), _decode("023435"), (0, 2)),      # 14
    (_decode("110123"), _decode("023435"), (0, 2)),      # 15
    (_decode("55010204"), _decode("2345"), (0,)),        # 16
    (_decode("11010204"), _decode("2345"), (0,)),        # 17
    (_decode("01032245"), _decode("34"), (0,)),          # 18
    (_decode("0112553545"), _decode("0334"), (0, 3, 4)), # 19
    (_decode("2212343502"), _decode("0113"), (0, 1, 3)), # 20
    (_decode("2202123545"), _decode("022334"), (0, 1, 3, 4)),  # 21
    (_decode("0125354555"), _decode("022334"), (0, 2, 3, 4)),  # 22
    (_decode("12354555"), _decode("011334"), (0, 1, 3, 4)),    # 23
    (_decode("25354555"), _decode("01122334"), (0, 1, 2, 3, 4)),  # 24
    (_decode("354555"), _decode("01122334"), (0, 1, 2, 3, 4)),    # 25
    (_decode("55123435"), _decode("0113"), (0, 1, 3)),   # 26
    (_decode("22123435"), _decode("0113"), (0, 1, 3)),   # 27
    (_decode("01233444"), _decode("0225"), (0, 2, 5)),   # 28
    (_decode("234555"), _decode("011224"), (0, 1, 2, 4)),  # 29
    (_decode("233444"), _decode("011225"), (0, 1, 2, 5)),  # 30
    (_decode("4555"), _decode("01122334"), (0, 1, 2, 3, 4)),    # 31
    (_decode("55"), _decode("0112233445"), (0, 1, 2, 3, 4, 5)), # 32
]


# First 128 DX7 patch names + algorithm numbers (1-32), from dx7_patches.h.
# Index i -> AMY patch number i + 128.
DX7_PATCHES = [
    ("BRASS   1", 22), ("BRASS   2", 22), ("BRASS   3", 18), ("STRINGS 1", 2),
    ("STRINGS 2", 2), ("STRINGS 3", 15), ("ORCHESTRA", 2), ("PIANO   1", 19),
    ("PIANO   2", 18), ("PIANO   3", 3), ("E.PIANO 1", 5), ("GUITAR  1", 8),
    ("GUITAR  2", 16), ("SYN-LEAD 1", 18), ("BASS    1", 16), ("BASS    2", 17),
    ("E.ORGAN 1", 32), ("PIPES   1", 19), ("HARPSICH 1", 5), ("CLAV    1", 3),
    ("VIBE    1", 23), ("MARIMBA", 7), ("KOTO", 2), ("FLUTE   1", 16),
    ("ORCH-CHIME", 5), ("TUB BELLS", 5), ("STEEL DRUM", 15), ("TIMPANI", 16),
    ("REFS WHISL", 18), ("VOICE   1", 7), ("TRAIN", 5), ("TAKE OFF", 10),
    ("PIANO   4", 18), ("PIANO   5", 3), ("E.PIANO 2", 12), ("E.PIANO 3", 5),
    ("E.PIANO 4", 5), ("PIANO 5THS", 5), ("CELESTE", 31), ("TOY PIANO", 30),
    ("HARPSICH 2", 3), ("HARPSICH 3", 3), ("CLAV    2", 4), ("CLAV    3", 4),
    ("E.ORGAN 2", 29), ("E.ORGAN 3", 29), ("E.ORGAN 4", 5), ("E.ORGAN 5", 29),
    ("PIPES   2", 3), ("PIPES   3", 25), ("PIPES   4", 6), ("CALIOPE", 16),
    ("ACCORDION", 3), ("SITAR", 8), ("GUITAR  3", 14), ("GUITAR  4", 14),
    ("GUITAR  5", 14), ("GUITAR  6", 3), ("LUTE", 14), ("BANJO", 8),
    ("HARP    1", 3), ("HARP    2", 3), ("BASS    3", 17), ("BASS    4", 17),
    ("PICCOLO", 5), ("FLUTE   2", 18), ("OBOE", 3), ("CLARINET", 17),
    ("SAX BC", 18), ("BASSOON", 2), ("STRINGS 4", 2), ("STRINGS 5", 2),
    ("STRINGS 6", 15), ("STRINGS 7", 2), ("STRINGS 8", 2), ("BRASS   4", 22),
    ("BRASS   5", 2), ("BRASS 6 BC", 22), ("BRASS   7", 8), ("BRASS   8", 22),
    ("RECORDER", 6), ("HARMONICA1", 1), ("HRMNCA2 BC", 1), ("VOICE   2", 7),
    ("VOICE   3", 5), ("GLOKENSPL", 7), ("VIBE    2", 5), ("XYLOPHONE", 7),
    ("CHIMES", 6), ("GONG    1", 16), ("GONG    2", 17), ("BELLS", 27),
    ("COW BELL", 6), ("BLOCK", 18), ("FLEXATONE", 7), ("LOG DRUM", 14),
    ("SYN-LEAD 2", 22), ("SYN-LEAD 3", 22), ("SYN-LEAD 4", 22), ("SYN-LEAD 5", 1),
    ("SYN-CLAV 1", 16), ("SYN-CLAV 2", 7), ("SYN-CLAV 3", 2), ("SYN-PIANO", 5),
    ("SYNBRASS 1", 22), ("SYNBRASS 2", 22), ("SYNORGAN 1", 22), ("SYNORGAN 2", 3),
    ("SYN-VOX", 25), ("SYN-ORCH", 25), ("SYN-BASS 1", 3), ("SYN-BASS 2", 9),
    ("HARP-FLUTE", 3), ("BELL-FLUTE", 5), ("E.P-BRS BC", 5), ("T.BL-EXPA", 5),
    ("CHIME-STRG", 5), ("B.DRM-SNAR", 3), ("SHIMMER", 5), ("EVOLUTION", 1),
    ("WATER GDN", 32), ("WASP STING", 18), ("LASER GUN", 31), ("DESCENT", 1),
    ("OCTAVE WAR", 7), ("GRAND PRIX", 17), ("ST.HELENS", 17), ("EXPLOSION", 16),
    ("FLUTE   1", 16),
]

# Juno-106 patch names (indices 0-127), from juno_patches.h.
# These match amy.juno.get_juno_patch() ordering.
JUNO_NAMES = [
    "Brass Set 1", "Brass Swell", "Trumpet", "Flutes",
    "Moving Strings", "Brass & Strings", "Choir", "Piano I",
    "Organ I", "Organ II", "Combo Organ", "Calliope",
    "Donald Pluck", "Celeste*", "Elect. Piano I", "Elect. Piano II",
    "Clock Chimes*", "Steel Drums", "Xylophone", "Brass III",
    "Fanfare", "String III", "Pizzicato", "High Strings",
    "Bass clarinet", "English Horn", "Brass Ensemble", "Guitar",
    "Koto", "Dark Pluck", "Funky I", "Synth Bass I",
    "Lead I", "Lead II", "Lead III", "Funky II",
    "Synth Bass II", "Funky III", "Thud Wah", "Going Up",
    "Piano II", "Clav", "Frontier Organ", "Snare Drum",
    "Tom Toms", "Timpani", "Shaker", "Synth Pad",
    "Sweep I", "Pluck Sweep", "Repeater", "Sweep II",
    "Pluck Bell", "Dark Synth Piano", "Sustainer", "Wah Release",
    "Gong", "Resonance Funk", "Drum Booms*", "Dust Storm",
    "Rocket Men", "Hand Claps", "FX Sweep", "Caverns",
    "Strings", "Violin", "Chorus Vibes", "Organ 1",
    "Harpsichord 1", "Recorder", "Perc. Pluck", "Noise Sweep",
    "Space Chimes", "Nylon Guitar", "Orchestral Pad", "Bright Pluck",
    "Organ Bell", "Accordion", "FX Rise 1", "FX Rise 2",
    "Brass", "Helicopter", "Lute", "Chorus Funk",
    "Tomita", "FX Sweep 1", "Sharp Reed", "Bass Pluck",
    "Resonant Rise", "Harpsichord 2", "Dark Ensemble", "Contact Wah",
    "Noise Sweep 2", "Glassy Wah", "Phase Ensemble", "Chorused Bell",
    "Clav", "Organ 2", "Bassoon", "Auto Release Noise",
    "Brass Ensemble", "Ethereal", "Chorus Bell 2", "Blizzard",
    "E. Piano w/ Tremolo", "Clarinet", "Thunder", "Reedy Organ",
    "Flute / Horn", "Toy Rhodes", "Surf's Up", "OW Bass",
    "Piccolo", "Melodic Taps", "Meow Brass", "Violin (high)",
    "High Bells", "Rolling Wah", "Ping Bell", "Brassy Organ",
    "Low Dark Strings", "Piccolo Trumpet", "Cello", "High Strings",
    "Rocket Men", "Forbidden Planet", "Froggy", "Owgan",
]