# AMYboard World on CYNTHIA — Research Findings

**Status: §9 (archive), §10 (encoder facade), §12–§13 (robustness + software
watchdog) and §15 (beta hardening) are IMPLEMENTED and, where noted, verified on
hardware. §3 (inbound World browsing) remains research only.**
Written 2026-10-15; implementation notes 2026-10-16 .. 2026-10-19.

Author's constraint for this phase: *"while we are in the R&D phase I'd prefer not
touching other people's work"* — i.e. no changes to `tulip/amyboardweb/` (the web
editor), and minimal CYNTHIA-side surface. Both held: the web editor is untouched
(`git diff` on it is empty), and the only device changes are two new frozen
modules plus one wrapped function in `cynthia/compat.py`.

**Scoping correction (2026-10-16):** the first attempt at §3 built a *device-side
HTTP client* (wifi credentials, listing cache, on-device download). That was out
of scope — the ask was for the **browser** to load sketches into CYNTHIA, using
the network the browser already has. There is **no on-board wifi work in this
change** (`cynthia/world.py` was written and then deleted; the browser-menu entry
was reverted). See §4-implemented below.

Companion docs: `SESSION_CONTEXT.md` (§17 boot/instrument flow, §17a compat, §11
pointer to this file), `PORT_NOTES.md`, `PATCHES.md`.

---

## 0. Goal

A portable synth where the AMYboard World sketch library *is* the instrument
library. Two directions:

- **Inbound** — browse/fetch sketches from amyboard.com into the device's
  instrument list (cache-on-fetch; see §3).
- **Outbound** — a sketch written in the web editor also lands in the device's
  instrument list, so it can be re-selected later like any other instrument.

This note covers measurements and the exact code path for both.

---

## 1. Taxonomy: patch vs instrument vs sketch (settled)

