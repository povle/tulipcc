# cynthia/naming.py
# Infer a library name for a sketch that arrives without one.
#
# Why this exists: the AMYboard World download path (cynthia/world.py) gets a
# sketch's *source* and *filename* but no explicit display name, and the
# instrument library is folder-based (/user/instruments/<name>/). So the name
# has to be recovered from the source text.
#
# The convention it reads (measured over the live 622-sketch corpus, 2026-10-15):
#
#   # AMYboard Sketch                <- boilerplate, useless as a name
#   # DESCRIPTION: Acid bass synth on MIDI ch 1; M5Stack joystick X=resonance...
#   import amy, amyboard, sequencer
#
# 565/622 (91%) carry the `# DESCRIPTION:` line (usually line 2). It is written
# by the *server's* AI sketch generator, not the web editor, so coverage is
# lower for hand-written uploads — hence the fallback chain below.
#
# Pure stdlib (no `re`, which is large on MicroPython) and no device imports, so
# it is host-testable against the real corpus.

# Comment lines that are template boilerplate, never a name. Matched
# case-insensitively as a prefix. The last four are lines from the *body* of
# stock templates that a sloppy header lets through — measured on the live
# corpus, they produced names like "starting-on-a", "top-level-code" and
# "chunk-1", all of which are worse than falling back to the filename.
_BOILERPLATE = (
    "amyboard sketch",
    "code put here runs first",
    "do not edit",
    "starting on a bar downbeat",
    "top-level code runs once",
    "loop() runs repeatedly",
    "chunk ",   # "CHUNK 1: LINES 1-375" — a paste-detritus banner, not a name
)

# Where a one-line description ends and detail begins. Whichever occurs first
# wins. Sentence ends count: a description is one line, so "." almost always
# means "rest is detail".
_SEPARATORS = (";", ". ", " - ", ": ", "—", "–", ",", "(", "[")

# Decoration-only comment bodies (lines of ====, ----, ****, ...).
_DECORATION_CHARS = "=-*_+~# "

# How far into the file to look for a description.
_MAX_SCAN_LINES = 40


def _comment_body(line):
    """Strip leading '#' and surrounding whitespace from a comment line."""
    s = line.strip()
    if s.startswith("#"):
        s = s[1:]
    return s.strip()


def _is_decoration(body):
    if not body:
        return True
    for ch in body:
        if ch not in _DECORATION_CHARS:
            return False
    return True


def _is_boilerplate(body):
    low = body.lower()
    for b in _BOILERPLATE:
        if low.startswith(b):
            return True
    return False


def description(text):
    """The explicit `# DESCRIPTION:` line from a sketch's source, or None.

    This is the high-confidence source: the server's AI generator is instructed
    to emit exactly this line, and it is a deliberate one-line summary rather
    than incidental comment text.
    """
    if not text:
        return None
    lines = text.split("\n")
    for i in range(min(len(lines), _MAX_SCAN_LINES)):
        s = lines[i].strip()
        if not s.startswith("#"):
            continue
        body = _comment_body(s)
        if body[:12].upper() == "DESCRIPTION:":
            value = body[12:].strip()
            if value:
                return value
    return None


def headline(text):
    """The best one-line description from a sketch's source, or None.

    Prefers the explicit `# DESCRIPTION:` marker (see `description`), then falls
    back to the first meaningful comment line — which catches hand-written
    banner headers like `# DOLPHIN SELECTOR 3 - AMYboard Eurorack Synth`.

    The fallback is deliberately *lower* priority than the filename in
    `suggest_name`: comment text is unreliable (body-of-template lines leak
    through), so it is only used when nothing better exists.

    Scanning stops at the first line of real code, so a comment *inside* the
    body is never mistaken for a header.
    """
    hit = description(text)
    if hit:
        return hit
    if not text:
        return None
    lines = text.split("\n")
    for i in range(min(len(lines), _MAX_SCAN_LINES)):
        s = lines[i].strip()
        if not s:
            continue
        if not s.startswith("#"):
            break  # real code — the header is over
        body = _comment_body(s)
        if _is_decoration(body) or _is_boilerplate(body):
            continue
        return body
    return None


