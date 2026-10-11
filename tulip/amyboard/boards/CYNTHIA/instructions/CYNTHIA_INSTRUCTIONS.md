# CYNTHIA Firmware — Implementation Instructions

**Purpose:** canonical instruction set for the CYNTHIA port of tulipcc/AMYboard firmware.
Hand this file plus `SESSION_CONTEXT.md`, `PORT_NOTES.md`, `PATCHES.md`, and `Board.h`
to a fresh agent session. Revision 5 (October 2026).

**Status:** Core port, dedicated C input scanning task, and hardware drivers complete.
Keys, knobs, buttons, joystick, MIDI injection, OLED display, and patch engine routing operational.

---

## 0. Ground rules (read first)

1. **Never define `AMYBOARD` for CYNTHIA.** `amy/src/i2s.c` ~line 67 uses
   `#if !defined(AMYBOARD)` to pick I2S *master*. Defining AMYBOARD forces slave +
   external clock (PCM9211) and hangs `i2s_channel_init_std_mode()`. CYNTHIA's PCM5102A
   needs master. Board identity is `CYNTHIA`.
2. **All C edits are additive guards only** (`|| defined(CYNTHIA)` / `#ifndef CYNTHIA`),
   logged in `PATCHES.md`. The amy pin may change on sync; PATCHES.md is the re-apply source.
3. **Prefer Python over C for drivers/logic.** Touch C only for hardware init, a
   proven-real-time poll path (§8.4), or a MIDI-injection hook (§6).
4. **NEVER hardcode GPIO numbers outside the pin-map layer.** Pins change between board
   revisions. Drivers/app use logical names only (§3.2).
5. **No blocking or lag anywhere in the control path.** Hard product requirement. Follow §8.
6. **Sketches must not need keyboard/button-specific code.** Keys & joystick feed AMY as
   MIDI (§6); buttons/params/patches are firmware-managed with opt-in sketch hooks.
7. **New Python goes in CYNTHIA-specific modules** (`cynthia/` package, frozen or `/user`),
   never by editing shared upstream files beyond minimal CYNTHIA-guarded branches.
8. **Verify codebase freshness before implementing.** SESSION_CONTEXT is a snapshot; if
   tulipcc/amy have moved, re-check §1 assumptions and re-apply PATCHES.md.
9. **Upstream merges stay mechanical.** If a change can't be a guard or isolated file, stop.
10. Build: `rm -rf build` when switching `-DMICROPY_BOARD=`; use
    `tulip/shared/grab_submodules.sh`; ignore `micropython` submodule dirt.

---

## 1. Current working state (do not re-derive; verify freshness per §0.8)

Verified working as of the 2026-08-07 build (SESSION_CONTEXT §6):
- Boot, USB CDC REPL, PSRAM heap, AMY audio over PCM5102A (I2S master).
- Web editor full handshake: zI, zP, zA, zT, zB. USB-MIDI in/out via TinyUSB gadget.
- Board target `tulip/amyboard/boards/CYNTHIA/`. Known-good snapshot `git stash@{0}`.

Hardware inventory for THIS phase:

| Resource               | Notes                                                                                       |
| ---------------------- | ------------------------------------------------------------------------------------------- |
| Mainboard ADS1115      | 0x48 on I2C_1 (SDA=5,SCL=4); AIN0=Joy Y, AIN1=Joy X, AIN2=Pot MUX (driven by C task)        |
| I2S mic (ICS-43434)    | **optional, may be unpopulated**; expose to AMY like AMYboard audio-in (§15)                |
| Display                | **on Spark-main daughterboard** (SH1106 128×64, I2C Bus 1, SDA=5, SCL=4)                     |
| Daughterboard controls | 28 chromatic keys + 4 buttons (2x MCP23017 on I2C_0), 16 pots (74HC4067 MUX) (driven by C task) |
| Battery                | BATT_LVL=1, BAT_ST_1=2, BAT_ST_2=44                                                         |

Corrections to SESSION_CONTEXT: audio-in is NOT "none" — optional I2S mic exists. Display is
a daughterboard resource, not mainboard. See §14, §15.

---

## 2. Target architecture

Two input planes + output. This is the conceptual backbone:

```
NOTE/CONTROL PLANE — feeds AMY directly; sketches don't handle these
  keyboard ──> MIDI note on/off ─┐
  joystick (PLAY) ──> pitch bend + CC1 ─┤──> AMY MIDI input (§6)
  external USB MIDI ─────────────┤
  patch buttons (fallback) ──> MIDI Program Change ─┘
  pots ──> AMY params (external_coef / CC) ──> AMY (§9)

UI PLANE — consumed by firmware modes (§7) and optionally sketches
  joystick (BROWSER/MENU) ──> navigation
  joystick button ──> select (short) / back (LONG)
  4 control buttons ──> octave−/+, patch prev/next (long-press = menu)
  touch strips ──> selection or per-sketch

OUTPUT
  display (SH1106, on spark_main) · NeoPixel LEDs · AMY audio
```