| | Patch | Instrument | AMYboard World sketch |
|---|---|---|---|
| Scope | parameters *inside* an instrument | *is* the whole instrument | *is* the whole instrument |
| Storage | `/user/patches/<inst>/<n>.json` | `/user/instruments/<name>/sketch.py` | same as instrument |
| Owned by | Juno/DX7 engine | runtime mode manager | runtime mode manager |
| UI | overlay during PLAY | 2×3 grid menu | same grid (it's an instrument) |

**Consequence:** "browse World" is *not* a new mode — it is a second **source**
for the existing instrument browser (`_menu_insts`). A patch is not comparable
(a patch lives inside an instrument); a sketch is an instrument.

---

## 2. Library size measurements (the "ship it all?" question)

Measured against the live API on 2026-10-15, not estimated.

Endpoint: `GET /api/amyboardworld/files?limit=N&latest_per_user_env=true&item_type=environment`

**Row count.** The `limit` default truncates silently. With `latest_per_user_env=true`
(dedupes to newest row per user+filename) the count **saturates at 622 sketches**
(requesting limit=1000 or 2000 returns 622; 200/500 return exactly the limit, so
those are truncated, not totals).

| limit | rows returned |
|---|---|
| 200 | 200 (truncated) |
| 500 | 500 (truncated) |
| 1000 | 622 (saturated) |
| 2000 | 622 (saturated) |

Server-side: `_list_file_rows(..., limit=limit, ...)` — no total/count is returned
in the response body (`{"items": [...]}` only). The real counts live behind the
admin-gated `/api/world/debug/storage`.

### 2b. The corpus is 79% one automated account — big finding

Uploader distribution across all 622 (from the `username` field):

| User | Sketches | Share |
|---|---|---|
| **`generator`** | **491** | **79%** |
| dpwe | 21 | |
| shorepine | 21 | |
| bwhitman | 13 | |
| AntoineDoinel | 12 | |
| Gero | 9 | |
| wheatishbrown | 8 | |
| drbooka | 6 | |
| …28 more | … | |

`generator` is the server-side AI flow (`amyboardworld_db_api.py` Claude generator,
the same one that mandates `# DESCRIPTION:`, §4d). Its 491 sketches are largely
near-duplicates — e.g. `acid-bass-synth` slug appears **9 times**, all `generator`.

Splitting by author changes the size picture substantially:

| Cohort | n | raw | zlib -9 |
|---|---|---|---|
| **ALL** | 622 | 5.13 MB | 1.73 MB |
| `generator` (AI) | 491 | 1.97 MB | **0.70 MB** |
| **human-authored** | **131** | 3.17 MB | **1.03 MB** |

**Consequence:** the "whole library" is really ~131 human instruments / **1.03 MB
compressed** — which fits `vfs` (2.29 MB) with **1.26 MB to spare**, and would fit
even without deduping generator output away. The 622-row / 1.73 MB figure is an
artifact of AI spam. If a shipped corpus ever happens, ship the human cohort.

(Also relevant for UX: a browser showing 622 entries where 79% are AI variations of
the same handful of ideas is a bad menu. Dedupe/filter by author or by slug
similarity is a *browsing* concern, independent of storage.)

**Byte totals** (all 622 downloaded and compressed locally):

| Form | Total | Ratio | vs 2.29 MB `vfs` |
|---|---|---|---|
| raw `.py` | **5.13 MB** (4.89 MiB) | 1.00× | ✗ 2.2× over |
| `.py` + zlib -9 | **1.73 MB** (1.65 MiB) | 2.97× | ✓ fits, ~0.56 MB spare |
| `.mpy` (mpy-cross `-march=xtensawin -O2`) | **2.16 MB** (2.06 MiB) | 2.38× | ✗ just over |
| **`.mpy` + zlib -9** | **1.37 MB** (1.31 MiB) | 3.75× | ✓ fits, ~0.92 MB spare |

All figures **measured**, not projected: the full 622-file corpus was compiled with
the repo's own `micropython/mpy-cross/build/mpy-cross`. **621/622 compiled; 1
genuine SyntaxError** (`549.py:95` — a broken sketch in the live corpus, worth
noting as real-world evidence that downloaded content does need validating).

Size distribution: median **3,483 B**, mean **8,249 B**, p90 **9,684 B**,
max **293,965 B** (`Dolphin_selector_3.py`).

Interesting asymmetry: zlib beats `.mpy` for *storage* (1.73 MB vs 2.16 MB), because
mpy-cross trades file size for load speed. `.mpy`+zlib wins overall (1.37 MB), but
if storage is the binding constraint, shipping `zlib(.py)` and compiling on first
load is smaller — at the cost of a one-time compile on device. (`.mpy` would also
need no compile step at all, which matters for the boot path.)

### 2a. Flash headroom — the actual constraint

The partition table tiles the 16 MiB flash **exactly** (0 free — verified, sum of
sizes == 0x1000000):

```
ota_0   0x010000 .. 0x3A0000   3.74 MB  (3.56 MiB)   app, round 1
ota_1   0x3A0000 .. 0x730000   3.74 MB  (3.56 MiB)   app, round 2 — OTA
system  0x730000 .. 0xA30000   3.15 MB  (3.00 MiB)   /sys, baked at flash time
vfs     0xA30000 .. 0xC60000   2.29 MB  (2.19 MiB)   /user, littlefs
drums   0xC60000 .. 0x1000000  3.80 MB  (3.62 MiB)   raw int16 PCM, mmapped by AMY
```

Two observations:

1. **`/sys` reserves 3.15 MB but ships only ~1.3 MB** (`fs/amyboard/` = 1.3 MB
   `ex/` + 16 KB `instruments/`). ~1.8 MB allocated-but-empty — the natural home
   for a shipped corpus, if we ever want one. Note `/sys` is written by
   `fs_create.py` at flash time, so shipping there means reflashing to update.
2. **`drums` is 3.80 MB for a 3.74 MB `drums.bin`** — only ~65 KB slack
   (verified: `amy/build/drums.bin` = 3,735,788 B vs partition 3,801,088 B).
   Rebalancing partitions has less room than it first looks.

**Decision: do not ship the corpus by default.** Cache-on-fetch (below). If a
shipped corpus is ever revisited, ship the **human cohort only** (§2b: 131 sketches,
1.03 MB compressed — fits `vfs` with 1.26 MB spare), not the 622-row corpus.

#### 2c. There is already a bundled-instrument mechanism (`seed_defaults`)

Discovered while verifying the inbound path — `cynthia/browser.py:20-68` already
implements exactly the seeding pattern a shipped corpus would need:

```
BUNDLED_INSTRUMENTS_DIR = "/sys/instruments"     # read-only system partition
INSTRUMENTS_DIR         = "/user/instruments"

seed_defaults():   # idempotent — copies /sys/instruments/<name>/ into
                   # /user/instruments/<name>/ for any not already present,
                   # only for folders containing sketch.py, copying
                   # sketch.py + meta.json
```

`list_instruments()` calls `seed_defaults()` on every invocation, so it runs
automatically. `_boot.py:198` calls `cynthia_config.seed_default()`
(daughterboard config — a *different*, similarly-named function; don't confuse
them).

**Consequence:** shipping a starter library does **not** require new machinery —
it is "put folders in `tulip/fs/amyboard/instruments/`" (currently 16 KB, holding
`dx7/` and `juno/`, each with `sketch.py` + `meta.json`) and `fs_create.py` bakes
them into `/sys`. The `meta.json` sidecar is already read for display names. So
§2a's "ship the human cohort" option is a *content* decision, not an engineering
one — and the two stock instruments would be the seed of it.

**Note the two similarly-named seeders** (easy to confuse):
- `cynthia.browser.seed_defaults()` — instruments, `/sys/instruments` → `/user/instruments`.
- `cynthia.config.seed_default()` — daughterboard wiring JSON. Called from `_boot.py:198`.
- (`amyboard.ensure_user_environment()` is a third, separate thing — the
  `/user/current` layout, called earlier in `_boot.py`.)

---

## 3. Inbound path (World → device)

> **Status: NOT implemented** (and the on-board-network approach was dropped as
> out-of-scope 2026-10-16 — the browser has the network, the device does not need
> it). Everything below is design analysis. The *outbound* direction is the one
> that shipped; see §9.

### 3a. What already works

`world.py` (frozen, already imported at CYNTHIA boot) provides:

- `world.amyboard.ls(count=10)` — prints a listing.
- `world.amyboard.download(sketch, username=None, start=True)` — resolves via
  `q=`-narrowed listing (newest-first, first exact-filename match wins), fetches
  `download_url`, writes `/user/current/sketch.py`.

HTTPS works with **no cert bundle**: `tulip/shared/py/tuliprequests.py:122` sets
`context.verify_mode = tls.CERT_NONE`. `MICROPY_PY_SSL` + `MICROPY_SSL_MBEDTLS` are
on at `tulip/amyboard/mpconfigport.h:201-202` (port-level, so inherited by
CYNTHIA); `esp_wifi`/`esp_netif` are in the IDF component list
(`esp32_common.cmake:269,277`). No new plumbing needed.

**Note — the API returns more than `download()` uses.** A listing item carries 13
fields (`age_ms`, `content`, `description`, `download_url`, `filename`, `id`,
`item_type`, `kind`, `scope`, `size`, `tags`, `time`, `username`). Crucially:

- **`description`** is present and human-readable — e.g. `"Dolphin_CV_1_V6
  (Eurorack edition, v13.1): MicroPython control system for the AMYboard sy…"`.
  This is a *better* library label than anything the naming heuristic (§4d) can
  recover from file content, and it arrives with the listing — **no extra fetch**.
- **`username`** is present → attribution is free.
- **`tags`** is present (often `[]`, but populated for some).

So the *inbound* path has real metadata available; only the *outbound* path (§4)
lacks a name. Worth remembering when the browser UI is designed.

### 3b. What's missing

1. **Credentials.** No on-device entry path. Options considered: cobbled
   keyboard from 28 keys + 4 buttons (clunky); `/user/wifi.json` written via
   web-editor REPL or serial paste (cheap); captive portal (a real feature).
   Preference stated: **web interface over cable/BT for now**.
2. **Listing cache.** Menu-open must not depend on a network round-trip, and the
   622-item listing is **306 KB of JSON — measured, not estimated** (requesting
   `limit=1000&latest_per_user_env=true&item_type=environment` returns 313,428
   bytes; the earlier ~100 KB figure in this note was wrong). Too big to hold in
   RAM as a whole on a headless device → cache to `/user/amyworld.json`, refresh
   on demand, and load it streaming/partially for the menu. Makes the browser
   work offline.
   - Trimming fields would help a lot: `content` and `description` are near-
     duplicates of each other and dominate the payload. A slimmed
     `{filename, username, size, tags}` projection is a small fraction of 306 KB.
3. **`download()` targets `/user/current/` and auto-starts.** For a library
   import we want `/user/instruments/<name>/sketch.py` + `meta.json`, then select
   — not replace-and-run.

### 3c. Proposed shape (not implemented)

- `/user/wifi.json` → connect on demand (browser-open or download), disconnect after.
- `/user/amyworld.json` → listing cache (slimmed projection — see §3b.2).
- Remote entries merged into the existing instrument grid, marked by state
  (e.g. `~name` cached, `↓name` not yet fetched).
- Names/metadata from the API's `description`/`username`/`tags` (§3a) rather than
  a content heuristic — inbound is the easy direction for naming.
- Download → validate (`compile()`) → store → appears as a normal instrument.

---

## 4. Outbound path (device → device library), no website changes

> **Status: IMPLEMENTED (design A).** See §9 for what shipped and how it was
> verified. This section is the analysis behind it.

### 4a. The blocking fact

The web editor writes to **`/user/current/sketch.py` and nothing else**:

```
spss.js:5247   _send_text_file_to_amyboard('/user/current/sketch.py', sketchText)   # "Write to AMYboard"
spss.js:3655   _send_text_file_to_amyboard(sketchPath, opts.sketchText)             # post-zB upload
               where sketchPath = opts.sketchPath || '/user/current/sketch.py'
```

`opts.sketchPath` **is already plumbed** and `_send_text_file_to_amyboard(path, text)`
already takes an arbitrary path — but **nobody overrides it**. And `packageName`
(spss.js:3215/3680) only sets `#editor_filename` in the DOM; **it never crosses the
wire**.

Therefore at upload time the device receives: **bytes + the path string
`/user/current/sketch.py`.** No name, no author, no tags.

### 4b. Device-side receive path is fully generic

Verified end-to-end — no path whitelist anywhere:

| Layer | Detail |
|---|---|
| Protocol | `zT<path>,<size>Z` + base64 chunks, ACK'd |
| `amy/src/transfer.c:231` | `start_receiving_file_transfer(length, filename)` — rejects only NULL/empty; opens via `amy_external_fopen_hook` |
| `amy_connector.c:328` | `mp_fopen_hook` → `tulip_fopen(filename, mode)` → MicroPython VFS. Any path works, including `/user/instruments/<n>/sketch.py` |
| `amy/src/transfer.c:275` | On completion: `sequencer_midi_start()` then `amy_external_file_transfer_done_hook(filename)` |

**One device-side wart:** `mp_file_transfer_done_hook` (`amy_connector.c:413`)
compares the **basename** to `"sketch.py"` and schedules
`tulip_environment_transfer_done()` → `restart_sketch()`. So *any* uploaded file
named `sketch.py`, **anywhere**, restarts the running sketch. A library import at
`/user/instruments/<n>/sketch.py` would needlessly restart. Scoping that check to
`/user/current/` (~5 lines of C) is arguably a correctness fix regardless.

### 4c. The zero-website-change design: archive on write

The web tool always sends, immediately after the transfer:

```
zPimport amyboard; amyboard.environment_transfer_done()Z
```

`zP` executes **arbitrary Python on device** (via `mp_exec_hook`,
`amy_connector.c:397`). So the device can act entirely on its own:

> In `environment_transfer_done()` (`amyboard-py/amyboard.py:432`), *in addition to*
> restarting: copy the just-received `/user/current/sketch.py` into
> `/user/instruments/<name>/` (+ `meta.json`), where `<name>` is inferred from the
> sketch content.

Same bytes on the wire, no protocol change, no website change. The library
accumulates whatever has been flashed.

#### 4c-bis. The restart currently fires TWICE per write (verified)

Worth knowing before putting archive logic in `environment_transfer_done()`,
because that function is called **twice** for a single "Write to AMYboard":

1. **C side.** `zT` completes → `mp_file_transfer_done_hook`
   (`amy_connector.c:423`) sees basename `sketch.py` → `mp_sched_schedule(
   tulip_environment_transfer_done_obj)`. That wrapper (`amy_connector.c:390`)
   imports `amyboard` and calls `environment_transfer_done()` on the MP main loop.
2. **Web side.** `spss.js:5249` then sends
   `zPimport amyboard; amyboard.environment_transfer_done()Z`, which runs the
   *same* function again via `mp_exec_hook`.

Both are installed/emitted for CYNTHIA (verified: the hook is set in `run_amy()` at
`amy_connector.c:491`; the `zP` is unconditional in the control-mode branch). So
`restart_sketch()` runs twice — `stop_sketch()` + `run_sketch()` each time.

Implications:

- **Any archive logic must be idempotent.** On the second call the file is already
  in the library, so the "skip if byte-identical" rule (§4e.2) is required, not
  optional — otherwise every write files two copies (or races the same path twice).
- This also means the existing double `restart_sketch()` is today's behavior on
  AMYboard too; it appears harmless in practice (each restart is a full teardown),
  but it does mean the upload path is doing 2× the work.
- A cheaper framing may be to put the archive in the **`zP`-only** path (a
  distinct function the web already calls), but the web is not being changed, so
  the device has to make `environment_transfer_done()` itself idempotent.


### 4d. Naming heuristic (accepted) — and the `# DESCRIPTION:` convention

**Correction to an earlier guess in this session:** there is no `# name:` marker in
the corpus — `grep -lEi '^#\s*(name|title)\s*:'` matches **0 files**. The real
convention is different and much stronger:

```
# AMYboard Sketch                 <- stock first line
# DESCRIPTION: Acid bass synth on MIDI ch 1; M5Stack joystick X=resonance, Y=filter
#   cutoff. Displays joystick position and detection status on OLED.
import amy, amyboard, sequencer
```

Measured across the full 622-file corpus (2026-10-15):

| Marker | Files | Share |
|---|---|---|
| `# DESCRIPTION:` line present | **565** | **90.8%** |
| stock `# AMYboard Sketch` first line | high | boilerplate, useless as a name |
| `# name:` / `# title:` | **0** | none |

Position: the `# DESCRIPTION:` line is line 2 in 181/200 sampled, line 3 in 15 — so
reading the first few comment lines suffices. The text is a genuine one-line human
description (*"Acid bass synth on MIDI ch 1; M5Stack joystick X=resonance, Y=filter
cutoff…"*), much better than the Dolphin file's ad-hoc banner.

#### Where `# DESCRIPTION:` comes from — this matters a lot

It is **not** written by the web editor. It is mandated by the **server's Claude-
based sketch generator** (`amyboardworld_db_api.py:1367`):

> `- Begin with comment lines, including one line of the form: # DESCRIPTION: <short summary>`

So the convention holds for sketches **generated through the website's AI flow**
(and all 23 bundled `amyboardweb/sketches/*.py` samples carry it, because they are
generator output). It is *not* guaranteed for:

- sketches **hand-written or hand-edited** in the editor (the editor's own default
  template is `# AMYboard Sketch` / `# Code put here runs first…` — no DESCRIPTION);
- the 57/622 corpus files that lack it (e.g. `1587.py`, `1574.py` which starts
  straight at `import amy`).

**Consequence for outbound (§4):** a "Write to AMYboard" from the editor sends
whatever text is in the editor. If the user generated it via the AI flow, it has
`# DESCRIPTION:`; if they hand-wrote it, it may not. So the heuristic's coverage on
the *outbound* path is **unknown and probably lower than 91%** — the 91% figure
describes the *published corpus*, which is skewed toward generator output. This is
the honest caveat: the fallback path matters more than the corpus suggests.

**`# DESCRIPTION:` is a sentence, not a slug.** A library folder name wants
something short (the 2×3 grid draws names in a small font, `SESSION_CONTEXT.md`
§17). The heuristic needs truncation/slugging on top: cut at the first `;`, `:`,
`—`, or ~20 chars; lowercase; strip non-alphanumerics.

**Collision rate — measured** (slug = first 3 words of the description, cut at the
first `;`/`:`/`—`, lowercased, non-alphanumerics stripped, ≤24 chars):

| Scheme | Distinct slugs | Colliding entries |
|---|---|---|
| slug alone | 360 / 622 | **205** (33%) |
| `username/slug` | 394 / 622 | 171 (27%) |

So a naive slug collides on a third of the corpus — but almost entirely *within* the
`generator` account (§2b): the top collisions are all `generator`
(`acid-bass-synth` ×9, `juno-presets-on` ×8, `rotary-encoder-scrolls` ×7). Real
users' sketches are far more distinct.

**Implication:** collisions are mostly an artifact of AI spam, so the fix is not a
better slug — it's `username/slug` for inbound (§3, where username is known) plus a
`-2`, `-3` suffix on device when a folder already exists. For the *outbound* path
(§4) there is no username, so the suffix rule alone must carry it.

Caveat recorded: still a heuristic. A dedicated "Add to library" button (design B)
gives exact names later; this is the R&D-phase compromise.

#### Better idea for outbound naming (noted, not decided)

Since the *inbound* API returns a real `description` (§3a), and the *outbound* path
lacks one, the asymmetry suggests: on archive, if the sketch has no usable
`# DESCRIPTION:`, fall back to the **first line of the editor content the user
typed** — but the device can't see that. So the practical options remain
(a) heuristic from file content, or (b) accept generic `sketch-N` names for
hand-written uploads. Option (b) is arguably fine: the instrument the user is
*actively running* doesn't need a library name; the library is for things they
want to come back to, and those are usually AI-generated (hence named).

### 4e. Open questions / risks (undecided)

1. **Copy vs move.** Copy is non-destructive (Write still means "replace what's
   running"; library accumulates separately). Move would make the library the
   single source. *Leaning copy.*
2. **Junk drawer.** If every write archives, the library fills with experiments.
   Mitigation proposed: skip archiving when the content is byte-identical to an
   existing entry (idempotent re-writes become no-ops); deletion already exists in
   the patch-style browser.
3. **Critical-path cost.** `environment_transfer_done()` runs on the MicroPython
   main thread via `mp_sched_schedule`, right after `zT` — filesystem work here
   (mkdir + ~8 KB write) adds an estimated 50–100 ms to every upload, before the
   restart. Keep it to one read + one write; explicitly **no `compile()`** here
   (validation belongs in the inbound path / browser, not the upload hot path).
4. **Path traversal.** If a name ever becomes a path on the inbound path, `../`
   must be slugged on device (the website is not being changed to guard it).

---

## 5. Alternatives considered

| Design | Website change | Device change | Naming |
|---|---|---|---|
| **A. Archive-on-write** (§4c) | none | `environment_transfer_done()` copies to library | inferred (§4d) |
| B. Two-path via existing param | small — set `opts.sketchPath`; needs a UI trigger | done-hook path scoping | exact |
| C. Explicit `zP` command | small — one extra `zP` | new `cynthia.add_instrument(name)` | exact |

**A is the only true zero-website-change option** and is the one that shipped
(§9). B/C remain the better long-term UX — an explicit "Add to library" button —
once touching the web editor is acceptable. B/C are better UX and are the likely end state once
touching the web editor is acceptable.

---

## 6. Transport note: cable vs Bluetooth

Bluetooth is **not required** for the outbound path. The web editor talks
**WebMIDI**, which on CYNTHIA is the USB-CDC gadget — already working. A cable
gives the full editor today with no BLE stack. BLE-MIDI is listed as a reserved
CYNTHIA extension point (`CYNTHIA_INSTRUCTIONS.md` §16) but is a much larger lift
than this feature needs.

So: **cable → existing web editor → device-side archive → library entry.** Wi-Fi is
then only needed for the *inbound* (World browse) direction.

---

## 7. Sketch safety (independent of inbound/outbound choice)

Downloaded or uploaded code is untrusted either way. Verified state of the
existing mitigations (all confirmed by reading source, not assumed):

**What exists:**

- **Import-time self-heal** (`amyboard.py:622` `run_sketch()`): if `import sketch`
  raises, it reports the traceback via `_report_sketch_error()` **and** overwrites
  `sketch.py` with `DEFAULT_SKETCH_SOURCE`, then retries once. A corrupted file
  therefore does *not* persist across boots.
- **Loop-failure cutoff** (`amyboard.py:690` `_start_sketch_loop`): `_guarded_loop`
  wraps every `loop(tick)` call in `try/except`, counts consecutive failures, and
  at `_MAX_LOOP_FAILS = 8` (amyboard.py:18) calls `stop_sketch()` and prints why.
  Also re-entrancy guarded by `_busy`. So a *raising* `loop()` is already handled.
- **`_report_sketch_error()`** ships the traceback over sysex to the web console.

**Corrections to earlier assumptions:**

- The boot-button skip (`self_test.self_test_if_button()`, GPIO0) is called **only
  under `board() == "AMYBOARD"`** in `_boot.py:180`. **CYNTHIA does not get it** —
  there is no button-triggered escape hatch on CYNTHIA at all.
- `restore_last()` (`browser.py:135`) *copies* the last instrument's `sketch.py`
  into `/user/current/` before `start_amy()` runs it. It does **not** itself
  guard against a sketch that crashes on import — that is `run_sketch()`'s job
  (above), which does recover.

**Real remaining gaps (what's actually missing):**

1. **No `loop()` *time* budget — only a failure count.** `_guarded_loop` catches
   exceptions but does not measure duration. A `loop()` that *hangs* or
   consistently overruns its 32nd-note slot never raises, so `_fails` stays 0 and
   the sequencer callback is starved indefinitely. AMY renders on the other core
   so audio may survive, but input/UI dies with no cutoff. Fix would be a
   `ticks_ms()` delta check in `_guarded_loop` (no such call exists anywhere in
   `amyboard.py` today — verified by grep).
2. **No safe-mode boot / boot-attempt counter.** A sketch that *hard-faults*
   (not a Python exception — e.g. watchdog, `nlr_jump_fail` → `esp_restart()` at
   `main.c:383`) reboots before `run_sketch()`'s self-heal can act, so
   boot→crash→reboot can repeat. There is no counter (`RTC_NOINIT`, nvs, or file)
   anywhere. Combined with the missing CYNTHIA boot-button skip above, the only
   recovery is USB reflash. This is the highest-value gap for a portable synth.
3. **No pre-install validation.** Nothing `compile()`s a fetched/uploaded sketch
   before it becomes the active one. `run_sketch()`'s recovery is reactive (it
   fixes things *after* a failed boot-time import). Compiling in the
   inbound/browser path (§4e.3) makes it preventive. Note the deliberate
   exclusion of `compile()` from the upload hot path (§4e.3) — validation belongs
   in the browser, not in `environment_transfer_done()`.
4. **Static threat-scanning is not worth it.** A watchdog + attempt counter is
   strictly better than grepping for "dangerous" calls.

---

## 8. Suggested order of work (when implementation resumes)

1. **Device-side safety net first** — highest value, protects *both* directions,
   needs no website or network:
   - boot-attempt counter / safe-mode boot (§7.2) — CYNTHIA currently has **no**
     boot-button skip, so a hard-faulting sketch means USB reflash;
   - `loop()` *time* budget (§7.1) — the existing `_MAX_LOOP_FAILS` only counts
     exceptions, not hangs.
2. Outbound archive-on-write (§4c/§4d) — self-contained, no website changes;
   must be idempotent because the restart fires twice (§4c-bis).
3. `mp_file_transfer_done_hook` path scoping (§4b wart) — small correctness fix.
4. Inbound cache-on-fetch (§3c) — needs Wi-Fi credentials decision.
5. Optional: ship the compressed corpus — **human cohort only** (§2b), in `/sys`'s
   unused 1.8 MB (§2a) or in `vfs` (1.03 MB compressed, 1.26 MB spare).

---

## 9. IMPLEMENTED 2026-10-16 — device-side archive (the §4c design)

"Write to AMYboard" in the web editor now also files the sketch into CYNTHIA's
instrument library. The editor is untouched; the device does the filing.

### 9a. What changed

| File | Change |
|---|---|
| `cynthia/naming.py` | **new** — pure, stdlib-only name heuristic (§9c) |
| `cynthia/library.py` | **new** — `archive()`: validate + install into `/user/instruments/<slug>/` |
| `cynthia/compat.py` | wrapped `environment_transfer_done` (additive; original still runs) + `_archive_current()` helper |

`cynthia/world.py` was written and **deleted** (wrong design — on-board HTTP);
the browser-menu World entry was written and **reverted**. `browser.py` is
byte-identical to before. `_boot.py` did not need changing: it already calls
`_compat.install(amyboard, ...)` at boot.

### 9b. The hook

`cynthia/compat.py` wraps `amyboard.environment_transfer_done`:

```
original transfer-done  ->  restart the sketch            (unchanged)
wrapped                 ->  archive /user/current/sketch.py, then the original
```

One attribute patch covers **both** call sites, because the C hook resolves the
name dynamically (`amy_connector.c:392` `mp_load_attr`) and the editor's explicit
`zP` does the same lookup. So the double-fire (§4c-bis) is handled in one place.

**Race worth noting:** the archive reads `/user/current/sketch.py` and the restart
re-imports the same file. The read completes before the original runs, so there is
no interleaving — but this is only safe because the wrap is *sequential*, not
threaded.

### 9c. Naming: measured, and the priority order that came out of it

`naming.suggest_name` order — established by measurement, not taste:

1. **`# DESCRIPTION:` line** (§4d), slugged — fires for 565/622 (91%);
2. **filename stem** — 57/622;
3. first header comment line;
4. `sketch`.

(2) ranks above (3) *because the corpus said so*: naive comment parsing leaked
body-of-template lines and produced names like `straight-into-amy`,
`top-level-code`, `chunk-1`. With the order above, all 622 names come from (1) or
(2); (3) never fires. Slug rules learned the same way:

- **A hyphen is not a word separator.** "Four-on-the-floor kick" must slug to
  `four-on-the-floor`; splitting on '-' produced `four-on-the` (the worst names in
  the corpus). Fixed.
- Truncation is whole-word; a single long hyphenated token keeps as many of its
  parts as fit (`karplus-strong-as-string` -> `karplus-strong-as`).
- Result is always a safe path component: median 15 chars, max 20, and
  **0 unsafe** across the corpus (no `/`, no `.`, no traversal).

### 9d. Archive semantics: copy, non-destructive, idempotent

- **Copy, not move.** `/user/current/sketch.py` keeps running; the library gets a
  second copy. "Write to AMYboard" still means "replace what is playing".
- **Idempotent.** Byte-identical repeat -> no-op. This is *required*, not
  defensive: the callback fires twice per upload.
- **A slug collision gets `-2`, never a merge.** Deliberate default. Measured over
  the corpus, **117 slugs are shared by >1 distinct sketch (350 sketches)** — so
  "slug match means overwrite" would silently destroy instruments. An
  `archive(..., replace=True)` opt-in provides the update-in-place semantics for
  anyone iterating on one sketch.
- **The board's own placeholder is never archived** (`is_default_sketch`): a fresh
  `/user` seeds `DEFAULT_SKETCH_SOURCE`, and the self-heal in `run_sketch()` can
  rewrite it. Matching its marker line keeps that out of the library.

### 9e. Verification actually performed

Firmware: `idf.py -DMICROPY_BOARD=CYNTHIA build` -> clean, **2,869,520 B** of the
3.74 MB partition (866 KB free, 23%); `frozen_content.c` confirms all three modules
are frozen in.

Behaviour: run under the **real MicroPython interpreter** (unix port), not just
CPython — see §9f for why that mattered. Fake device FS, real boot order, real
double-fire:

| Case | Result |
|---|---|
| boot install -> one upload (both triggers) | one folder, restart ran **2×** (preserved) |
| byte-identical re-upload | no-op |
| different sketch, same slug | suffixed `-2`, original body intact |
| board placeholder | ignored |
| non-UTF-8 (latin-1) sketch | archived as `caf-organ`, no crash |
| 622-sketch corpus | 622 distinct folders, 0 unsafe, names from desc/filename only |

### 9f. Two bugs the real interpreter caught (both would have shipped)

Running on CPython alone was **not** sufficient — neither of these shows up there:

1. **`str.isalnum()` does not exist in MicroPython.** Used it to strip
   non-alphanumerics; on device every archive would have failed with
   `AttributeError` — silently, since the wrap catches exceptions and only prints.
   Replaced with `isalpha() or isdigit()`.
2. **`bytes` reaching `headline()`.** `_archive_current` reads the file in `"rb"`
   mode; the naming code assumed `str`. Fixed by normalizing to `str` once at
   `archive()` entry.

Also confirmed absent on this port (so production code avoids them): `os.path`
(use `os.stat`), `str.isalnum`, `open(..., encoding=...)`.

**Still not done:** no hardware was attached, so this has not been flashed or
exercised against the real web editor over WebMIDI. That is the remaining step —
build, flash, "Write to AMYboard" from amyboard.com, confirm the sketch appears in
the instrument grid.

### 9g. Open question for the next iteration

The current behaviour archives *every* write, so the library accumulates whatever
was ever flashed. That is the honest consequence of "no website changes" — the
device cannot know whether you meant "keep this" or "just try it". A dedicated
"Add to library" button (design B in §5) is the real fix; then `archive()` becomes
explicit and this heuristic becomes its fallback namer only.

---

## 10. IMPLEMENTED 2026-10-16 — the "cynthia" encoder facade (§9 follow-up)

A library sketch that drives `amyboard.encoder()` was **inert on CYNTHIA**, because
`_detect_encoder_devices()` scans for I²C seesaw/M5Stack accessories and CYNTHIA has
none — so it returned `[]`, `encoders == 0`, and every encoder call was a no-op.

But `amyboard.encoder()` is **already a facade over host-fed fake devices**: the
`"web"` (in-browser simulator) and `"vcv"` (Rack plugin) sources are emulated, not
real I²C. CYNTHIA just needed a third source. Added (all in
`tulip/shared/amyboard-py/amyboard.py`, CYNTHIA-guarded, additive):

- `_cynthia()` board test; `_CYNTHIA_POT_TICKS` (4096 counts/rev); `_CYNTHIA_BTN_MAP`.
- `_detect_encoder_devices()` returns `[("cynthia", None)]` on CYNTHIA — only when
  `hasattr(tulip, "cynthia_knobs")`, so a board without the bindings still reports
  "no encoders" rather than a dead facade.
- `Encoder.__init__` builds a `"cynthia"` device: `n = len(tulip.cynthia_knobs())`
  encoders (16), `buttons = len(_CYNTHIA_BTN_MAP)` (5).
- `Encoder._raw_read` / `Encoder.button`: `"cynthia"` cases.
- `Encoder(type="cynthia")` accepted in the `type=` allowlist.

### 10a. Endless pots need unwrapping (the non-obvious part)

The new pot board (`cynthia/drivers/spark_pots_new.py`) is **16 endless 360° pots**
returning *absolute angle* 0..1 — which "maps trivially to encoder inputs", as
noted. But trivial mapping is wrong at the seam: an absolute angle jumping
0.98 -> 0.02 is a +4° nudge, not a full revolution backwards. Without unwrapping it
reads as one enormous negative turn per revolution.

`_raw_read` therefore keeps per-pot `angle`/`turns` state and unwraps:

    delta = ang - prev
    if delta > 0.5:  delta -= 1.0     # wrapped backwards past 0/1
    elif delta < -0.5: delta += 1.0   # wrapped forwards past 1/0
    turns += delta

Verified on the host: `0.95 -> 0.98 -> 0.02 -> 0.05` reads `1843 -> 1966 -> 2130
-> 2253`, monotonic. A naive implementation would have produced a large *negative*
reading at the third step.

Because `read()` is `_raw_read(i) - _offset[i]`, the turn counter is already
compatible with `reset()` and `invert()`.

### 10b. Buttons

`tulip.cynthia_buttons()` returns a **bitmask**; encoder button j maps
`(2, 3, 1, 0, 4)` -> patch_prev, patch_next, octave_up, octave_down, joystick.
Verified per-bit on the host, plus the `poll_button_events()` hold/release layer
(HELD after `_BUTTON_HOLD_STEPS`, PRESS on release).

### 10c. Status: host-verified, NOT flashed

Built clean (2,869,520 -> 2,871,792 B; 23% partition free) but **the flash did not
complete**: the device's USB port stopped enumerating mid-session (see §11).

### 10d. Deliberately NOT done yet

- **No display windowing.** `display_refresh` is still unpatched; a sketch calling
  `init_display()` still gets the full `CynthiaDisplay`.
- **No `scan_i2c()` sandbox.**
- **No sketch rewriting.** Considered and rejected as the default: the `amy.send`
  wire codec is lossy by design (`ptr16` packs a pointer table into 16-bit halves;
  `freq="440,0.5"` is a *string* the firmware parses), so faithful transport needs
  C-level interception, and a `$`-based rewrite would mangle `include=` and bare
  identifiers. Typed shims cover the real cases.

---

## 11. REPL usability finding (reproduced repeatedly, hardware)

While probing, the device repeatedly ended up **unreachable over serial** —
`/dev/cu.usbmodem*` opens but the REPL is silent (not even a `>>>` on newline), and
the port then stops enumerating entirely. Recovery required a **physical
replug/reset**; `esptool` could still talk to the ROM loader while the app was
unreachable, so the *chip* was alive and only the MicroPython application was stuck.

Frequency: 6+ times in one session, always while running **long REPL loops**
(>10 s of continuous Python from the REPL, especially ones doing display/I²C work).

Two candidate causes, not yet separated:

1. **Harness artifact.** The probe client enters **raw REPL** (`Ctrl-A`) and if it
   dies mid-probe the firmware stays in raw mode, which is *silent* until it gets an
   execute byte. `Ctrl-C`/newline do nothing there; only `Ctrl-B` exits. Later
   probes must always send `Ctrl-B` in a `finally`. This alone would explain
   "silent REPL" but not "port stops enumerating".
2. **A real hang in the app.** Long REPL execution starving the AMY/audio task or
   tripping a watchdog, or memory exhaustion from a huge recursive `repr`.

This matters beyond debugging: **the same shape is reachable from the web editor**
(any `zP`/`zT` path that leaves the running sketch blocked). It is the concrete
repro for the gap already listed in §7: CYNTHIA has **no boot-attempt counter and
no boot-button skip** (`self_test.self_test_if_button()` is AMYBOARD-only), so
"stuck sketch" == "USB replug". Worth prioritising ahead of the display work.

---

## 12. IMPLEMENTED 2026-10-16 — sketch robustness (§7 gaps closed)

§7 listed three gaps that made a bad sketch unrecoverable. All three are now
addressed; §11's wedge (reproduced 6+ times in one session) is the same failure
mode seen from the debugger's side.

### 12a. The task watchdog only WARNED — found by reading the built config

`CONFIG_ESP_TASK_WDT_PANIC` was **unset** in `build/config/sdkconfig.h`, while
`CONFIG_ESP_TASK_WDT_EN=1` and `CONFIG_ESP_TASK_WDT_TIMEOUT_S=5` were on. So the
task watchdog detected a CPU hog and then merely *printed*
`task_wdt: Task watchdog got triggered` and carried on — the board never came
back. `sdkconfig.base` also sets `ESP_TASK_WDT_CHECK_IDLE_TASK_CPU{0,1}=n`, so
the idle tasks were not even watched.

Fix (`boards/CYNTHIA/sdkconfig.board`, which is **last** in `SDKCONFIG_DEFAULTS`
and therefore wins over `sdkconfig.base`):

    CONFIG_ESP_TASK_WDT_PANIC=y
    CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0=y
    CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1=y

Verified in the rebuilt `sdkconfig.h`: `PANIC 1`, both idle checks `1`, timeout 5s.
A wedged board now reboots itself instead of needing a USB replug. `CONFIG_BOOTLOADER_WDT_ENABLE`
was already on (9s), so even a boot-time hang self-recovers.

This is the single highest-value change in the file: everything else below is a
*nicer* recovery, but without this one there is no automatic recovery at all.

### 12b. Boot guard — `cynthia/guard.py` (new)

`boot -> crash -> reboot` was a loop, because CYNTHIA's boot flow copies the
last-selected instrument's `sketch.py` into `/user/current/` **before** running
it. `run_sketch()`'s import self-heal cannot break that: a hard fault reboots
before it runs, and even when it does fire, the *next* boot re-copies the broken
file from the instrument folder.

Guard design:

- `note_boot()` runs **before** the restore, so a boot that never completes is
  counted (a counter that only incremented on success would never see the bug).
- `clear()` runs at the **end** of the CYNTHIA boot block — AMY up, instrument
  restored and its top-level code run, runtime attached. A sketch whose `loop()`
  later wedges reboots with the counter already back to 1, which is what makes
  this a measure of boots that never *completed*, not of crashes in general.
- After `MAX_BOOT_ATTEMPTS = 3`, **safe mode**: skip the restore entirely, land
  at the REPL, print the offending instrument's name, and `suspend()` it so
  `restore_last()` skips it on later boots (so clearing the counter does not
  immediately re-arm the bad sketch).
- State lives in **`/user/boot_guard.json`**, not `RTC_NOINIT` — deliberately, so
  it survives a full power cycle, which is exactly what a person does when the
  board looks dead.
- `guard.reset()` forgets everything; re-selecting an instrument in the browser
  calls `guard.resume(name)` (an explicit choice overrides "known bad").

CYNTHIA is headless and has **no boot-button escape hatch**
(`self_test.self_test_if_button()` is AMYBOARD-only), so "land at the REPL with
the name printed" is the only recovery that exists without another computer.

**Two design flaws found by integration-testing the real boot sequence, not just
the guard's unit behaviour** (the first version passed its unit tests and was
still wrong):

1. **Safe mode must clear the counter.** The first version left `attempts` high
   after deciding safe mode, so *every* later boot also satisfied
   `in_safe_mode()` — the board would never run an instrument again. That turns a
   recoverable crash loop into a REPL-only brick, i.e. worse than the bug. Fixed
   with `enter_safe_mode(culprit)`, which suspends the culprit *and* zeroes the
   counter: safe mode is a **transition, not a state**.
2. **Only the offending instrument may be suspended.** The first version suspended
   whatever `last_instrument()` returned, which — once the good instrument was
   selected — suspended an innocent one. Now the culprit is tracked from the boot
   that actually failed, and `note_boot()` **restarts the count when the selected
   instrument changes**, so N bad instruments each get their own budget and
   alternating between them can never reach the threshold.

3. **Skipping the restore is not enough — the stale file still runs.** This is
   the one that would have made the whole guard useless, and it was found only by
   modelling `amyboard.start_amy()` in the simulation (the earlier test stopped at
   the guard boundary and so could not see it). Safe mode skipped
   `restore_last()`, but `/user/current/sketch.py` still held whatever the
   *previous* crash-looping boot had written there, and `start_amy()` runs that
   file regardless. So the crash simply repeated and the counter cycled —
   safe mode never converged. Safe mode now also overwrites
   `/user/current/sketch.py` with `DEFAULT_SKETCH_SOURCE`, so the board comes up
   quiet with a REPL. (A symptom of the same oversight: `attach_current()` was
   still being handed the culprit's name, so the UI would have labelled itself
   with a broken instrument — it is now given `None` in safe mode.)

### 12c-bis. Flaw 4: the guard could not see a hang that starts AFTER boot

Found on **hardware**, not in simulation — the hang test left the board in an
infinite reboot loop with the previous build. The guard counted boots that never
*complete*, and a sketch whose top-level code is fine and whose `loop()` hangs
completes boot perfectly:

```
boot: note_boot -> restore -> start_amy -> run_sketch imports sketch
      -> loop() scheduled -> compat install -> attach_current -> guard.clear()
      ... some later tick enters `while True:` ...
      idle task starves -> TWDT resets (5s) -> boot again -> guard.clear() again
```

`attempts` therefore stayed 0 forever while the board rebooted every 5 seconds.
The watchdog fix (§12a) is what made the *reboot* work; but rebooting forever is
its own failure, and the guard could not see it.

Fix: a **second counter** for post-boot deaths, keyed off the reset cause:

- `note_wdt_boot()` increments it when `machine.reset_cause() == WDT_RESET`, i.e.
  the previous boot died by watchdog (meaning it had *completed*).
- `clear_wdt()` zeroes it, armed by `runtime._poll` via `_stable_after` — only
  after a boot has survived `_STABLE_POLLS = 150` polls (~5s). A hang-every-time
  sketch never gets there; an occasional one-off reset always does.
- `MAX_WDT_ATTEMPTS = 6`, deliberately looser than the 3 for never-completed
  boots: a completed boot is much more likely to be a good instrument with one
  bad moment.

Both triggers now funnel into a **single** safe-mode path, so the stale-file
neutralisation (flaw 3) cannot be forgotten for one of them.

Simulated across a mixed sequence: a hang-every-boot sketch trips safe mode on
boot 7 (6 consecutive WDT resets); alternating hang/healthy boots never
accumulate (`wdt` returns to 0 on every stable boot).

Two invariants fell out of the state-machine testing, and both are easy to
break later:

- **`clear()` must NOT touch the wdt counter.** They look like "the boot
  succeeded, reset everything", but clearing `wdt` there would mean a sketch that
  hangs after every completed boot never accumulates — the exact hole this fix
  closes. Verified: one completed boot leaves `wdt` unchanged.
- **Both safe-mode entries clear BOTH counters.** Otherwise the sibling counter
  stays at its threshold and the very next boot re-enters safe mode, producing
  the flaw-1 brick again through a different door.

**The lesson worth keeping:** all three flaws passed the guard's own unit tests.
They only surfaced when the *surrounding* boot sequence was simulated. A recovery
path is only as good as its model of the thing it is recovering from.

Verified by simulating the boot sequence on the host (final version, with
`start_amy()` modelled — boot 3 now reaches safe mode, resets the file and
completes, instead of cycling):

| Scenario | Result |
|---|---|
| crash loop | boot 1,2 run; boot 3 safe mode; boot 4 skips culprit, lands at REPL |
| switch to a good instrument | boots normally, counter clears, suspensions kept |
| user fixes + re-selects | `resume()` clears suspension, boots fine |
| a *second* bad instrument | gets its own budget; no cross-contamination |
| alternating bad instruments | count restarts per instrument; threshold never spurious |

`clear()` resets the counter *without* clearing suspensions; `resume()` clears them. Compile validation returns `None` for good
source, a `SyntaxError` string for bad source, and `"not valid UTF-8"` for binary.

### 12c. `loop()` time budget (`amyboard.py`, `_start_sketch_loop`)

`_MAX_LOOP_FAILS = 8` only counted loops that **raise**. A loop that consistently
overruns its slot — or hangs until the watchdog fires — never raises, so `_fails`
stayed 0 and the sketch was never stopped. Added a measured budget:

    _LOOP_OVERRUN_MS = 150     # ~2.5 slots at 120 BPM (slot is ~62ms)
    _MAX_LOOP_OVERRUNS = 16    # consecutive, so one slow tick is tolerated

Counted over consecutive calls (`ticks_diff` from the previous entry, reset on any
good tick), so a sketch that merely does one slow render survives while one that
monopolises the CPU is stopped with a printed reason.

Verified on the **real MicroPython** unix port (not CPython — `ticks_ms`/
`ticks_diff` semantics matter): fast loop never trips; one 400ms tick is
tolerated and resets; 16 consecutive 300ms ticks trip; `ticks_diff` wrap correct.

### 12d. Pre-flight compile validation

`guard.validate()` compiles before a sketch becomes the *active* one, wired into
`browser.select()` — so a truncated download or a broken edit is refused while the
good instrument keeps playing, instead of being installed and discovered at the
next boot where the only symptom is a crash loop.

Deliberately **not** called from the boot path: `run_sketch()` already self-heals
an unimportable file, and compiling 100 KB+ at every boot costs real milliseconds
for no extra safety.

### 12e. Status

All four built clean (`micropython.bin` 2,879,328 B; 23% partition free) and
**host-verified**; the flash was blocked by the §11 wedge itself (the board was
sitting in the old, watchdog-less firmware from an earlier probe). One replug
needed to put the new image on — after which the failure that caused it should
self-recover.

---

## 13. IMPLEMENTED 2026-10-16 — CYNTHIA's own software watchdog

**Why the IDF task watchdog was abandoned (measured, not assumed).** §12a
configured it correctly and every static check passed:

- Kconfig option names verified against `esp_system/Kconfig`
- `build/sdkconfig`: `CONFIG_ESP_TASK_WDT_PANIC=y`,
  `CHECK_IDLE_TASK_CPU0/1=y`, `TIMEOUT_S=5`
- `build/config/sdkconfig.h`: `PANIC 1`, both idle checks `1`
- `freertos/app_startup.c:193-196` assembles `idle_core_mask` from exactly those
  symbols, and `task_wdt.c:536-538` calls `subscribe_idle()` for each bit

Yet a Python `while True: pass` produced **30 seconds of total console silence
against a 5-second timeout** — no `task_wdt` text, no panic, no reset, no USB
re-enumeration. The board simply stayed hung until a physical replug;
`ioreg` showed the device still enumerated as "AMYboard", so it was a hang and
not a reset loop. Worse, a *person* cannot distinguish that from a dead board, and
"press RST" is not an acceptable recovery on a product.

Whatever the root cause (not chased further — a watchdog whose firing we cannot
predict is not one to ship on), the conclusion is that **the watchdog has to be
ours, where its behaviour is explicit and testable**.

### 13a. Design

`cynthia_wdt.c` / `.h` (new), owned by the **CYNTHIA input task** — a separate
FreeRTOS task on core 0, priority 1, looping every 5 ms, which never calls into
MicroPython. That independence is the entire point: the check must survive a hung
VM on core 1.

- MicroPython **feeds** it from the UI poll (`runtime.py::_poll`), because that
  callback is the liveness signal a stuck `loop()` cannot provide.
- The owner **checks** it every task iteration (placed first in the loop, so a
  slow I²C scan cannot delay the recovery decision).
- On expiry it records the reason in **`RTC_NOINIT_ATTR`** memory, then
  `esp_restart()`.

Why the RTC variable is necessary: **`esp_restart()` reports `ESP_RST_SW`, which
MicroPython maps to `MP_HARD_RESET` — not `WDT_RESET`.** So `reset_cause()` alone
cannot tell a watchdog reboot from an ordinary one, and the boot guard (§12b)
would never see the hang. `cynthia_wdt_take_reason()` is read-and-clear at boot,
and a non-zero reason is fed to the guard exactly like an IDF watchdog reset.

### 13b. The two hard parts (both are about NOT rebooting)

1. **Disarmed by default, armed only when the poll is running.** Arming at boot
   would fire during sketch import/compile or a slow file transfer — all
   legitimately slow, none of them hangs.
2. **Everything that deliberately stops the poll must disarm explicitly.** "No
   poll" and "hung sketch" are indistinguishable to the C side, so `stop()`
   (leaving PLAY) and `calibrate_ui()` (which blocks for seconds on cooperative
   stdin reads) both call `_disarm_watchdog()`, and calibration re-arms on every
   exit path. Without this the board would reboot every time a user left PLAY.

Timeout is 10 s by default — generous enough for a slow render or a heavy I²C
burst, still recovering a genuinely stuck board quickly.

### 13c. Python surface

    tulip.cynthia_wdt_arm(timeout_ms=0)   # 0 = default
    tulip.cynthia_wdt_disarm()
    tulip.cynthia_wdt_feed()
    tulip.cynthia_wdt_reason()            # read-and-clear; non-zero = recovered
    tulip.cynthia_wdt_status()            # (armed, ms_since_feed, feed_count)

Stubs exist for non-CYNTHIA builds, so `shared/modtulip.c` stays board-agnostic
and `runtime.py` can call these unconditionally via `hasattr`.

### 13d. VERIFIED ON HARDWARE (2026-10-16) — the board recovers itself

**The product requirement is met: a sketch whose `loop()` never returns no longer
requires a replug or the RST button.**

    installed a real sketch:  def loop(tick): while True: pass
    ... 30s, hands off ...
    => RC 3 (WDT_RESET), board back up, engine DX7Instrument, running True
       /user/current/sketch.py self-healed to the default (107 bytes)

`machine.reset_cause()` returning **3 (`WDT_RESET`)** is the proof: before this
work the identical test ended in a permanent hang.

### 13e. Two false starts worth recording (both cost real time)

1. **`while True: pass` at the REPL is NOT a hang.** My first attempt hung the
   REPL and then concluded the watchdog was broken. It isn't: the VM calls
   `mp_handle_pending` at branch points (`MP_BC_JUMP`,
   `MP_BC_POP_JUMP_IF_TRUE` → `DISPATCH_WITH_PEND_EXC_CHECK`), which runs
   scheduled callbacks — so `_poll` keeps running and the feed continues. The
   board is unresponsive *to the REPL* but is not hung. Measured: 12s of that
   loop produces no reset (correct), and **Ctrl-C breaks it instantly** (so a
   REPL-level mistake is always recoverable, no watchdog needed).
2. **The scheduled-callback case is the real one.** A sketch's `loop()` is itself
   a scheduled callback, so hanging inside it leaves `sched_state ==
   MP_SCHED_LOCKED`; every later `mp_sched_run_pending()` returns immediately
   without running `_poll`. That is what stops the feed, and that is what the
   watchdog catches.

This also finally explains why the IDF task watchdog was blind (§13): it measures
**CPU idle**, and `MICROPY_PY_THREAD_GIL_VM_DIVISOR = 32` makes the VM bounce the
GIL every 32 branch points, so the idle task keeps running and keeps resetting the
idle watchpoint even while Python spins. The failure to catch is **semantic
liveness**, not idleness — a different signal, which is why feeding from the UI
poll is the right design rather than a workaround.

### 13f. Residual caveat

Schedule/callback starvation is caught. A hang that somehow *keeps* the feed alive
(a sketch calling `cynthia_wdt_feed()` itself, or a wedged C task) is not, by
construction. Not a shipping concern, but worth knowing before trusting this as a
universal net.

---

## 14. General hardening audit (2026-10-16)

Beyond the sketch-hang case (the one users will actually hit), the other ways a
shipped CYNTHIA could become unrecoverable. Checked by reading the built config
and the code, not by assumption.

| Path | Finding | Action |
|---|---|---|
| **Sketch `loop()` hang** | Was unrecoverable (permanent hang). Now self-recovers. | Fixed (§13, verified on hardware) |
| **Brownout / weak supply** | `CONFIG_ESP_BROWNOUT_DET=1`, level 7 (~2.44V), `BROWNOUT_INTR=1` — on by IDF default and already sufficient. Resets cleanly, and `/user` is littlefs so a mid-write reset is journaled. | None needed |
| **Boot-time hang** | `CONFIG_BOOTLOADER_WDT_ENABLE=1`, 9 s — already on, so a boot that wedges before MicroPython starts still self-recovers. | None needed |
| **Boot→crash→reboot loop** | Unrecoverable before; now safe mode after 3 attempts (§12b), with the stale-file neutralisation (§12b flaw 3). | Fixed |
| **Hang *after* boot completes** | Invisible to the "did boot complete" counter; now caught by the post-boot WDT counter (§12c-bis) fed by the software watchdog's recorded reason. | Fixed |
| **I²C device lockup (input path)** | Every C-side transfer has a bounded timeout (`pdMS_TO_TICKS(10)`, 5 sites); the driver functions return `esp_err_t` and callers fall through. Cannot wedge the input task. | None needed |
| **I²C device lockup (Python display path)** | `display.py` uses plain `machine.I2C.writeto` with no explicit timeout — a stuck bus *inside a sketch's draw* could block the poll. But a poll that blocks stops feeding, so the software watchdog now covers this case as well. | Covered by §13 |
| **AMY render/fill task death** | **Genuine residual gap.** Those tasks run at `ESP_TASK_PRIO_MAX-1` (24) — *above* the watchdog's task (1) — so a hang inside AMY would starve the checker and no watchdog in that task could report it. Mitigation added: the checker also reads `amy_global.total_blocks` (AMY's audio-clock counter) and distinguishes "python hang" (audio alive) from "audio path stalled" in its message. That names the fault rather than fixing it. | Partially mitigated; see below |
| **REPL-level infinite loop** | Not a hang at all: the VM's branch-point `mp_handle_pending` keeps running scheduled callbacks, so feeds continue (correctly no reset) — and **Ctrl-C breaks it** (measured). | None needed |
| **Flashing a hung board** | `esptool --before=usb_reset` and a fresh replug both work; `machine.bootloader()` from a live REPL also drops into ROM mode. Only a *fully* wedged board with the old firmware truly needed a replug. | Documented |

### 14a. The residual gap, stated plainly

If AMY's own render/fill task wedges, the current watchdog can *report* it (audio
counter frozen) but the check itself runs at priority 1 and may never get to run.
Closing that properly means either subscribing AMY's tasks to the IDF TWDT
(which we now know does not fire reliably here — §13) or moving the audio-liveness
check into a higher-priority task. Not done: it needs its own investigation, and
it is a much rarer failure than a user sketch hanging. Recorded so it is a known
boundary rather than a surprise.