def cut_headline(s):
    """Trim a description down to its headline clause."""
    if not s:
        return ""
    best = len(s)
    for sep in _SEPARATORS:
        at = s.find(sep)
        if at >= 0 and at < best:
            best = at
    return s[:best].strip()


def slugify(s, max_words=3, max_len=18):
    """Lowercase, hyphenated, filesystem-safe slug from a phrase.

    Truncation is whole-word: a word that would overflow `max_len` is dropped
    rather than cut in half ("dolphin-selector-3" -> "dolphin-selector"), so
    slugs never end mid-word. Only if nothing fits is a hard cut used.

    A hyphen is *not* a word separator: "Four-on-the-floor kick" must slug to
    "four-on-the-floor", not "four-on-the" (measured — splitting there produced
    the worst names in the corpus). Only whitespace and other punctuation split
    words.

    Non-alphanumerics (other than the joining hyphen) are dropped, which also
    makes the result safe as a path component: no '/', no '.', no traversal.
    """
    if not s:
        return ""
    # Split on any non-alnum that is not '-', keeping interior hyphens intact.
    # NB: MicroPython's str has no isalnum() (verified on the unix port and so
    # on esp32), so test isalpha()/isdigit() — using isalnum() here silently
    # broke every archive with AttributeError.
    words = []
    cur = []
    for ch in s:
        if ch.isalpha() or ch.isdigit() or ch == "-":
            cur.append(ch.lower())
        else:
            if cur:
                words.append("".join(cur))
                cur = []
    if cur:
        words.append("".join(cur))
    # Trim hyphens that ended up on the edges of a token.
    words = [w.strip("-") for w in words]
    words = [w for w in words if w]

    kept = []
    length = 0
    for w in words:
        if len(kept) >= max_words:
            break
        extra = len(w) if not kept else len(w) + 1
        if kept and length + extra > max_len:
            break
        if not kept and len(w) > max_len:
            # A single long hyphenated token ("karplus-strong-as-string"): keep
            # as many of its parts as fit instead of hard-cutting mid-part.
            parts = []
            plen = 0
            for p in w.split("-"):
                extra2 = len(p) if not parts else len(p) + 1
                if parts and plen + extra2 > max_len:
                    break
                parts.append(p)
                plen += extra2
            kept.append("-".join(parts) if parts else w[:max_len])
            length = len(kept[0])
            break
        kept.append(w)
        length += extra

    if not kept:
        return ""
    return "-".join(kept)[:max_len].strip("-")


def unique(name, taken):
    """`name`, or `name-2`/`name-3`/... if it is already in `taken`."""
    if not name:
        name = "sketch"
    if name not in taken:
        return name
    n = 2
    while ("%s-%d" % (name, n)) in taken:
        n += 1
    return "%s-%d" % (name, n)


def _filename_stem(filename):
    if not filename:
        return ""
    stem = filename.rsplit("/", 1)[-1]
    if stem.lower().endswith(".py"):
        stem = stem[:-3]
    return stem


def suggest_name(text, filename=None, taken=()):
    """Folder name for a sketch, in priority order:

    1. the explicit `# DESCRIPTION:` line, slugged — the server's deliberate
       one-line summary, available for ~91% of published sketches;
    2. the sketch's own filename stem — the API always supplies one, and a
       person's filename ("turning-machine-4", "wavetable") is usually a better
       handle than incidental comment text;
    3. the first header comment line, if any survived boilerplate filtering;
    4. `sketch` as a last resort.

    Note (2) above (3): measured on the live corpus, the header-comment fallback
    produced the *worst* names because body-of-template lines leak through
    ("straight-into-amy", "top-level-code"). The filename is the more reliable
    of the two, so it wins.

    `taken` is the set of existing folder names, so the result never collides.
    """
    base = slugify(cut_headline(description(text) or ""))
    if not base:
        base = slugify(_filename_stem(filename))
    if not base:
        base = slugify(cut_headline(headline(text) or ""))
    return unique(base, taken)
