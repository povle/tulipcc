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
│      + SETTINGS app on menu row 2 (§13.1)                     │
│ STATE: per-instrument state.json (knobs/params/patch) (§17)   │
│        global settings.json (locale/MIDI/BLE) (§13)           │
├──────────────────────────────────────────────────────────────┤
│ ROUTING: cynthia/midi.py (MIDI event bus/injection + taps)    │
│          cynthia/ble_midi.py (BLE-MIDI peripheral, §13.3)     │
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
| Patch prev | previous patch (§12)                                 | **open save-patch menu**         |
| Patch next | next patch (§12)                                     | **open patch menu (MENU mode)** |

The instrument browser is entered by a **long press on the joystick button** (§7); inside it,
the menu is 2 rows x 3 cols of instruments plus a **Settings bar as row 2** (§13.1). Joystick
Y picks the row (top third / middle / bottom third), X moves within the row, short press
selects, and a long press always returns to PLAY.

Octave shift is a firmware-level note offset applied to the keyboard's MIDI note numbers;
transparent to sketches. Persisted as `octave` in `/user/settings.json` (§13.1) and re-applied
at boot by `settings.apply_to_runtime()`.

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

**This is the stock board as of config revision 2.** `cynthia/config.py` seeds
`slot1: spark_pots_new` and `_migrate()` upgrades a previously seeded
`daughterboard.json` once (only if the driver name is still the old stock
`spark_pots_legacy`; a hand-picked driver is never overwritten).

Driver notes worth keeping:
- MCP3002 framing is a **leading don't-care DIN bit**, then start/SGL/ODD/MSBF
  (hence 0x68/0x78), and the first received byte is `xxxxx null B9 B8`
  (datasheet Fig. 6-1). That is why the parse is `((rx[0] & 3) << 8) | rx[1]`.
- The wiper that is not near an ADC rail is chosen by its distance to the
  **nearest** dead zone: wiper 0's are at both ends of its 340° travel, wiper
  1's at 160°/180° after the 180° offset. Measuring wiper 0 only from the 340°
  end picks a noisy value when it sits just off raw=0.
- NeoPixel writes happen only when a pot moves past `LED_EPSILON`; a 16-LED
  WS2812 push is ~0.5 ms of blocking, so redrawing unchanged colours at the
  50 Hz scan rate burned ~2.5% of the frame.
- `stop()` deinits the SPI host: `machine.SPI(id)` cannot be created twice, so
  a driver reload otherwise fails with "SPI host already in use".
- **Relative (endless) knobs.** `knob_deltas()` reports rotation, not position.
  The runtime seeds a virtual position per pot from the engine's own value
  (`JunoInstrument.get_pot()`) at every instrument/patch change, then applies
  deltas to it. That is what removes the snap: loading a patch leaves the
  parameters alone, touching a knob moves the parameter by the angle turned, and
  hitting 0 or 1 simply pins it there (reversing responds immediately). Deltas
  are emitted in 2-degree steps (`DELTA_DEADBAND_DEG`), which is the hysteresis
  that stops wiper noise from integrating into a slow drift — measured: 100
  scans of ±1-degree jitter emit nothing. Wrap is handled, so 355°→5° is +10°,
  not -350°. Engines without `get_pot` fall back to absolute+pickup.