---

## 15. Beta-hardening pass (2026-10-19)

Product decision: freeze experiments, make the firmware stable for beta testers.
Full review of every CYNTHIA source file (runtime, browser, surface, instruments,
drivers, ads, midi, compat, guard, library, naming, and the C input/UI/WDT tasks),
looking for brick risks, crashes, and avoidable cost.

### 15a. Brick risks fixed

| Risk | Why it mattered | Fix |
|---|---|---|
| **Watchdog armed too late at boot** | It was armed in `_start_poll()` (inside `attach_current`), which runs AFTER `start_amy()` → `run_sketch()` → `import sketch`. A sketch whose **top-level** code hangs blocks there, before any poll exists, so nothing watched it — permanent hang, exactly the brick this work exists to prevent. | Arm in `_boot.py` *before* `start_amy()`, and disarm at the end of boot if no poll started (so a quiet headless REPL is not rebooted for idling). |
| **Boot timeout too short** | The boot arm cannot use the 10s runtime timeout: a *valid* boot legitimately blocks for seconds (I2C prescan, display init, sketches that `time.sleep(1.5)` at import). That would reboot healthy boards. | `CYNTHIA_WDT_BOOT_MS = 60000`. 60s still catches a top-level hang. |
| **`_poll` could raise** | `_poll` is a scheduled callback; an exception there is swallowed by the VM, which **silently kills every later poll** — keys keep playing (C-side) while buttons/knobs/joystick/display go dead until reboot. This is the single most likely beta bug report. | Split the body into `_poll_body()` and guard it; print the first 3 tracebacks (throttled — at ~30Hz an unthrottled print would itself starve the REPL), then stay quiet. Feed the watchdog *outside* the try so a body fault does not also stop the feed. |
| **`ensure_user_environment()` unguarded** | If `/user` was unwritable, boot died right there — nothing after it ran, including the guard and watchdog. | Wrapped; boot always reaches the REPL. |
| **Half-written instruments** | `install()` wrote `sketch.py` then `meta.json`; a disk-full failure left a folder that `list_instruments()` accepted but which cannot load. | Write `meta.json` **first**, `sketch.py` last (the listing gates on `sketch.py`), so the worst case is an invisible metadata-only folder. Also `list_instruments()` now rejects a **zero-byte** `sketch.py`. |
| **`bytes.decode()` with `errors`** | MicroPython's `decode()` takes **no** `errors` argument — `decode("utf-8","replace")` raises `UnicodeError`. Any sketch with a stray high byte crashed the archive path. Found by running the suite under the real MicroPython unix port, not CPython. | `library.to_text()`: strict decode, then latin-1, then a byte-wise fallback. Never raises. |

