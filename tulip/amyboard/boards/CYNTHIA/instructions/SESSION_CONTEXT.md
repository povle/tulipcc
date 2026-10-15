# CYNTHIA × tulipcc — Full Session Context

> **Purpose**: hand this file to a fresh AI session (or human) and they can continue
> work on the CYNTHIA port without re-deriving anything. Last updated 2026-10-09 (dedicated C input task & hardware mapping completed).
> Companion files in this directory: `PORT_NOTES.md` (issue history #1–#12),
> `PATCHES.md` (exact patch sites for future merges).

---

## 1. What is tulipcc?

`tulipcc` (github.com/shorepine/tulipcc) is the firmware repo for **AMYboard** —
an ESP32-S3 music microcontroller. Architecture:

- **MicroPython** (ESP32 port, submodule `micropython/`) — REPL, VFS, runs user Python
- **AMY** (submodule `amy/`) — the synth engine: oscillators, FM, wavetables, effects,
  MIDI parsing, wire-string protocol, sequencer, sysex transfer layer
- **Glue** (~10% of the code):
  - `tulip/shared/amy_connector.c` — bidirectional hooks between C and Python
    (exec/file/reboot/MIDI hooks, `run_amy()` config)
  - `tulip/shared/modtulip.c` — the `tulip` Python C-extension module
  - `tulip/shared/amyboard-py/amyboard.py` (~1500 lines) — sketch lifecycle,
    display driver, knobs, OTA, web-editor support
  - `tulip/shared/py/_boot.py` — boot sequence: mounts `/sys` + `/user`, starts AMY,
    runs `sketch.py`

**Web editor** (amyboard.com, in `tulip/amyboardweb/`) talks to the board over
**WebMIDI sysex** using the "SPSS" protocol (sysex manufacturer id `00 03 45`):

| Message | Function |
|---------|----------|
| `zI` | ping → board replies `OK` sysex (pure C) |
| `zP<code>Z` | run Python code on device → ACK |
| `zA[filename]Z` | write current synth state into sketch.py knobs block → ACK |
| `zT<name>,<size>Z` + data | file transfer (sketch upload) → ACK, then restart sketch |
| `zD[file]Z` | dump synth state or file contents back over sysex |
| `zB[mode]Z` | reboot (0=bootloader, 1=normal, 2=ROM flash) |

Flow control: sender waits for an ACK (`F0 00 03 45 'A' 'K' F7`) after each message.
Sysex arriving on the MIDI task is copied into an 8-slot ring
(`SYSEX_COPY_SLOTS`) and deferred to the MicroPython main loop via
`mp_sched_schedule(tulip_amy_send_sysex_obj)`; the ACK is sent *after* processing.

**"amyworld" sketches** are community Python scripts (a `sketch.py` defining synths +
a `loop()`), loaded onto the board via the web editor. Sketch synth settings are
stored in an `_auto_generated_knobs` block inside sketch.py.

---

## 2. What is CYNTHIA?

A compact ESP32-S3 synth board (user's own hardware), similar to AMYboard but smaller:

| Component | CYNTHIA | AMYboard |
|-----------|---------|----------|
| MCU | ESP32-S3 | ESP32-S3 |
| Flash | 16MB | 16MB |
| PSRAM | 8MB octal @120MHz | 8MB |
| Audio out | **PCM5102A** I2S DAC (BCLK=38, DIN=39, LRCLK=40, no MCLK, **I2S master**) | PCM9211 SPDIF transceiver (**I2S slave**, external clock) |
| Audio in | none | yes |
| ADC | ADS1115 at 0x48 on I2C_1 (SDA=5, SCL=4) — **probed and driven by C input task** | ADS1015 (CV inputs) |
| MIDI | USB gadget only (no UART MIDI hw) | UART + USB |
| SD card | none | yes |
| Display | **128×64** I2C OLED (SDA=5, SCL=4) — not yet driven | 128×128 (SSD1327/SH1107) |
| Controls | 28 mechanical chromatic keys + 4 control buttons (2x MCP23017), 16 pots (74HC4067 MUX into ADS1115 AIN2), analog joystick (ADS1115 AIN0/AIN1, btn GPIO10) — **driven by dedicated C task** | pots + keys |
| Battery | level GPIO7, stat GPIO5/6 — not yet driven | — |

**User's goal**: a portable synth that runs amyworld sketches, with an on-device
instrument browser (list of sketches = list of instruments).

### Critical hardware constraint (why CYNTHIA ≠ AMYBOARD)

`amy/src/i2s.c` line 67: `#if !defined(AMYBOARD)` selects **I2S master** mode.
Defining `AMYBOARD` selects **slave** mode with `I2S_CLK_SRC_EXTERNAL` (expects the
PCM9211's clock). CYNTHIA's PCM5102A needs master mode; defining AMYBOARD makes
`i2s_channel_init_std_mode()` hang forever. **Never define AMYBOARD for CYNTHIA.**

---

## 3. The CYNTHIA board target

`tulip/amyboard/boards/CYNTHIA/`:

- `mpconfigboard.cmake` — `BOARD_DEFINITION1 CYNTHIA`, `BOARD_DEFINITION2 ""`,
  `MICROPY_PY_TINYUSB ON`
- `mpconfigboard.h` — `MICROPY_HW_BOARD_NAME "CYNTHIA (AMYboard)"`
  (the `(AMYboard)` suffix is **required**: the web editor matches USB MIDI ports
  with the regex `/amyboard/i`)
- `pins.h` / `pins.csv` — I2S/I2C pin mapping
- `sdkconfig.board` — 16MB flash, octal PSRAM @120MHz, TinyUSB device
- `tulip-partitions-16MB.csv` — partition table incl. `drums` (Gamma9001 samples)
- `board.json`

Build (from repo root):

```bash
source ~/.espressif/python_env/idf5.4_py3.13_env/bin/activate
source ~/esp/esp-idf/export.sh          # ESP-IDF 5.4.1 (NOT esp-idf-v5.4.1)
cd tulip/amyboard
rm -rf build                             # REQUIRED when switching boards (stale QSTR)
idf.py -DMICROPY_BOARD=CYNTHIA build
idf.py -p /dev/cu.usbmodem* -DMICROPY_BOARD=CYNTHIA flash monitor
```

---

## 4. All changes made (current working tree, uncommitted)

### 4a. amy submodule — 3 files, 10 additive guard sites (pin 4885773, "v1.2.58")

**These are local edits. If the amy pin changes / submodule resets, they vanish and
must be re-applied exactly as listed in `PATCHES.md` §1.** Each site carries a
`CYNTHIA:` comment marker.

| File | Site | Guard change |
|------|------|--------------|
| `src/amy_midi.h` | `SYSEX_COPY_SLOTS` (line ~59) | `defined(AMYBOARD)` → `… \|\| defined(CYNTHIA)` |
| `src/amy_midi.c` | `py/runtime.h` include (line ~7) | `TULIP \|\| AMYBOARD` → `… \|\| CYNTHIA` |
| `src/amy_midi.c` | `tulip_amy_send_sysex_obj` extern (~283) | same |
| `src/amy_midi.c` | `mp_sched_schedule` in `parse_sysex()` (~332) | same |
| `src/amy_midi.c` | `TUD_USB_GADGET` block + `check_tusb_midi()` def (~492) | `AMYBOARD \|\| AMYBOARD_ARDUINO` → `… \|\| CYNTHIA` |
| `src/amy_midi.c` | `check_tusb_midi()` call in `run_midi_task()` (~572) | same |
| `src/amy_midi.c` | `mp_usbd_task()` in `midi_out()` (~736) | `TULIP \|\| AMYBOARD` → `… \|\| CYNTHIA` |
| `src/parse.c` | `py/runtime.h` include (~9) | same |
| `src/parse.c` | `tulip_pcm_load_file_obj` extern (~318) | same |
| `src/parse.c` | `mp_sched_schedule` PCM load (~510) | `AMYBOARD \|\| TULIP` → `… \|\| CYNTHIA` |

Why each matters: without them CYNTHIA gets no USB-MIDI input polling
(`check_tusb_midi`), no USB-MIDI output draining (`mp_usbd_task`), `SYSEX_COPY_SLOTS=0`
(div-by-zero in `tulip_amy_send_sysex`), and zP/zA/zT silently dropped.

### 4b. tulipcc — modified files

| File | Change |
|------|--------|
| `tulip/amyboard/esp32_common.cmake` | **Removed hardcoded `AMYBOARD`** from `target_compile_definitions` (board define now comes from `BOARD_DEFINITION1`); added `MICROPY_PY_TINYUSB=1` to `MICROPY_DEF_TINYUSB` |
| `tulip/shared/amy_connector.c` | Outer hooks section guard `+CYNTHIA`; `tulip_send_midi_out` USB-host exclusion `+CYNTHIA`; `run_amy()` CYNTHIA branch (audio_in=0, MIDI=UART\|USB_GADGET, **before** the AMYBOARD branch); **4 inner hook guards** `+CYNTHIA`: `mp_exec_hook` (zP), `mp_update_file_hook` (zA), `mp_file_transfer_done_hook` (zT), `mp_reboot_hook` (zB). Also contains temporary `fprintf(stderr,…)` debug prints in `run_amy()` (can be removed) |
| `tulip/shared/modtulip.c` | `tulip_board()` returns `"CYNTHIA"`; UI-header exclusion guard `+!CYNTHIA`; amyboard_send/start/bootloader_mode section `+CYNTHIA` (but `amyboard_set_midi_out` stays AMYBOARD-only) |
| `tulip/shared/py/_boot.py` | CYNTHIA branches: import amyboard, `ensure_user_environment()` (no display), `start_amy()`, MIDI callback; auto-format unformatted partitions; debug prints (temporary) |
| `tulip/shared/py/tulip.py` | Skip graphics/UI imports for CYNTHIA (headless) |
| `tulip/shared/amyboard-py/amyboard.py` | Guarded PCM9211 init, SD mount, MIDI UART for non-AMYBOARD |
| `tulip/shared/tulip_helpers.c` | ui.h exclusion guard `+!CYNTHIA` |
| `tulip/amyboard/main.c` | `mp_main_task_handle` init in `mp_task()`; I2C-follower/CV tasks guarded `#ifdef AMYBOARD`; `cynthia_input_task` created under `#elif defined(CYNTHIA)`; `tsequencer_init()` guarded `#ifndef CYNTHIA` |
| `tulip/amyboard/tasks.h` | Added `CYNTHIA_INPUT_TASK_*` definitions and `cynthia_input_handle` |
| `tulip/amyboard/amyboard_support.c` | `i2c_follower_init`, `i2c_check_for_data`, `cv_read_task` guarded `#ifdef AMYBOARD` |
| `tulip/shared/modtulip.c` | Added `cynthia_knobs`, `cynthia_joystick`, `cynthia_buttons`, `cynthia_button_event`, `cynthia_octave_shift`, `cynthia_status` bindings with C fallbacks |
| `tulip/shared/cynthia-py/cynthia/` | Python driver layer (`spark_main.py`, `spark_pots_legacy.py`, `surface.py`, `midi.py`, `juno.py`) consuming cached C state |
| `tulip/amyboard/boards/CYNTHIA/` | **Board directory**: `pins.h`, `cynthia_input.c`, `cynthia_input.h`, `mpconfigboard.cmake` (compiles `cynthia_input.c`), and instructions |

### 4c. Git state

- tulipcc: changes are **staged** (`M ` in first column) except `amy_connector.c`
  which has staged+unstaged parts (`MM`), and `boards/CYNTHIA/` which is untracked.
  Nothing committed yet.
- amy submodule: 3 modified files, **unstaged**, on pin 4885773.
- `micropython` submodule shows `m` — expected dirtiness from
  `tulip/shared/grab_submodules.sh` patching (ignore per AGENTS.md).
- Known-good snapshot: `git stash@{0}` ("cynthia-option-a-working").

---

## 5. Issue history (PORT_NOTES.md, condensed)

1. `mp_main_task_handle` not initialized → REPL crash on input
2. `_boot.py` ENODEV → auto-format partitions on first boot
3. `help()` hang → skip graphics imports headless
4. MIDI UART pin conflict → skip UART MIDI hw init
5. PCM9211/SD absent → Python guards in amyboard.py
6. `tsequencer_init()` needs `amy_global` → skip for CYNTHIA
7. I2C follower/CV tasks AMYboard-only → `#ifdef AMYBOARD`
8. **ROOT CAUSE of amy_start hang**: hardcoded `AMYBOARD` define in
   esp32_common.cmake forced I2S slave mode → removed
9. Build errors after #8 → additive CYNTHIA guards in tulipcc
10. USB MIDI input dead → `check_tusb_midi()` guard + `MICROPY_PY_TINYUSB=1` define
11. zP dead → 8 amy-submodule guards (sysex deferral path)
12. **Regression**: amy pin reset silently discarded #10/#11 edits; plus 4 inner
    hook guards in amy_connector.c were missing CYNTHIA. Fixed systematically and
    documented in PATCHES.md for LLM-reapplication.

---


13. **Python sequencer polling bottleneck & latency**:
    - Polling 16 pots + 2 MCPs via Python in the sequencer loop caused timing jitter, slow knob response, and I2C contention.
    - Replaced with a dedicated C FreeRTOS task (`cynthia_input_task`) pinned to Core 0.
14. **`app_main` task creation condition**:
    - The task launch block was nested inside `#ifdef AMYBOARD` / `#ifndef CYNTHIA` / `#else`, which was completely skipped when `AMYBOARD` was undefined.
    - Fixed in `main.c` using `#elif defined(CYNTHIA)`.
15. **Dual I2C bus probing for ADS1115 and MCP23017s**:
    - ADS1115 sits on Bus 1 (GPIO 5 SDA, GPIO 4 SCL) on CYNTHIA hardware, while MCPs sit on Bus 0 (GPIO 17 SDA, GPIO 16 SCL).
    - `cynthia_input.c` initializes both buses and probes dynamically.
16. **ADS1115 analog channel & key matrix mapping**:
    - MUX signal is connected to AIN2 (`ADC_A_2` / `0x6000`), Joystick X to AIN1 (`ADC_B_1` / `0x5000`), Joystick Y to AIN0 (`ADC_A_1` / `0x4000`).
    - Fixed non-linear Row 2 key mapping ({56, 58, 55, 57, 59} -> G#3, A#3, G3, A3, B3) in both C and Python.
    - Fixed swapped octave buttons: PA5 is Octave Down, PA4 is Octave Up.
17. **Juno patch parameter update crash**:
    - `JunoInstrument.set_patch()` was missing `self.patch.init_AMY()`, causing missing oscillator attributes (`ctl_osc`, `pwm_osc`) during knob tweaks. Fixed.

## 6. Verified working (build 2026-08-07, clean `rm -rf build`)

- Boot, USB CDC REPL, PSRAM heap, AMY audio over PCM5102A
- Web editor full handshake: zI ping, zP exec, zA knobs, zT file transfer, zB reboot
- Binary: `micropython.bin` 2.7MB, 26% of app partition free

**Not yet verified on hardware since the latest rebuild** — needs flash + test with
amyboard.com.

---

## 7. Agreed strategy & roadmap (from planning discussion)

**Decision: adapt this firmware, do NOT build a custom MicroPython+AMY firmware.**
Compatibility with amyworld = the Python API surface (`amyboard.py`, sketch
lifecycle, SPSS sysex stack, frozen FS layout) — all already working on CYNTHIA.
Rebuilding it from scratch would be months of whack-a-mole.

**User's product concept**: portable synth where a filesystem/list of amyworld
sketches acts as selectable "instruments". Assessed as **feasible, additive Python
only**:

1. **Instrument library**: `/user/instruments/<name>/sketch.py` (standard amyworld
   sketch folders). Browser UI (CYNTHIA-only Python module) lists them on the
   display, joystick selects, then reuse existing `run_sketch()` machinery
   (it already: cd's to env dir, applies `_auto_generated_knobs`, fresh-imports
   sketch, starts `loop()`). Concern to test: clean AMY state reset between
   instrument switches (`amy.send(reset=…)` / RESET_AMY path).
2. **Display**: keep the 128×64 OLED (hardware respin for 128×128 rejected as too
   much work). Plan: `CynthiaDisplay` subclass of `amyboard.Display` — keep the
   virtual 128×128 GS4 canvas so community-sketch coordinates stay valid, override
   `show()` to crop top 64 rows + threshold to 1-bit + push to SSD1306-class panel
   via I2C. ~100–150 lines Python, wire into the existing CYNTHIA `_boot.py` branch.
3. **Knobs**: port `cv_read_task` (ADS1015, already in amyboard_support.c behind
   `#ifdef AMYBOARD`) + wire `amy_external_coef_hook` → `amy.coef()` and `ic` CC
   mappings work like on AMYboard.
4. **Joystick/buttons**: GPIO/ADC driver as small Python module.
5. **Battery**: GPIO7 level reading.

All steps are isolated, CYNTHIA-guarded, and keep upstream merges mechanical
(everything documented in PATCHES.md).

---

## 8. House rules (from AGENTS.md, condensed)

- Worktree edits: relative paths only; never edit `/Users/bwhitman/...` main repo.
- Submodules: use `tulip/shared/grab_submodules.sh` from `tulip/shared/`.
- `micropython` dirtiness after bootstrap is expected — ignore.
- amy changes normally require the PR→test→pin flow in AGENTS.md; the current local
  amy edits are an exception documented in PATCHES.md (upstreaming them to the amy
  repo is the long-term fix for lossless pin bumps).
- Always `rm -rf build` when switching `-DMICROPY_BOARD=` targets.
---

## 10. Cynthia UI Implementation (completed 2026-10-12)

### 10a. U8G2 Font Engine (C)

File: `tulip/amyboard/boards/CYNTHIA/cynthia_ui.c` + `cynthia_fonts.h` + `cynthia_ui.h`

7 fonts fetched from upstream U8g2 repo (`single_font_files/`) and compiled into firmware:
- `u8g2_font_5x7_tr` (status bar, headers, labels)
- `u8g2_font_4x6_tr` (micro labels, slider annotations)
- `u8g2_font_7x13_tr` (sampler / status values)
- `u8g2_font_tenfatguys_tf` (patch names, bold titles)
- `u8g2_font_tenthinnerguys_tf` (instrument grid menu labels)
- `u8g2_font_resoledbold_tr` (synth name in status bar header)
- `u8g2_font_spleen12x24_mu` (large readout numbers)

Exposed as:
- `tulip.cynthia_draw_str(buf, font_idx, x, y, str, color=1)` — renders into 1024-byte MONO_VLSB buffer
- `tulip.cynthia_str_width(font_idx, str)` — exact advance width (with U8G2 last-glyph adjustment)
- `tulip.cynthia_battery_voltage()` — reads ESP32 ADC on BATT_LVL (GPIO 1)

Added to `MICROPY_SOURCE_BOARD` in `mpconfigboard.cmake` and registered in `modtulip.c`.

### 10b. C Input Task (already documented in §4)

`cynthia_input.c`: keyboard, pots, buttons, joystick. Pots now prescan all 16 channels at init
so first Python poll sees real values (no ramp from 0.5 overwriting patch params).

### 10c. Display Driver (`cynthia/display.py`)

SH1106 128×64 via I2C. `SH1106` class with `framebuf.MONO_VLSB` backing.
Additional methods on the display object:
- `draw_str(font_idx, x, y, str, col)` — U8G2 font rendering pass-through to C
- `str_width(font_idx, str)` — pass-through to C
- `draw_header(octave, battery_v, inst_name, joystick_xy=None)` — status bar
- `draw_instrument_grid(instruments[], cursor_row, cursor_col)` — 2×3 grid

Font constants: `FONT_5X7` through `FONT_SPLEEN12X24` as module-level ints.

### 10d. Runtime (`cynthia/runtime.py`)

- `play(name)` / `stop()` / `attach_current(name)` — boot + browser→play transitions
- `_poll(tick)` — paced at ~96Hz via TulipSequence, routes pot changes, joystick MIDI, button events
- Joystick X → pitch bend, Y → CC1 modulation, every poll cycle
- Menu opens on **long-press joystick button** (index 4, 800ms), navigates with joystick axes
- Short-press joystick button in menu = **select instrument**
- **Param name overlay**: last turned knob shows parameter name + `NN%` for 500ms in header bar.
  Falls back to standard names from `_POT_NAMES[16]` table; instruments can optionally override
  with `engine.pot_name(ch)`.
- **User patch system**: `save_patch(inst, idx, name, state)`, `load_patch(inst, idx)`,
  `list_user_patches(inst)`, `delete_patch(inst, idx)` — JSON files under `/user/patches/`.

### 10e. Instruments

| Instrument | File | Patches | Voices | Pot Mapping |
|-----------|------|---------|--------|-------------|
| Juno-106   | `instruments/juno.py` | 128 built-in (AMY 0-127) | 6 | 16 params via `POT_MAP_16` + `POT_NAMES` |
| DX7        | `instruments/dx7.py` | 128 built-in (AMY 128-255) | 8 | Locked (DX7 has no front-panel editing) |

Juno `update_pot()` handles all 16 channels matching `InstrumentJuno.cpp`:
0→dco_pwm, 1→saw (binary), 2→dco_sub, 3→hpf (quantized), 4→lfo_rate,
5→vcf_lfo, 6→lfo_target (no-op), 7→vcf_freq, 8→vcf_res, 9→env_a,
10→env_d, 11→env_s, 12→env_r, 13→delay level (amy.echo),
14→delay time (amy.echo), 15→dco_noise.

### 10f. Joystick Calibration

- `surface.calibrate_joystick(samples=200)` — quick center sample
- `surface.calibrate_joystick_full()` — guided center + range via button presses
- `runtime.calibrate_ui()` — display-guided version with on-screen prompts
- Calibration saved to `/user/joystick_cal.json`, applied live via center-offset + deadzone

### 10g. Known Issues / Status

- Boot init fixed: C task now prescans all 16 pots + joystick before Python runtime starts.
- Knob takeover: `_last = []` in `surface.start()` ensures first `knob_changes()` aligns to hardware.
- DX7 carrier boxes use `fill_rect` + black text (not broken XOR).
- Custom patch save/load API is in place; button-triggered patch browser UI is pending.
- Battery voltage returns to normal header (joystick display removed).