- **When the knobs jump**, split software from hardware at the REPL:
  ```
  import cynthia.runtime as rt
  rt.set_solo_pots([0])    # only P01 applied
  rt.pots_report()         # raw0/raw1/angle for every pot
  rt.trace_pot(0, 40)      # sample P01 while turning it
  rt.pot_glitches()        # samples rejected as impossible rotation
  rt.set_pot_deadband(6)   # more hysteresis, live
  rt.pot_debug(0)          # on-screen scope for one pot (also Settings >
                           # Pots debug, which returns there on exit)
  ```
  Left/right on the stick cycles the scan setting live, and the label top-right
  says which: `20ms`..`1000ms` visit **every** mux address, `1P 20ms`..`1P 200ms`
  poll **only the selected pot**, and `OFF` stops scanning entirely. In
  single-pot mode the mux seeks just that pot's two addresses
  (`_ADDR0[n]`/`_ADDR1[n]` — they differ because the two muxes are wired in
  different orders), so it parks on those two positions instead of sweeping all
  16; other pots keep their last values. Measured: 2 ADC reads per scan versus
  32. Changing the selected pot re-seeks immediately.

  The rate alone does NOT narrow the scan — `20ms` means the mux still cycles
  all 16 addresses at the current rate and the screen only displays one pot's
  slice. `OFF` parks the counter;
  so a channel can be probed with a meter while it is not being switched. With
  the scan stopped, a
  click performs one scan instead of leaving the screen (the bottom line says
  `click=1 scan`); leaving the scope always restores 20 ms so the knobs are not
  left frozen. `rt.set_pot_scan_rate(ms|None)` and `rt.pot_scan_rate()` do the
  same from the REPL. Note `rt.trace_pot()` scans directly on top of the poll,
  so its rejection counts climb faster than the steady-state rate.
  The scope draws both raw wiper channels as bars (filtered values, with the
  raw counts printed under them), ticks at the raw count the second wiper
  should have (that 180-degree offset, +/- the tolerance band), plus the
  resulting error and the rejection count for that pot. A pair is healthy when
  the second bar's filled end sits between the two ticks and `err` stays near
  zero. It refreshes at ~10Hz: one full SH1106 push is ~23ms of I2C, so a
  faster scope would spend more than half the frame on the bus. Sampling still
  runs at the driver's 50Hz.
  `raw0`/`raw1` steady while the angle leaps → software (reconciler). Raw
  columns wandering → sensing (mux/wiring/noise). A glitch count that climbs
  with nobody touching the panel is the signature of the two wipers being
  reconciled ~180 degrees apart, i.e. wrong pairing rather than noise.
  **Wiper pairs are validated before reconciliation.** While both wipers are
  on-track they describe the same physical angle 180 degrees apart, so their two
  estimates must agree; a disagreement over `PAIR_TOL_DEG` (15) cannot be
  mechanical play and means the channels are reading different sensors. Checked
  in the ANGLE domain, not raw counts: the raw offset is +-541.6 depending on
  which side of the wrap the wiper is on, so a modulo-1023 test is wrong near
  0/360 (that mistake rejected valid samples during bring-up). A rejected pair
  holds the last good angle and increments a per-pot counter, and when both
  wipers sit in their dead zone the sample is rejected too (at a genuine
  boundary only ONE wiper is dead). `DEADZONE_RAW` is 100 counts: a wiper 60
  counts from the rail is already non-linear, and treating it as a trustworthy
  angle source produced a stable-but-wrong reading on a bad pot.
  **Isolated bad reads are the dominant failure mode** on the sample board: a
  trace showed the two wipers agreeing within 0.1-5 degrees almost every sample,
  with occasional single samples where one wiper was stale (raw1 ~500 when it
  should be ~57) or both railed. Each wiper is therefore median-filtered over the
  last three scans before the angle is computed; a one-sample corruption then
  cannot reach the angle, and real movement passes through unchanged. Timing is
  tunable per board in `daughterboard.json` (`spi_baudrate`, `mux_settle_us`) if
  a panel turns out to be noisier than that.
  **Thresholds are set from measured noise.** The panel drifts about +-10 raw
  counts (3.3 deg), i.e. ~6.6 deg peak-to-peak, so `DELTA_DEADBAND_DEG` is 7
  deg — a 5 deg threshold sat inside the drift and emitted deltas while the
  knobs were untouched. `PAIR_TOL_DEG` is 20 deg: above the noise/spike
  amplitude so a normal turn is never falsely rejected, far below the 140-175
  deg disagreement a genuinely mis-paired channel shows. Both are tunable
  (`rt.set_pot_deadband`, `delta_deadband_deg`); lower the deadband after a
  reflow once the signal is clean, since every degree of deadband is a degree
  of end-of-turn lag.
  **Odd pots are mechanically reversed** (they sit on the back of the board):
  `reverse_odd_pots` (default true) negates their deltas, so every knob turns
  the same way. The delta is flipped rather than the angle, because the angle
  stays the physical position the pair checks reason about.
  **Recovery after a bad stretch resyncs instead of jumping.** When a pair is
  rejected the value is held; on the next valid pair the position is adopted
  and no delta is emitted. Holding instead (the earlier behaviour) left the
  anchor behind, so every subsequent sample also looked like a jump and the pot
  stayed stuck while the real value moved on.
  `GLITCH_DEG` (90) rejects movement no hand can produce (25 turns/s at 50Hz),
  so a broken board degrades to "knob does nothing" instead of slamming values.
