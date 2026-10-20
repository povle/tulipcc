# cynthia/library.py
# Turn an uploaded sketch into a selectable instrument.
#
# Why this exists: the AMYboard web editor writes sketches to exactly one path,
# /user/current/sketch.py, and always has. There is no wire field carrying a
# name, and changing the website is out of scope. So "I wrote a sketch in the
# editor and I want it in my CYNTHIA instrument list" has to be a *device-side*
# reaction to receiving that file.
#
# The reaction is a copy, not a move: /user/current/sketch.py keeps running
# exactly as before (the editor's "Write to AMYboard" still means "replace what
# is playing"), and we additionally file the same bytes under
# /user/instruments/<name>/ so the browser can show it alongside the bundled
# instruments. See cynthia/compat.py for the hook and naming.py for how <name>
# is derived.

import os

from . import naming

INSTRUMENTS_DIR = "/user/instruments"


def _log(msg):
    try:
        import tulip
        tulip.stderr_write("cynthia.library: %s" % msg)
    except Exception:
        pass


def folders():
    """Existing instrument folder names."""
    try:
        return os.listdir(INSTRUMENTS_DIR)
    except OSError:
        return []


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def install(name, text, meta=None):
    """Write /user/instruments/<name>/{sketch.py,meta.json}. Returns the name.

    Order matters: `meta.json` is written FIRST and `sketch.py` last, because
    `list_instruments()` gates on the presence of `sketch.py`. So if /user fills
    up mid-write, the worst case is a folder with only metadata (invisible to the
    browser) rather than a folder that looks like a usable instrument but whose
    sketch is truncated — which would run as a broken instrument and, at worst,
    trip the boot guard.
    """
    if isinstance(text, str):
        text = text.encode("utf-8")
    folder = INSTRUMENTS_DIR + "/" + name
    try:
        os.mkdir(INSTRUMENTS_DIR)
    except OSError:
        pass
    try:
        os.mkdir(folder)
    except OSError:
        pass
    if meta is not None:
        import json
        with open(folder + "/meta.json", "w") as f:
            json.dump(meta, f)
    _write(folder + "/sketch.py", text)
    return name


def to_text(data):
    """Bytes -> str, tolerating non-UTF-8.

    MicroPython's bytes.decode() takes no `errors` argument (it raises
    "extra keyword arguments given"), so the usual decode("utf-8","replace")
    crashes there — measured. Sketches are user content and do occasionally carry
    stray high bytes, so this must never raise: decode strictly where possible,
    else fall back to a lossy byte-wise pass.
    """
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except Exception:
        pass
    try:
        # latin-1 maps every byte to a codepoint, so it cannot fail.
        return data.decode("latin-1")
    except Exception:
        return "".join(chr(b) for b in data)


def is_default_sketch(text):
    """True for the placeholder AMYboard/`ensure_user_environment` seeds.

    That file is written by the board itself (fresh /user, or the self-heal in
    amyboard.run_sketch), never uploaded by a person, so it must not become an
    instrument. Matched on its distinctive marker line rather than the whole
    body so small edits to the template don't break this.
    """
    return "Do not edit. Set automatically by the knobs" in to_text(text)


def content_in_library(text):
    """The folder name of an existing instrument with identical sketch.py, or None."""
    if isinstance(text, str):
        text = text.encode("utf-8")
    for name in folders():
        data = _read(INSTRUMENTS_DIR + "/" + name + "/sketch.py")
        if data is not None and data == text:
            return name
    return None


def _slug_of(text, filename=None):
    """The slug a sketch would get, ignoring collision suffixes.

    Kept in step with naming.suggest_name: DESCRIPTION, then filename, then the
    header-comment fallback.
    """
    text = to_text(text)
    base = naming.slugify(naming.cut_headline(naming.description(text) or ""))
    if not base and filename:
        base = naming.slugify(naming._filename_stem(filename))
    if not base:
        base = naming.slugify(naming.cut_headline(naming.headline(text) or ""))
    return base


def archive(text, filename=None, meta=None, replace=False):
    """File an uploaded sketch under /user/instruments/. Returns the folder
    name, or None if nothing was written.

    Idempotent, because the transfer-done callback fires **twice** per upload
    (the C hook in amy_connector.c and the editor's explicit
    `zP...environment_transfer_done()` both reach this), and because a person
    may hit "Write" repeatedly with the same sketch:

      already in the library byte-for-byte  -> no-op, whatever `replace` says

    Otherwise the sketch gets its **own** folder. A slug collision — a genuinely
    different sketch whose description shares a prefix — is suffixed (`-2`),
    never merged. That default is deliberate: measured over the live corpus, 117
    slugs are shared by more than one distinct sketch (350 sketches would be
    silently merged if a slug match meant "overwrite"). Names are the user's
    handle on their library, so losing one is worse than a `-2`.

    Pass `replace=True` to opt into the opposite policy: a slug match is treated
    as an edit of that instrument and overwrites it in place. Useful for
    iterating on one sketch; never the default because it cannot tell an
    iteration from a coincidence.
    """
    if not text or is_default_sketch(text):
        return None

    # Work in str throughout (naming/headline parsing is text); encode once at
    # the write. Lossy decoding only affects the *name* heuristic, never the bytes
    # we install.
    text = to_text(text)

    if content_in_library(text) is not None:
        return None  # already filed, nothing to do

    taken = folders()

    if meta is None:
        # meta.json.name is what the REPL menu shows (the OLED grid shows the
        # folder slug, see draw_instrument_grid). The full headline reads better
        # there than a truncated slug, so derive it from the same heuristic.
        nice = naming.cut_headline(naming.headline(text) or "")
        if nice:
            meta = {"name": nice}

    if replace:
        slug = _slug_of(text, filename)
        if slug and slug in taken:
            _log("replacing instrument %r" % slug)
            return install(slug, text, meta)

    name = naming.suggest_name(text, filename, taken=taken)
    _log("adding instrument %r" % name)
    return install(name, text, meta)