```
┌──────────────────────────────────────────────────────────────┐
│ MODE MANAGER (Python) — BROWSER <-> PLAY <-> MENU, routes     │
├──────────────────────────────────────────────────────────────┤
│ APP: instrument_browser.py (list/select/run sketches)         │
│ STATE: per-instrument state.json (knobs/params/patch) (§17)   │
├──────────────────────────────────────────────────────────────┤
│ ROUTING: cynthia/midi.py (MIDI event bus/injection)           │
│          cynthia/surface.py (knobs/touch/buttons/UI events)   │
│          cynthia/params.py (§11) · cynthia/patch.py (§12)     │
│          cynthia/registry.py + cynthia/config.py              │
├──────────────────────────────────────────────────────────────┤
│ DRIVERS (Python): spark_main | spark_pots_new |               │
│                   spark_pots_legacy | ondine                  │
├──────────────────────────────────────────────────────────────┤
│ HW ABSTRACTION: cynthia/pins.py · connector.py · analog.py ·  │
│                 ads.py                                        │
├──────────────────────────────────────────────────────────────┤
│ PLATFORM: CYNTHIA-guarded C port + machine.I2C/SPI/Pin +      │
│           neopixel (+ C escape-hatch poll tasks §8.4)         │
└──────────────────────────────────────────────────────────────┘
```

Principle: nothing above the driver layer knows which daughterboards or physical pins are
in play. Drivers talk to `Slot` objects via logical names; only `pins.py` knows GPIO numbers.

---

## 3. Connector model & pin abstraction

### 3.1 What an FFC slot provides
CYNTHIA has **2 FFC connectors** (`slot0`, `slot1`). Each provides:

| Resource      | Count | Notes                                                  |
| ------------- | ----- | ------------------------------------------------------ |
| GPIO `A..G`   | 7     | slot-specific physical pins                            |
| ADC `A`, `B`  | 2     | analog channels → mainboard ADS inputs (§5.2)          |
| I2C_0 + I2C_1 | both  | shared buses; daughterboard I2C devices hang off these |

The FFC carries no dedicated SPI bus, but the ESP32-S3 GPIO matrix lets `machine.SPI` use
any GPIO as SCK/MOSI/MISO/CS, so daughterboard SPI devices (MCP3002, future SPI display)
run at hardware speed over slot GPIO. **Confirmed: this is the intended approach.**

### 3.2 Pin map is revision-dependent → abstract it
`Board.h` is the C-side pin map for the *current* revision and **will change**.
- `cynthia/pins.py` = single Python source of truth mirroring `Board.h` for this revision.
  On a respin, **only `pins.py` (and `Board.h`) change.**
- Keep a comment in `pins.py` recording which `Board.h` revision it matches.
- Drivers/app never import `pins` directly — only via `connector`.

### 3.3 `cynthia/connector.py` — the slot object
```python
class Slot:
    def __init__(self, slot_id):            # 0 or 1
        self.id   = slot_id
        self.gpio = {n: machine.Pin(...) for n in "ABCDEFG"}   # from pins.py
        self.adc  = {"A": <ADS ch idx>, "B": <ADS ch idx>}     # mainboard ADS 0..3
        self.i2c  = {0: machine.I2C(0), 1: machine.I2C(1)}
    def spi(self, sck, mosi, miso, cs):     # hardware SPI over chosen GPIO
        return machine.SPI(..., sck=self.gpio[sck], ...)
```
Drivers use `slot.gpio["A"]`, `slot.adc["B"]`, `slot.i2c[0]`, `slot.spi(...)`.

---

## 4. Daughterboard selection (config authoritative, autodetect best-effort)