### 15b. Cross-core correctness (C)

The CYNTHIA input task runs on core 0 and MicroPython on core 1. Several pieces of
state are written by one and read by the other but were plain non-`volatile`
statics, so the compiler was free to cache or hoist the read across the cross-core
access: `s_evt_head`, `s_evt_tail`, `s_knob_values[]`, `s_joy_x`, `s_joy_y`,
`s_joy_btn`, `s_btn_state`. All are now `volatile`. No locking is needed — aligned
32-bit loads/stores are atomic on ESP32 and the queue is single-producer/
single-consumer behind an already-bounded ring.

### 15c. Performance fix worth knowing

The **new** pot driver (`spark_pots_new`) ran a full 16-address scan on **every**
surface poll: 16 × (mux settle + 2 ADC reads) ≈ **3.8ms of blocking
`time.sleep_us`**, against a ~10ms poll budget at ~96Hz — **37% of the frame spent
busy-waiting**, and it starved the same poll that feeds the watchdog. It now paces
itself (`SCAN_PERIOD_MS = 20`, i.e. 50Hz — far faster than a hand can turn a pot),
which is exactly what the **legacy** driver already did (one pot per poll, with a
comment explaining why). Both drivers now share the same discipline.

### 15d. Checked and found already correct

- `list_instruments`, `seed_defaults`, `_meta_name`, `_copy_sketch` — bounded, idempotent.
- Event ring buffer: bounded push with a full check, matching pop. No overflow.
- `cynthia_get_knobs` clamps `count` to `CYNTHIA_NUM_POTS`; caller array matches.
- `midi.inject` caches identical messages (the joystick path sends at ~96Hz) and allocates nothing on repeat.
- `ADSManager.poll` is already paced and wrapped in try/except, reading the *previous* conversion so it never waits on I²C.
- `CynthiaDisplay` implements **every** method of `amyboard.Display` — no compat gaps.
- Blit uses the C binding (`cynthia_blit_gs4`) with a Python fallback only off-device.
- Full AST sweep (including nested functions, read-before-assign) over all
  `cynthia/**/*.py`: **no** global-scoping hazards. One apparent hit in
  `surface.start()` is a false positive (assigned before read).