- The LED strip is optional and best-effort: a missing `neopixel` module or an
  unpopulated strip logs a warning and leaves the pots working. `neopixel` is
  frozen in `boards/manifest.py` so it is available offline.

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

## 13. Settings app, i18n and BLE-MIDI

Three features that all live in the same layer (the UI/app plane, not the synth):

### 13.1 Settings is an app, not a hidden gesture

The menu is **2 rows x 3 cols of instruments + a full-width Settings bar as row 2**
(`runtime.SETTINGS_ROW`). One joystick walk visits instruments and Settings; a
short press selects. Settings is *not* a slot after the last instrument: that
encoding put it at a grid position above row 1, which made it unreachable as
soon as there were 3+ instruments (found and fixed during implementation). A
dedicated row is reachable at any instrument count.

Layout: 2 rows x 3 cols of instruments at 42x20 px from y=12, then a
**full-width bordered Settings bar at y=53..63**. The bar uses the same face as
the instrument cells (FONT_TENTHINNERGUYS when the label is ASCII and fits,
else FONT_4X6) and inverts when selected — it has to look selectable, not like a
caption. Instrument cells prefer FONT_TENTHINNERGUYS for short names and fall
back to FONT_4X6 only for long or Cyrillic labels.

Rows: Language, MIDI channel, Velocity, Octave, BLE MIDI (toggle), BLE Name,
BLE Status (read-only), Pots (action), Joystick calibration (action), Back.

**Pots / per-instrument panel.** `pots` in Settings, or **long-press the joystick
button while the stick is pushed down**, opens a full-panel view of all 16 pots
with live bars and their parameter names from the engine's `pot_name()`. A
diagonal tick marks pots still waiting for pickup. Joystick-down is otherwise
free (its Y also sends CC1 while playing), and a centred long-press still opens
the browser, so the gestures do not collide. Joystick vertical = move row, horizontal = change
value, short press = toggle/enter, long press = back to PLAY (universal back).
Storage is `/user/settings.json` (`cynthia/settings.py`), written
tmp-file-then-rename so a power cut cannot leave an empty config; every read
falls back to the documented default, so a corrupt file never blocks boot.
Hardware wiring stays in `daughterboard.json` — different file, different
concern, so one bad file cannot take out the other.

**Joystick calibration** is reachable two ways: the `calibrate` row in Settings,
or **holding Octave- and Octave+ together for 5 seconds** anywhere in PLAY (a
held-button chord, detected from `cynthia_get_buttons()` because button *events*
only report short/long presses and have no chord). While held, the panel shows a
progress bar. The routine itself: hold-still centre sample → sweep sample with a
progress bar → save → a live dot-in-a-box test screen that exits on a click or
after 6s. It disarms the watchdog while blocking and re-arms in a `finally`, so
an aborted calibration cannot leave the poll stopped.

### 13.2 i18n (en, ru) — chrome only

`cynthia/i18n.py` holds a flat key -> string table for `en` and `ru`. Translate
**chrome only**: headers, menu/menu titles, buttons, settings labels, knob
parameter names, prompts. **Never** translate instrument names (from `meta.json`)
or patch names (user-typed) — those are content, and a Russian user who names a
patch "Bass" should still see "Bass".

Cyrillic rendering needed two things:

1. **Font data.** The U8G2 `_tr` faces are ASCII-only, so `cynthia_fonts.h` now
   carries the Cyrillic builds of the three text faces (`5x7`, `4x6`, `7x13` —
   same family, superset glyph coverage, +5.4 KB flash). The custom faces
   (`tenfatguys`, `tenthinnerguys`, `resoledbold`) and `spleen` have **no
   Cyrillic build upstream**, so `CYNTHIA_FONT_6X13B` was added as an explicit
   Cyrillic-capable bold fallback for any non-ASCII string (header instrument
   name, etc.). `display.draw_header` selects it automatically via
   `i18n.is_cyrillic`.