Autodetect can't be relied on alone: pot boards have **no I2C devices** (probe finds
nothing), and I2C buses are **shared across both slots** (probe identifies a device *type*
but not *which slot* it's in).
1. **`/user/daughterboard.json` is authoritative for slot assignment.**
   ```json
   {
     "adc_chip": "ads1115",
     "slots": {
       "slot0": { "driver": "spark_main" },
       "slot1": { "driver": "spark_pots_new" }
     }
   }
   ```
2. **Autodetect is best-effort:** probe I2C; for boards WITH I2C devices (spark_main's
   MCP23017/SH1106, ondine's AT42QT2120) identify the type. Use it to pre-fill an empty
   config or warn on mismatch. Never silently override explicit config.
3. **If config absent and autodetect ambiguous** → prompt (display if available, else REPL).

A full instrument may span **both** slots (Spark = spark_main + a pot board). The surface,
MIDI routing, params and patches aggregate across slots.

---

## 5. Analog input subsystem

Two kinds of analog source; both feed the same uniform API upward.

### 5.1 Source types
- **Mainboard ADS** (`cynthia/ads.py`): single shared ADS1015/1115 (I2C_0, 0x48), 4 ch.
  A mainboard resource; drivers *claim channels*.
- **Daughterboard-local ADC**: a chip physically on a daughterboard (e.g. new pot board's
  **MCP3002 over SPI**), used when more polling speed is needed than the I2C ADS gives.
  Owned/driven by that board's driver (it knows its own mux/counter scheme).

### 5.2 Mainboard ADS channel map
```
ADS AIN0 = slot0.adc.A      ADS AIN2 = slot1.adc.A
ADS AIN1 = slot0.adc.B      ADS AIN3 = slot1.adc.B
```

### 5.3 `cynthia/ads.py` — mainboard ADS manager (selective polling)
**Only channels at least one driver claimed are polled** — fewer active channels = higher
per-channel rate. If nothing subscribed, the poll idles (no I2C).
```python
class ADSManager:
    def __init__(self, i2c, addr=0x48, chip="ads1115"): ...   # chip configurable
    def subscribe(self, channel, owner) -> Handle              # Handle.value kept fresh
    def unsubscribe(self, handle)
```
Chip `ads1015` (12-bit, ≤3300 SPS) vs `ads1115` (16-bit, ≤860 SPS): same I2C register map;
parameterize data-rate + result interpretation (ADS1015 result is 12-bit left-justified in
the 16-bit register → >>4; ADS1115 is full 16-bit signed). **Default `ads1115`; some boards
are `ads1015`; set via `daughterboard.json`.**

### 5.4 Daughterboard-local ADCs (e.g. MCP3002)
The owning driver drives it via `slot.spi(...)` + its own GPIO (mux counter clk/reset). Use
**hardware SPI** (GPIO matrix), not bit-banging. The driver implements its own scan but must
follow §8 pacing + zero-alloc rules. The analog layer provides shared pacing utilities so
scans don't busy-loop.

---

## 6. MIDI routing — keyboard & joystick are MIDI sources

**Recommendation (the "pseudo-MIDI device?" idea): YES, do this.** Expose the keyboard and
joystick-in-play-mode to AMY as MIDI sources. Reason: amyworld sketches already respond to
MIDI note/CC/pitch-bend. Translating the keyboard into MIDI note-on/off (and the joystick
into pitch-bend + CC1) and feeding them into AMY's MIDI input path makes the internal
controls behave exactly like an external MIDI keyboard/controller. **Every sketch then works
with the keyboard automatically — no sketch needs keyboard-specific code.**

**Injection point:** Locate where external MIDI bytes enter AMY (`amy/src/amy_midi.c`, the
parser entry used by `run_midi_task()`/`check_tusb_midi()`). Feed synthetic 3-byte MIDI
messages (status, data1, data2) into that same path so internal and external MIDI are
indistinguishable. If no Python-accessible injector exists, add a small CYNTHIA-guarded C
helper. **Agent: explore the amy source to confirm the exact entry point and whether
`amy.send()`/`amy.message()` can express note-on/off directly (fallback).**

**MIDI channel:** the keyboard must send on a channel the active sketch listens to.
AMYboard convention: ch 1 (index 0) = main/lead voice, ch 10 = drums. **Default the keyboard
to channel 0 (MIDI ch 1), make it configurable.** Agent: verify how amyworld sketches receive
MIDI (single configured channel vs all-channels-to-active-synth) and adjust the default.

**Velocity:** mechanical switches have no velocity sensing. Fixed velocity (configurable,
default ~100) for note-on; note-off via 0x80 or 0x90 vel 0.

**Key mapping:** 28 keys → consecutive MIDI note numbers. Base note configurable (default
centered on a useful range, e.g. start at C3=48). Octave shift applied by the firmware
(§10.1 buttons), transparent to sketches.

**Joystick in PLAY mode:** X axis → MIDI pitch bend (0xE0, same channel as keyboard),
centered = no bend; Y axis → MIDI CC1 modulation (0xB0 CC 1). Deadzone around center; center
detents map to neutral.

**Patch navigation also rides MIDI** (§12): the default patch path emits Program Change
messages, consistent with external MIDI control.

**Future benefit:** because these are real MIDI events in a central path, the BLE/USB MIDI
controller modes (§16) can forward internal MIDI out to a host by tapping the same stream.

---

## 7. Operating modes & input routing

Three modes. A small mode/state manager owns the current mode and routes inputs.

| Input                     | BROWSER (select instrument) | PLAY (sketch running)                              | MENU (editing params/patch)                   |
| ------------------------- | --------------------------- | -------------------------------------------------- | --------------------------------------------- |
| Keyboard 28 keys          | — (future: text entry)      | MIDI note on/off → AMY (§6)                        | — (or live preview)                           |
| Joystick X                | nav left/right              | MIDI pitch bend → AMY                              | adjust highlighted value                      |
| Joystick Y                | nav up/down                 | MIDI CC1 mod → AMY                                 | scroll list                                   |
| Joystick button           | short = select/enter        | short = assignable; **LONG = BROWSER**             | short = edit/confirm; **LONG = exit to PLAY** |
| Octave− / Octave+ buttons | (global)                    | octave shift (§10.1)                               | octave shift                                  |
| Patch prev / next buttons | (global)                    | patch prev/next (§12); **LONG patch = patch menu** | navigate patch list                           |
| Pots                      | (optional preview)          | AMY params via external_coef/CC (§9)               | —                                             |
| Touch strips              | optional direct-select      | per-sketch (pitch or control)                      | —                                             |
| NeoPixels                 | highlight selection         | pot-position color or override (§10.2)             | highlight                                     |
| Display                   | instrument list             | sketch UI / status                                 | param list / patch list                       |

Transitions:
- **BROWSER → PLAY:** short-press on a highlighted instrument.
- **PLAY → BROWSER:** long-press joystick button.
- **PLAY → MENU:** long-press a patch button (opens patch menu) or a configurable menu
  gesture; MENU → PLAY via long-press joystick or an exit item.
- MENU is reachable in BROWSER too for global settings (MIDI channel, adc_chip, etc.).

**Long-press detection:** track button GPIO press duration (debounced). Held
≥ `LONG_PRESS_MS` (default 800ms) → emit one `long_press` event *at the threshold* (fire
during the hold for responsiveness); else on release emit `short_press`. A long-press never
also emits a short-press. **Joystick long-press is always reserved for "back to BROWSER";
sketches cannot capture it.**

Nav from joystick: threshold the 2 analog axes with a deadzone into up/down/left/right;
simple key-repeat while held for scrolling long lists.

The mode manager cleanly stops the current sketch + resets AMY before entering BROWSER, and
starts the selected sketch on entering PLAY (§17).

---

## 8. Performance strategy (control path must never block or lag)

Hard feel-requirements: keys/buttons < ~5ms; knobs/pots/touch < ~20–30ms (poll ≥50Hz,
target 100Hz); display ≥15fps.

MicroPython threats and mitigations:
- **T1 GC pauses (the big one).** GC can stall the VM for ms unpredictably.
  → `gc.freeze()` after all init; **zero allocation inside poll/IRQ callbacks** (pre-allocate
  buffers, reuse bytearrays, no list/dict building in hot paths); keep hot-loop graphs small.
- **T2 Busy polling starving audio.** A `while True` poll fights AMY's render task.
  → **Never busy-loop.** Pace periodic polling via AMY's scheduler (`tulip.defer` / seq
  callbacks) or a hardware timer at a fixed rate. Match the proven AMYboard pattern:
  `cv_read_task` does ADC reads on a dedicated task so the render thread never blocks on I2C.
- **T3 I2C/SPI bus contention.** Large transfers (display) block small latency-sensitive reads
  (keys) sharing a bus.
  → Put latency-sensitive devices and the display on **different I2C buses** when possible
  (both reach every slot). Update the display in chunks, not one giant transaction.
- **T4 Long ISRs.** Reading an MCP23017 over I2C inside a pin-IRQ is too slow/disallowed.
  → ISR only sets a flag or `micropython.schedule()`s the read; the I2C transaction happens
  in poll/main context.

Per-driver verdict (is Python enough?):
| Driver                         | Mechanism                        | Python OK?                                                                                      |
| ------------------------------ | -------------------------------- | ----------------------------------------------------------------------------------------------- |
| MCP23017 keys/buttons          | event-driven via INT pins        | yes, comfortably                                                                                |
| AT42QT2120 touch               | moderate I2C poll / CHANGE irq   | yes                                                                                             |
| Joystick (ADS)                 | 2 ch, subscription-polled        | yes                                                                                             |
| Legacy pots (ADS+4-GPIO mux)   | I2C-bound, ~1 ch used            | yes @ ~50–100Hz                                                                                 |
| New pots (MCP3002 SPI+counter) | 32 SPI reads + mux step per scan | **tightest path** — yes if disciplined (hw SPI, zero-alloc), first candidate for C escape hatch |

### 8.4 C escape hatch (design for it now, use only if needed)
The analog abstraction must allow a poll source to be backed **either** by a Python paced
loop **OR** by a C FreeRTOS task that caches values (like `cv_read_task`). The driver API
(`surface.knobs()` etc.) is identical either way, so promoting a jittery Python poll to C is
a drop-in change touching no driver or app code. **Do not bake "Python loop" into the API.**

---

## 9. Surface API (control + UI plane)

Note inputs (keyboard, joystick-play) go through MIDI routing (§6) and do NOT need to be read
by sketches. The surface exposes the remaining readable state, aggregated across both slots:
```python
knobs()     -> list[float]        # all pots, 0..1 (also mapped to AMY)
touch()     -> list[(pos, pres)]  # ondine strips
buttons()   -> dict[str, bool]    # control buttons, joystick btn
joystick()  -> (x, y, btn)        # raw, for UI/nav
events()    -> list               # ("nav",dir)/("select")/("long_press")/...
display()   -> Display|None       # first driver providing a display
set_leds(colors)/set_led_mode(mode)   # §10.2
start()/stop()
sync_knobs(values)                # takeover sync on instrument load (§9.1)
```
Getters return **cached** values, never block (§8). Drivers claim slot resources in
`start()`, release in `stop()`.

### 9.1 Knob takeover (endless/relative controls)
For relative controls the driver keeps `virtual_knobs[]`; on instrument load the browser
calls `surface.sync_knobs(patch_values)` so values match the patch's `_auto_generated_knobs`
block → no jump. On movement `virtual_knobs[i] += delta*step`, clamped 0–1. Plain pots:
passthrough.

### 9.2 AMY integration of knobs
Wire pot values into `amy_external_coef_hook` (port the upstream `cv_read_task` pattern,
sourcing from the driver cache) so sketches can use `ext0/ext1` CtrlCoefs and `ic` CC
mappings unchanged. Also expose via `knobs()`.

---

## 10. The daughterboard drivers

> Agent: the user will supply exact pins/test snippets per board. Below is the general
> hardware map. Wrap each into the contracts above. Do not invent behavior — ask if ambiguous.
> Declare resource needs in LOGICAL terms; the registry resolves them to the assigned slot.

### 10.1 `drivers/spark_main.py` — Spark main board
Hardware:
- **28-key keyboard**, mechanical switches wired **directly** into **2× MCP23017** I2C GPIO
  expanders (**no matrix**). 28 keys + **4 control buttons** = 32 inputs.
- MCP23017 **INT pins → slot GPIO** → **interrupt-driven** key reads (§8 T4). Software-debounce.
- **On key press/release → generate MIDI note-on/off and inject via §6.** The keyboard is a
  MIDI *source*, not a polled key-state provider.
- **Analog joystick** (2 axes) → slot ADS channels; **joystick button → a slot GPIO**.
  Dual role per §7.
- **SH1106 128×64 OLED on I2C** (may move to SPI later) → provides the surface display (§14).
  Place display and MCP23017 on different I2C buses if possible (§8 T3).

**4 control buttons — fixed roles (Spark-specific):**
| Button     | Short press                                          | Long press                      |
| ---------- | ---------------------------------------------------- | ------------------------------- |
| Octave−    | keyboard octave down (firmware note-offset, clamped) | (reserved/free)                 |
| Octave+    | keyboard octave up                                   | (reserved/free)                 |
| Patch prev | previous patch (§12)                                 | (reserved)                      |
| Patch next | next patch (§12)                                     | **open patch menu (MENU mode)** |

Octave shift is a firmware-level note offset applied to the keyboard's MIDI note numbers;
transparent to sketches. Persisted in `state.json → surface.octave_shift`.

Capabilities: MIDI notes, pitch/mod, nav, `display`, octave/patch buttons.

### 10.2 `drivers/spark_pots_new.py` — Spark pot board (new)
Hardware:
- **16 continuous pots, two wipers each = 32 analog inputs**, muxed into a **SPI MCP3002**
  (2-ch, 10-bit). Mux addressed by a **binary counter** driven by **2 GPIO (clk, reset)**.
  Scan: reset counter, then per position read the 2 MCP3002 channels (one pot's 2 wipers) and
  pulse clk. Compute pot position from the 2 wiper inputs.
- **Optional strip of 16 NeoPixels.** LED semantics:
  - **AUTO (default):** LED `i` color tracks pot `i` position → map position to hue across a
    pleasant range (e.g. 0.0 warm/red → 1.0 cool/blue), constant brightness. **Only re-push
    when a pot changes beyond a small epsilon** (avoid constant NeoPixel writes).
  - **MANUAL / override:** `set_leds(colors)` sets explicit per-LED RGB and switches to
    MANUAL; persists until `set_led_mode("auto")` or a new `set_leds`. Hook for sequencers.
  - API: `set_led_mode("auto"|"manual")`, `set_leds(list[(r,g,b)])`.
Capabilities: `knobs` (16), `leds`. **Tightest poll path** (§8) — hardware SPI, zero-alloc
scan, scheduler-paced.

### 10.3 `drivers/spark_pots_legacy.py` — Spark pot board (legacy)
Hardware:
- **16 regular pots** multiplexed into the **mainboard ADS** (uses 1 slot ADS channel); the
  multiplexer is addressed **directly by 4 GPIO** (binary). Scan: set 4-GPIO address, read the
  ADS channel, repeat for 16.
Capabilities: `knobs` (16). Subscribes 1 mainboard-ADS channel; I2C-bound, poll ~50–100Hz.

### 10.4 `drivers/ondine.py` — Ondine
Hardware:
- **2 capacitive touch strips**, one **AT42QT2120** per strip, **one on each I2C bus**.
Capabilities: `touch` (2 strips → [(position, pressure), ...]). Pitch policy stays in the
sketch; the driver only reports position/pressure.

---

## 11. Parameter system (menu-diving, opt-in)

Menu-accessible parameters (scale, arp division, swing, effect sends, etc.) are a distinct
state tier — named, typed values, not knob positions. The firmware provides the framework;
sketches **opt in**. Community sketches that don't opt in still work (their menu just shows
global settings).

### 11.1 `cynthia/params.py` — the `Param` model
```python
class Param:
    name: str
    type: str                # "int" | "float" | "choice" | "bool"
    default: Any
    value: Any               # current value — the single source of truth
    min/max: num | None      # for int/float
    options: list | None     # for choice
    on_change: fn | None     # OPTIONAL callback
```

**Callback AND polling are both supported — they are not mutually exclusive.** The value
always lives in `param.value`; a callback is just an optional push notification:
- **Push:** sketch passes `on_change=fn`; firmware calls `fn(new_value)` immediately when the
  user edits the param in MENU mode. Use for latency-sensitive / AMY-updating params.
- **Poll:** sketch omits the callback and reads `params["swing"].value` inside `loop()`.
  Simpler; fine for params consumed per-frame.
- A sketch may mix both across its params.

### 11.2 Sketch opt-in convention (duck-typed, like the other hooks)
```python
# In a CYNTHIA-aware sketch.py
def cynthia_params():
    return [
        Param("scale", "choice", options=["chromatic","major","minor","dorian"],
              default="chromatic", on_change=_set_scale),   # push
        Param("arp_div", "int", min=1, max=16, default=4),  # poll
        Param("swing", "float", min=0.0, max=0.5, default=0.0),  # poll
        Param("reverb_send", "float", min=0.0, max=1.0, default=0.3,
              on_change=_set_reverb),                        # push
    ]
```
The browser calls `cynthia_params()` after `run_sketch()` loads the module (if present) and
builds the MENU param list from it. `on_change` callbacks must be cheap (§8) — do the
`amy.send()` and return.

### 11.3 Persistence
Param values are saved to `state.json → params` (§17) keyed by param name. On instrument
load, saved values are applied to the `Param` objects (and `on_change` fires once per changed
param so AMY state syncs). Unknown/missing keys fall back to defaults. Debounce writes (save
on MENU exit + on mode transition, not on every tick).

---

## 12. Patch system (navigation + AMY standard fallback)

CYNTHIA does **not** invent a new patch format. AMY already has patches (Juno 0–255, Gamma9001
drum kits 384–390, bank-aware via MIDI Program Change + CC0 bank select). CYNTHIA adds
**navigation, delegation, and persistence** on top.

### 12.1 PatchProvider protocol (sketch opt-in)
An instrument may claim patch control by defining:
```python
def cynthia_patches():
    return provider   # object or module implementing:
```
```python
class PatchProvider:              # duck-typed; all methods required if claimed
    def count(self) -> int
    def name(self, i) -> str
    def select(self, i) -> None   # make patch i active
    def current(self) -> int
```
If the sketch defines `cynthia_patches()`, the patch buttons/menu delegate to that provider.
This is how sketches with their own sound banks or custom program structures take control.

### 12.2 Default provider — AMY standard patches
When **no provider is claimed** (community sketch, or no sketch at all), the firmware uses a
built-in provider over **AMY's standard patch set**:
- Patch prev/next buttons and the patch menu navigate AMY patches.
- Selection is implemented by **emitting a MIDI Program Change** on the keyboard channel into
  the §6 MIDI path, so it goes through AMY's existing **bank-aware PC handler** — the exact
  same code path as an external MIDI controller. (Agent: verify PC→patch mapping in
  `amy_midi.c`; check whether AMY exposes a cleaner direct patch-load API or patch *names* to
  Python — use names in the menu if available, else numeric.)
- Default cycling range: melodic patches **0–255**; drum kits (bank MSB 3, patches 384+) are
  handled via bank select, not the prev/next cycle. Refine after inspecting AMY's patch table.
- Because buttons and external MIDI both use Program Change, patch state stays consistent
  no matter who drives it.

### 12.3 Patch menu (MENU mode)
Long-press a patch button → MENU mode showing the patch list (name + number). Joystick
scrolls, short-press selects (audition immediately), long-press joystick exits. Current patch
highlighted.

### 12.4 Persistence
Current patch index saved to `state.json → patch` (§17). On instrument load, re-select the
saved patch (via the active provider, or a Program Change for the default provider).

### 12.5 Fallback / "no sketch" behavior
When nothing claims control — **no sketch running** (booted to REPL / raw synth), or a sketch
without `cynthia_patches()` / `cynthia_params()`:
- The device is a **bare AMY synth**: patch buttons + patch menu drive AMY patches via
  Program Change; keyboard plays whatever patch is loaded; joystick sends pitch/mod.
- **REPL and external MIDI remain fully in control** — standard MIDI Program Change/bank
  select works exactly as on AMYboard. This is the "just REPL and MIDI controllable" mode.
- The menu shows global settings only (MIDI channel, octave, adc_chip, etc.).

---

## 13. (reserved)

---

## 14. Display (a daughterboard capability)

The display is the **SH1106 128×64 on the Spark-main daughterboard** (I2C today; may become
SPI). **Confirmed: no mainboard display.** Exposed via `surface.display()`, so it exists only
when that driver is active. The browser/menu degrade to headless (REPL/log) when no display.

Plan: `cynthia/display.py` → `CynthiaDisplay(amyboard.Display)` subclass:
- Keep the upstream **virtual 128×128 GS4 canvas** so community-sketch draw coordinates stay
  valid.
- Override `show()`: crop top 64 rows → threshold GS4 → 1-bit → push to the **SH1106**
  (note SH1106 page-addressing + small RAM column offset; not identical to SSD1306).
- Wire into the active driver's `start()`.
Agent: confirm SH1106 vs SSD1306 and the I2C address at implementation time; keep an eye on
the possible SPI variant.

---

## 15. I2S mic (ICS-43434) — optional audio input

The mainboard has an **ICS-43434 I2S mic** (Board.h `MIC_*` pins). **Optional — pins may be
unpopulated.**

Goal: expose it to AMY **the same way AMYboard exposes audio-in**, so sampling
(`amy.load_sample` from live input) and input processing work.
- **Agent: explore the amyboard repo for how audio-in is wired** — `run_amy()` audio_in
  config, the I2S RX capture path, and how AMY consumes input samples. Replicate for CYNTHIA's
  mic (I2S master, DMA-driven — C-level, no perf risk).
- ICS-43434 is **mono**; decide mono→stereo handling to match AMY's expectation.
- Gate on a **config flag** (`has_mic`), with an optional runtime sanity check (read a block,
  verify non-silence). Do not assume it exists.

---

## 16. Non-standard operating modes (reserve hooks, don't build yet)

CYNTHIA may run in roles beyond "standalone synth". Keep the architecture open; don't
implement now, but don't paint into a corner:
- **BLE MIDI controller**: ESP32-S3 BLE advertising a MIDI service, routing AMY MIDI in/out
  over BLE-GATT.
- **USB MIDI device**: already present (TinyUSB gadget); may be used as a pure MIDI
  interface/controller role.
Because §6 builds a central MIDI event path, these modes can **tap/forward that same stream**
(e.g. internal keyboard MIDI out to a host over BLE/USB). Treat **MIDI transport** and
**operating role** as extension points; keep AMY's MIDI routing decoupled from any single
transport. (Distinct from the BROWSER/PLAY/MENU mode of §7.)

---

## 17. Instrument browser & state

Product concept: **list of amyworld sketches = list of selectable instruments.**

Layout:
```
/user/instruments/<name>/sketch.py   # standard amyworld sketch (+ _auto_generated_knobs)
/user/instruments/<name>/meta.json   # optional: display name, author, tags
/user/instruments/<name>/state.json  # CYNTHIA-managed state (auto-saved), per-instrument
```
Install = copy a sketch folder (later: via web-editor zT transfer into that path).
**State is per-instrument (one `state.json` per folder), not global** — no orphaned state,
and "instrument = folder" stays clean.

`state.json` structure:
```json
{
  "version": 1,
  "knobs":   [0.72, 0.35, 0.0, 0.88],
  "surface": { "octave_shift": 0, "keyboard_channel": 0 },
  "params":  { "scale": "dorian", "arp_div": 8, "swing": 0.12 },
  "patch":   { "provider": "amy_default", "index": 5 },
  "sketch_state": null
}
```
Five tiers, five owners:

| Tier                | Examples                         | Owner                                    | Where                       |
| ------------------- | -------------------------------- | ---------------------------------------- | --------------------------- |
| AMY synth state     | osc configs, patches, filters    | `_auto_generated_knobs` + `run_sketch()` | sketch.py (existing)        |
| Knob positions      | physical pot values              | browser/surface                          | `state.json → knobs`        |
| Menu params         | scale, arp, swing                | param framework (§11)                    | `state.json → params`       |
| Patch selection     | AMY patch index / provider patch | patch system (§12)                       | `state.json → patch`        |
| Opaque sketch state | sequencer pos, pattern idx       | sketch save/load hooks                   | `state.json → sketch_state` |

Optional sketch state hooks (duck-typed, like the other hooks):
```python
def cynthia_save_state():  return {...}      # browser calls on exit
def cynthia_load_state(s): ...               # browser calls on load
```

`cynthia/instrument_browser.py` (runs in BROWSER mode):
- Scan `/user/instruments/*`, read meta (fallback: folder name), sort.
- Render on `surface.display()` if present, else REPL menu; joystick nav + short-press select
  (§7); touch optional direct-select (ondine).
- On selection (BROWSER → PLAY):
  1. Stop current sketch cleanly; **test which AMY reset primitive gives a clean slate**
     (`amy.send(panic=…)` / RESET_AMY path). Agent: determine empirically.
  2. Reuse existing `run_sketch()` (cd to env dir, apply `_auto_generated_knobs`,
     fresh-import, start `loop()`).
  3. Apply `state.json`: `surface.sync_knobs()` (takeover, §9.1), param values (§11.3,
     firing `on_change`), patch (§12.4), `cynthia_load_state()`.
- On exit (PLAY → BROWSER, long-press joystick):
  1. Capture `surface.knobs()` + surface settings + param values + patch index.
  2. Call `cynthia_save_state()` if defined.
  3. Write `state.json`.
- **Test explicitly:** repeated A→B→A switches for AMY state leaks (stuck notes, leftover
  oscs/patches). Document the reset recipe that works.

Boot flow (CYNTHIA branch of `_boot.py`):
```
boot → mount FS → start AMY → registry (config + autodetect) → surface.start()
     → if /user/last_instrument.txt valid: enter PLAY with it (apply its state.json)
     → else: enter BROWSER (instrument_browser)
```

---

## 18. Implementation order (each step independently testable)

1. **Verify codebase freshness** (§0.8); re-apply PATCHES.md if the amy pin moved.
2. `cynthia/pins.py` (mirror current Board.h) + `cynthia/connector.py` + `Slot`.
3. `daughterboard.json` + `config.py` + `registry.py` (a "none" surface works).
4. `cynthia/ads.py` manager (subscription polling) + verify with a raw analog input.
5. **`cynthia/midi.py`: find/build the MIDI injection point (§6)**; test with synthetic
   note-on/off → AMY makes sound.
6. `spark_main` (MCP23017 keys → MIDI; joystick; SH1106 display; 4 buttons wired per §10.1).
7. Browser v1: headless REPL menu + run_sketch reuse + AMY-reset test + long-press exit.
8. `spark_pots_legacy` (onboard ADS + 4-GPIO mux) → knobs visible, mapped to AMY (§9.2).
9. `spark_pots_new` (MCP3002 SPI + counter + NeoPixels) — apply §8 discipline, measure.
10. `ondine` (AT42QT2120 touch).
11. Browser v2 with display UI; joystick pitch/mod; knob takeover sync.
12. **Patch system** (§12): default AMY provider via Program Change + octave/patch buttons;
    verify PC→patch mapping in amy.
13. **Params** (§11): Param model + `cynthia_params()` hook + MENU param list.
14. **state.json** full save/restore (§17) incl. params + patch.
15. ICS-43434 mic audio-in (§15).
16. Battery level (BATT_LVL=1) + status UI. (Trivial, last.)

---

## 19. Upstream / maintenance notes

- Everything new is CYNTHIA-local Python or additive C guards → upstream rebases stay
  mechanical. Keep PATCHES.md updated for any new C touch point (incl. any MIDI-injection
  helper §6).
- Long-term: upstream the amy-submodule guards properly (PR → test → pin flow per AGENTS.md)
  so pin bumps stop being lossy.
- Web editor matches USB MIDI port name `/amyboard/i` — that's why `MICROPY_HW_BOARD_NAME`
  is `"CYNTHIA (AMYboard)"`. Don't "clean up" that suffix.
- If tulipcc has moved past the session snapshot: re-verify §1 assumptions
  (`esp32_common.cmake` define handling, `_boot.py` structure, `run_sketch()` signature).

---

## 20. Resolved decisions & remaining investigations

**Resolved:**
- Q2 Display: SH1106 128×64 on Spark-main daughterboard; no mainboard display. ✔
- Q3 Driver placement: frozen for stock drivers, `/user` override path if present. ✔
- Q4 ADS: default `ads1115`, some boards `ads1015`, configurable. ✔
- Q5 Joystick: pitch/mod (PLAY) + UI navigation (BROWSER/MENU). ✔
- Q6 Return to browser: long-press joystick button (≥800ms). ✔
- Q9 NeoPixel semantics: auto pot-tracking + manual override (§10.2). ✔
- Keyboard & joystick exposed to AMY as MIDI sources (§6). ✔
- MCP3002 uses hardware SPI via GPIO matrix. ✔
- Params: callback AND polling both supported; sketch picks per param (§11). ✔
- Buttons (Spark): octave−/octave+/patch-prev/patch-next; long-press patch = patch menu (§10.1). ✔
- Patch fallback: AMY standard patches via MIDI Program Change; REPL + MIDI controllable when
  nothing claims control (§12). ✔

**Remaining investigations (agent to resolve at implementation time):**
- Exact pins/test snippets per daughterboard (user will provide).
- MIDI injection entry point in amy (§6) + keyboard default MIDI channel.
- AMY PC→patch mapping, bank handling, and whether patch names are exposed to Python (§12.2).
- AMY reset recipe that gives a clean slate between instrument switches (§17).
- Mic audio-in API from the amyboard repo (§15); `has_mic` detection/config.
- SH1106 vs SSD1306 + I2C address (§14).
- Codebase freshness vs SESSION_CONTEXT snapshot (§0.8).