- No unused imports except one in `guard.py`, removed. No TODO/FIXME/debug leftovers.

### 15e. Status

Built clean; all new modules pass under the real MicroPython unix port (naming,
archive idempotency, latin-1, guard both counters, compile validation). Flashing the
hardening build needs a replug (board was left disconnected after the last test).

### 15f. Additional bugs found in the full-file sweep (not just my changes)

| Bug | Where | Effect | Fix |
|---|---|---|---|
| **False knob events on slot growth** | `surface.knob_changes()` | New slots were padded with `0.0`, so a pot already sitting at 0.8 read as a +0.8 change and jerked whatever patch parameter it drove. Triggered whenever the pot count grew (driver swap, or the `cynthia_knobs` fallback returning more channels). | Pad new slots from the CURRENT value (`curr[len(_last):]`), so an added pot is silent until it actually moves. Verified: first call and a 3->5 growth both produce no events; a real turn still does. |
| **`sync_knobs()` IndexError** | `surface.sync_knobs()` | Indexed `_last[i]` without growing it; raised `IndexError` whenever `_last` was shorter than `values` — which is true before the first poll fills it. Latent today (no live callers) but it is public API. | Grow `_values`/`_last` first, then assign. Verified with a short `_last`. |
| **SPI host collision between slots** | `connector.Slot.spi()` | Hardcoded `machine.SPI(1)`. On ESP32-S3 flash owns SPI0/1 and only SPI2/SPI3 exist, exposed as ids 0 and 1 — so both slots asking for id 1 collided and the second driver died with "SPI host already in use". Latent (one user today). | Id derived from the slot: slot 0 -> 0 (SPI2), slot 1 -> 1 (SPI3). |