2. **UTF-8 decoding in C.** `cynthia_ui_draw_str` / `cynthia_ui_str_width` used
   to walk bytes, which fed each Cyrillic character to the glyph lookup as two
   unrelated codepoints (blank or garbage). Both now decode UTF-8 first
   (`utf8_next`, tolerant of truncated input).

**Latent bug found while doing this:** the Unicode branch of
`font_get_glyph_data` never reset its search pointer to
`font+23+start_pos_unicode` (u8g2's reference does), and advanced by a size byte
that could be 0 — an infinite loop. It was unreachable while every font was
ASCII-only; the Cyrillic faces make it live. Fixed to match u8g2.

### 13.2b Pot pickup, input latency, and why controls went dead

**Instrument browser: grid or list.** `menu_style` in Settings switches the
browser between the original 2x3 tile grid and a one-column list (6 visible
rows, scrolling, Settings as the last entry). The grid wastes most cells once
there are only a few instruments, and its tile labels are truncated; the list is
the better default for a small library, but the grid is kept because it matches
the original hardware.

**Pot pickup.** Loading a patch applies its parameter values, and the next poll
then fed the *physical* pot positions through `update_pot()` — overwriting them
within a few frames. That is the "the settings change on startup" report. A pot
is now *unarmed* after every instrument/patch change and its position is ignored
until it moves more than `PICKUP_EPS` (3%) from the value it held at load time.
`_arm_pot_pickup()` must be called **after** `surface.start()` (which resets the
cached knob array), or the baseline is captured as zeros.

**Pot scan rate.** One pot per input-task iteration, at a 2 ms ADS conversion
plus a 5 ms loop delay, meant a 16-pot sweep of ~112 ms — **9 Hz per pot**, which
is what "the knobs are sluggish" actually measured. Now 2 pots per iteration with
a 1 ms delay: ~46 ms per sweep (~21 Hz/pot), while the key scan still runs every
~6 ms.

**Controls going dead.** `attach_current()` returned early when no sketch was
imported, which left the poll stopped: no button scanning, no knob routing, no
display refresh and no watchdog feed. The panel still showed the last frame, so
the board looked alive while every control was inert. It now always starts the
poll and paints a generic UI when there is no engine.

**Swallowed button presses.** Octave−/Octave+ long-press had no function, so a
press that ran past the 800 ms threshold did nothing at all. It is demoted to a
normal press (except during the calibration chord, where both are held on
purpose). The button event queue is 32 deep so a burst cannot drop silently.

**Menu reachability and paging.** The cursor is now a single selection index
(`_menu_sel`) with row/col/page derived from it. Rows with no instruments are
skipped, so Settings is reachable with a single instrument (previously stepping
onto an empty row clamped the cursor back). More than 6 instruments **page**
instead of silently disappearing; left/right walks every slot, and a page change
shows `n/pages`.

### 13.2c Instruments own the synth — no AMYboard boot defaults

**The actual cause** was `_boot.py`: `midi.setup()` ran for every board except
AMYBOARD/AMYBOARD_WEB/AMYBOARD_VCV, and CYNTHIA was not excluded. That calls
`add_default_synths()`, which installs `PatchSynth(patch=0, num_voices=6)` on
channel 1 — the first Juno patch — *after* `start_amy()` had already run the
sketch. CYNTHIA is now excluded (it is an AMYboard-class board, not Tulip-REPL;
`midi.config` stays None, and the `c_fired_midi_event` callback CYNTHIA installs
does not use it). This also stops the boot bleep.

A second, smaller contributor: `amyboard.start_amy()` and `_apply_knobs_text()` install AMYboard's boot state
with **queued raw wire messages**: `S<RESET_SYNTHS>`, `i1K257iv6` (the default
Juno patch) and `i10K384iv1` (GM drums). Those are drained by AMY *later* than a
sketch's `amy.send()`, so on CYNTHIA they arrived after the DX7 sketch had set its
patch and silently replaced it — the reported "stays on the right patch for a
second, then loads the first Juno patch". Both are now skipped when
`tulip.board() == "CYNTHIA"`: a CYNTHIA instrument is a full engine that
configures synth 1 itself, and `_auto_generated_knobs` lines are still applied.
Consequence: the implicit GM drum kit on channel 10 is no longer installed; a
sketch that wants drums must say so in its knob lines (amyworld sketches do).

### 13.2d Gestures and per-instrument settings

**All hold gestures live in `cynthia/gestures.py`.** A `HoldGesture` is
"while `detect()` is true, wait N ms, then fire once", with `progress()` for the
on-screen bar. Single-button holds are *not* gestures — the C input task already
turns an 800 ms hold into a `long_press` event — so these cover only what an
event cannot express: chords and analog-axis conditions.

| Gesture | Detection | Hold | Action |
|---|---|---|---|
| Calibrate | Octave− + Octave+ | 5 s | `calibrate_ui()` |
| Local settings | stick held down (`jy < 0.2`) | 1.2 s | `open_local_settings()` |

Adding one is a single entry in `_gestures`; the progress overlay and the
"suppress inputs while this gesture is held" check (`_gesture_active(name)`) pick
it up automatically. Gestures have an `enabled` predicate so they cannot fire
while a menu is open (the local-settings gesture requires an idle PLAY screen).

**Local (per-instrument) settings** — the instrument's own panel, not the global
one:

| Row | Behaviour |
|---|---|
| Patch | current patch name; stick left/right = prev/next |
| Knobs | opens the knob-assignment editor |
| Save patch | opens the save menu |
| Settings | jumps to the global settings app |
| Back | leaves |

Future instrument-scoped features (a sequencer) belong here rather than in
global settings.

**Knob assignments** are a physical-pot → engine-parameter-channel map,
identity by default, persisted per instrument as `knobs` in
`/user/instruments/<name>/state.json` (so it survives reboot and does not follow
you to another instrument). The editor is the pots screen in edit mode: up/down
selects a physical pot, left/right chooses the parameter, and closing saves. The
read-only pots view and the parameter overlay both name the *mapped* parameter.
Persistence is defensive: a corrupt or out-of-range map falls back to identity,
and saving preserves other keys in `state.json`.

### 13.3 BLE-MIDI (port of the original `InstrumentBLE.cpp`)

`cynthia/ble_midi.py` advertises the standard BLE-MIDI service
(`03b80e5a-…`, char `7772e5db-…`) so a phone/iPad/DAW sees CYNTHIA as a MIDI
device.

The radio half maps 1:1 to the Arduino version; the **routing** half deliberately
differs. The C++ instrument hard-coded a CC number per pot. Here the firmware
already has one MIDI bus (§6), so BLE taps it in both directions:

```
internal controls --inject()--> AMY (as always)
                          \--> forwarder --> BLE notify --> host
host write --> parse --> tulip.midi_local() --> AMY
```

`cynthia/midi.py` gained `add_forwarder/remove_forwarder` for that tap.

**The 28 keys do NOT go through that bus.** They are scanned and injected in
`cynthia_input.c` (`midi_local()`), so the Python forwarder never saw a note and
BLE transmitted joystick/pot traffic but no keys at all. `cynthia_input.c` now
also pushes every injected message into a 64-entry ring
(`cynthia_midi_tap_pop()`), exposed as `tulip.cynthia_midi_tap()`; the UI poll
drains it and calls `ble_midi.forward()`. If BLE ever goes silent for notes
specifically, check that drain first.

**Two build-system gates, both of which silently disabled BLE.** If BLE ever
stops working after a submodule bump, check these first:

1. `tulip/amyboard/mpconfigport.h` defines `MICROPY_PY_BLUETOOTH (0)` for every
   Tulip board — so `CONFIG_BT_ENABLED=y` alone compiled the IDF stack but never
   MicroPython's `bluetooth` module (`nm micropython.elf | grep nimble_port_init`
   was empty). CYNTHIA now sets `MICROPY_BOARD_WANTS_BLUETOOTH` in
   `mpconfigboard.h` and the define is guarded by it.
   **This header is COPIED over `micropython/ports/esp32/mpconfigport.h` by
   `amyboard/CMakeLists.txt` on every build** — editing the submodule copy does
   nothing.
2. MicroPython's NimBLE bonding code predates IDF 5.4 (`ble_store_key_sec.ediv_rand_present`
   was renamed), so pairing/bonding and central mode are compiled out for CYNTHIA
   in the same header. BLE-MIDI needs neither.

**No aioble.** It is asyncio-based, and nothing runs an event loop on CYNTHIA
(the UI is an AMY TulipSequence callback), so `uasyncio.create_task()` from
`_boot.py` scheduled an advertising coroutine that never executed — the status
showed "Advertising" while the radio was silent. The module uses the raw
`bluetooth` API (`gap_advertise` + an IRQ handler), which needs no loop and
mirrors the C++ `BLEDevice`/`BLEServer` callbacks.

Advertisement is built by hand (`_adv_payloads`): flags, then the 128-bit service
UUID **complete** (iOS filters scan results by service), then the device name in
the scan response. 21 + 9 bytes, both under the 31-byte limit. The UUID bytes are
produced by `_uuid_bytes` rather than `bytes(bluetooth.UUID(...))` so the payload
does not depend on the UUID object supporting the buffer protocol.

Two correctness details worth keeping:

- **Echo guard.** Incoming host MIDI is injected into the shared bus, which calls
  every forwarder — including ours. Without `_echo_guard` every note would be
  sent straight back to the host (doubling, or looping with a fast host).