### 15g. Verified-correct during the sweep (no change needed)

- `draw_h_line_vlsb` clamps both axes before indexing → **no buffer overflow** from
  a sketch drawing off-screen, which was the main C-side worry.
- Event ring buffer: bounded push, matching pop, full check. Correct.
- `cynthia_get_knobs` clamps `count`; caller array sized to match.
- `registry.start()/stop()`: idempotent, every driver call wrapped.
- `CynthiaDisplay` covers every `amyboard.Display` method (surfaces match exactly).
- Blit uses the C binding with a Python fallback only off-device.

### 15h. Build gotcha hit during this pass (worth knowing before shipping)

The amyboard build directory is **shared across board targets**. Switching
`-DMICROPY_BOARD=` without removing it reuses stale generated files, and the
failure is misleading: `frozen_content.c` keeps both boards' qstrs and the link
dies with

    error: redeclaration of enumerator 'MP_QSTR_amyboard_set_midi_out'

That looks like a code bug in whatever you just touched. It is not — `rm -rf
tulip/amyboard/build` and rebuild. Verified: AMYBOARD and CYNTHIA both build
clean from a fresh directory.

### 15i. Remaining known limits before beta

- **AMY task death** is still only *reported*, not recovered (§14a): AMY's tasks
  run above the watchdog's task, so a hang inside AMY starves the checker. Rarer
  than a bad sketch, and needs its own investigation.
- **Flashing a wedged board**: `--before=usb_reset`, a replug, or
  `machine.bootloader()` from a live REPL. There is no DTR/RTS→reset handshake on
  the USB CDC port, so a user with a truly stuck unit has no in-band recovery.
  Worth solving before retail, not before beta.
- Hardware smoke of this hardening build is **pending** (board was disconnected).
  Everything code-level is verified: both boards build clean, all CYNTHIA Python
  modules pass under the real MicroPython interpreter, and the earlier
  hardware-verified results (sketch archiving, DX7 save button, encoder facade,
  watchdog self-recovery) stand.