- **System real-time** (`0xF8`..`0xFF`) is a complete one-byte message that can
  appear *between* the bytes of a channel message; it must be emitted and must
  **not** become the new running status. System common (`0xF0`..`0xF7`) is
  dropped rather than length-parsed, so a sysex cannot eat the next message.
- Inbound writes are handed to the VM with `micropython.schedule()`, never
  processed on the NimBLE task (same reason `cynthia_input.c` uses
  `mp_sched_schedule`).

Build configuration: `CONFIG_BT_ENABLED` + NimBLE in
`boards/CYNTHIA/sdkconfig.board`, `bt` in `IDF_COMPONENTS`,
`mpnimbleport.c` in the port source list, private NimBLE include dir enabled.
NimBLE only (MicroPython's esp32 binding targets it), peripheral + broadcaster.
`CONFIG_BT_NIMBLE_HOST_TASK_STACK_SIZE=8192` — **use the `_HOST_` name**; the
shorter `CONFIG_BT_NIMBLE_TASK_STACK_SIZE` is a renamed-legacy alias that is
silently dropped from `sdkconfig.board`. MicroPython derives its BLE sync-event
stack as `(that - 1024)`, giving 7168 bytes of callback stack instead of 3072.
Cost: ~200 KB flash, 17% of the app partition still free.

**BLE is NOT started during boot.** A fault in the radio stack during boot takes
the whole boot down — no REPL, no UI, nothing to recover with (observed as
`Guru Meditation Error: Core 0 panic'ed (StoreProhibited)` right after flashing).
The runtime starts it ~2.5 s into a stable PLAY session
(`runtime._maybe_start_ble`), and the settings toggle starts/stops it live.

Two rules for the IRQ handler, both learned the hard way:

- **No `print()` and no `gap_advertise()` from the IRQ.** It runs on the NimBLE
  task (core 0) while MicroPython runs on core 1; running VM/IDF code there is
  how a radio event turns into a panic. The handler only records state, reads
  the written value, and hands it to the VM with `micropython.schedule()`.
  Re-advertising after a disconnect is done by `ble_midi.service()` from the
  poll. If the scheduler queue is full the packet is dropped — the earlier code
  called `_deliver()` inline, i.e. ran AMY's MIDI injection on the NimBLE task
  concurrently with AMY's own MIDI task on the same core.
- **NimBLE NVS persistence is off** (`CONFIG_BT_NIMBLE_NVS_PERSIST=n`): bonding
  is compiled out, so there is nothing to store and no flash path in the radio.

**Crash-loop guard.** Before the radio starts, `ble_midi.arm_pending()` writes
`/user/ble_pending`. It is removed once BLE has run for 10 s without dying. If
the file is still there at boot, the previous boot died with BLE active, so
`_boot.py` sets `ble_midi_enabled=false` and clears it. A panic in the radio
therefore costs one reboot, not a boot loop — and BLE can be re-enabled from
Settings once the cause is understood.

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
