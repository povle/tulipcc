# CYNTHIA Board Port Notes

## Hardware Summary

CYNTHIA is a compact ESP32-S3 music controller with:
- **MCU**: ESP32-S3 (dual-core, 240MHz)
- **Flash**: 16MB
- **PSRAM**: 8MB octal, 120MHz
- **Audio Output**: PCM5102A I2S DAC (BCLK=GPIO38, DIN=GPIO39, LRCLK=GPIO40, no MCLK)
- **ADC**: ADS1015/ADS1115 over I2C (SDA=GPIO17, SCL=GPIO16, addr=0x48)
- **USB**: Native ESP32-S3 USB (Serial/JTAG mode for REPL)
- **Optional** (not yet supported): I2C display (SDA=GPIO5, SCL=GPIO4), I2C keyboard, multiplexed potentiometers

## Pin Mapping (from ~/projects/synth)

| Function | GPIO |
|----------|------|
| I2S BCLK | 38 |
| I2S DIN | 39 |
| I2S LRCLK | 40 |
| I2C SDA | 17 |
| I2C SCL | 16 |
| Display SDA | 5 |
| Display SCL | 4 |
| Battery Level | 7 |
| Battery Stat 1 | 6 |
| Battery Stat 2 | 5 |
| MUX S0 | 47 |
| MUX S1 | 48 |
| MUX S2 | 21 |
| MUX S3 | 14 |
| Joystick Button | 3 |
| KB Interrupt 1 | 11 |
| KB Interrupt 2 | 12 |
| LED | 48 |

## Build Configuration

### Files Created
- `tulip/amyboard/boards/CYNTHIA/mpconfigboard.cmake`
- `tulip/amyboard/boards/CYNTHIA/mpconfigboard.h`
- `tulip/amyboard/boards/CYNTHIA/pins.h`
- `tulip/amyboard/boards/CYNTHIA/pins.csv`
- `tulip/amyboard/boards/CYNTHIA/sdkconfig.board`
- `tulip/amyboard/boards/CYNTHIA/board.json`
- `tulip/amyboard/boards/CYNTHIA/tulip-partitions-16MB.csv`

### Key SDK Config Settings
- `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`
- `CONFIG_SPIRAM_MODE_OCT=y`
- `CONFIG_SPIRAM_SPEED_120M=y`
- `CONFIG_ESPTOOLPY_FLASHFREQ_120M=y`

### USB Mode
Using TinyUSB CDC for REPL + USB MIDI (required for web UI compatibility):
- `MICROPY_HW_USB_CDC (1)`
- `MICROPY_HW_ESP_USB_SERIAL_JTAG (0)`
- `MICROPY_HW_ENABLE_USBDEV (1)`
- `MICROPY_HW_USB_CDC_1200BPS_TOUCH (1)`
- `CFG_TUD_MIDI_RX_BUFSIZE 512`
- `CFG_TUD_MIDI_TX_BUFSIZE 64`

**Note:** USB Serial/JTAG mode does NOT provide USB MIDI ports, which are required for the AMYboard Web UI (amyboard.com) to communicate with the board via WebMIDI sysex messages.

### Board Name
`MICROPY_HW_BOARD_NAME` is set to `"CYNTHIA (AMYboard)"` so the web UI's `/amyboard/i` regex matches the USB MIDI port name.

## Issues Found and Fixed

### 1. mp_main_task_handle Not Initialized
**Symptom**: Crash (LoadProhibited at 0x164) when any input sent to REPL
**Cause**: `mp_main_task_handle` in `mphalport.c` was never set. USB Serial/JTAG ISR calls `mp_hal_wake_main_task_from_isr()` which dereferences this NULL handle.
**Fix**: Added initialization in `mp_task()`:
```c
extern TaskHandle_t mp_main_task_handle;
mp_main_task_handle = xTaskGetCurrentTaskHandle();
```

### 2. _boot.py ENODEV on Partition Mount
**Symptom**: OSError: [Errno 19] ENODEV when mounting flash partitions
**Cause**: Partitions not formatted on first boot
**Fix**: Added auto-format fallback in `_boot.py`

### 3. help() Hanging
**Symptom**: REPL hangs when `help()` called
**Cause**: `tulip.py` imports graphics modules that don't exist on headless CYNTHIA
**Fix**: Added board guard in `tulip.py`:
```python
if board() != 'AMYBOARD' and board() != "AMYBOARD_WEB" and board() != "AMYBOARD_VCV" and board() != "CYNTHIA":
    from tulip_graphics import *
    from ui import *
    from editor import edit
```

### 4. MIDI UART Pin Conflict
**Symptom**: MIDI initialization fails or conflicts with I2S pins
**Cause**: CYNTHIA has no MIDI hardware, but code tries to initialize UART on dummy pins
**Fix**: Skip MIDI initialization for CYNTHIA in `_boot.py` and `amyboard.py`

### 5. PCM9211 and SD Card Not Present
**Symptom**: I2C errors during boot
**Cause**: CYNTHIA has PCM5102A (not PCM9211) and no SD card slot
**Fix**: Added board guards in `amyboard.py`:
```python
def init_pcm9211(addr=0x40):
    if tulip.board() != "AMYBOARD":
        return
    # ... rest of init

def mount_sd():
    if tulip.board() != "AMYBOARD":
        return
    # ... rest of mount
```

### 6. tsequencer_init() Depends on amy_global
**Symptom**: Crash if tsequencer_init() called before Python initializes AMY
**Cause**: `tsequencer_init()` in C code accesses `amy_global` which is NULL until Python runs
**Fix**: Skip `tsequencer_init()` for CYNTHIA in `app_main()`:
```c
#ifndef CYNTHIA
    tsequencer_init();
#endif
```

### 7. i2c_follower_init() and cv_read_task AMYboard-Specific
**Symptom**: I2C errors on Core 0
**Cause**: These tasks are for AMYboard's CV hardware, not present on CYNTHIA
**Fix**: Guarded with `#ifndef CYNTHIA` in `main.c`

### 8. Hardcoded AMYBOARD in esp32_common.cmake (ROOT CAUSE of amy_start hang)
**Symptom**: `amy_start()` hangs forever, never returns. Debug shows `audio_in=1` even though CYNTHIA config sets it to 0.
**Cause**: `tulip/amyboard/esp32_common.cmake` line 344 had `AMYBOARD` hardcoded in `target_compile_definitions`. This meant **both** `AMYBOARD` and `CYNTHIA` were always defined when building for CYNTHIA.

This caused two problems:
1. In `amy_connector.c`, `#if defined(AMYBOARD)` took precedence over `#elif defined(CYNTHIA)`, so `audio_in=1` was used instead of `audio_in=0`.
2. In `amy/src/i2s.c`, the condition `#if !defined(AMYBOARD) && !defined(AMYBOARD_ARDUINO)` was false, so the AMYBOARD-specific I2S setup was used instead of the default ESP I2S setup.

**The I2S setup difference is critical**:
- **AMYBOARD** uses `I2S_ROLE_SLAVE` with `I2S_CLK_SRC_EXTERNAL` because it has a PCM9211 SPDIF transceiver that provides the master clock.
- **CYNTHIA** needs `I2S_ROLE_MASTER` with internal clock because PCM5102A is a simple DAC that expects the MCU to generate the clock.

When CYNTHIA tried to use `I2S_ROLE_SLAVE` with no external clock source, `i2s_channel_init_std_mode()` hung waiting for a clock that never came.

**Fix**: Removed hardcoded `AMYBOARD` from `esp32_common.cmake`. Board defines now come only from `BOARD_DEFINITION1` and `BOARD_DEFINITION2` set in each board's `mpconfigboard.cmake`:
- AMYBOARD builds: `AMYBOARD` + `MAKERFABS`
- CYNTHIA builds: `CYNTHIA` only

### 9. Build Errors After Removing Hardcoded AMYBOARD
After removing the hardcoded `AMYBOARD`, several `#if` guards needed `CYNTHIA` added:

**tulip/shared/tulip_helpers.c**:
```c
#if !defined(AMYBOARD) && !defined(AMYBOARD_WEB) && !defined(AMYBOARD_VCV) && !defined(CYNTHIA)
#include "ui.h"
```

**tulip/shared/amy_connector.c** (multiple locations):
```c
// File hooks section
#if (defined AMYBOARD) || (defined TULIP) || (defined AMYBOARD_VCV) || (defined CYNTHIA)

// USB HOST MIDI exclusion
#if !defined(TDECK) && !defined(AMYBOARD) && !defined(CYNTHIA)

// run_amy() CYNTHIA config (must come before AMYBOARD)
#if defined(CYNTHIA)
    amy_config.features.audio_in = 0;
#elif defined(AMYBOARD)
    amy_config.features.audio_in = 1;
```

### 10. USB MIDI Input Not Working (Web UI zI Ping Timeout)
**Symptom**: Web UI (amyboard.com) sees MIDI ports but board doesn't respond to zI sysex pings. Console shows `inputs=1 outputs=1` but `wait_for_board_ready: timed out`.
**Cause**: Two issues:
1. In `amy/src/amy_midi.c`, the `run_midi_task()` function's `check_tusb_midi()` call was guarded by `#if defined (AMYBOARD) || defined(AMYBOARD_ARDUINO)` — CYNTHIA was not included.
2. `MICROPY_PY_TINYUSB` was a CMake variable but not a C preprocessor define, so `#ifdef MICROPY_PY_TINYUSB` was never true.

**Fix**:
1. Added `CYNTHIA` to the guard in `run_midi_task()`:
```c
#if defined (AMYBOARD) || defined(AMYBOARD_ARDUINO) || defined(CYNTHIA)
#ifdef MICROPY_PY_TINYUSB
check_tusb_midi();
#endif
#endif
```

2. Added `MICROPY_PY_TINYUSB=1` as a compile definition in `tulip/amyboard/esp32_common.cmake`:
```cmake
list(APPEND MICROPY_DEF_TINYUSB
    MICROPY_PY_TINYUSB=1
    CFG_TUSB_MCU=${tusb_mcu}
)
```

This also enables `TUD_USB_GADGET` which is needed for `midi_out()` to send USB MIDI replies.

### 11. zP (Run Python Code) Not Working — Web UI "Read" Fails
**Symptom**: Web UI zI ping works, but zP messages (run Python code via sysex) don't get ACKed. Web UI shows "board replies to zI but not zP — wedged".
**Cause**: Multiple `#if defined(TULIP) || defined(AMYBOARD)` guards in `amy/src/amy_midi.c`, `amy/src/parse.c`, and `amy/src/amy_midi.h` were missing CYNTHIA:
1. `amy_midi.c` line 5: `#include "py/runtime.h"` guard
2. `amy_midi.c` line 279: `extern tulip_amy_send_sysex_obj` declaration
3. `amy_midi.c` line 326: `mp_sched_schedule()` call for deferred Python execution
4. `amy_midi.c` line ~500: `mp_usbd_task()` call in `midi_out()` for USB FIFO draining
5. `parse.c` line 7: `#include "py/runtime.h"` guard
6. `parse.c` line ~380: `extern tulip_pcm_load_file_obj` declaration
7. `parse.c` line ~560: `mp_sched_schedule()` for PCM file loading
8. `amy_midi.h` line 56: `SYSEX_COPY_SLOTS` definition (was 0 for CYNTHIA, causing div-by-zero)

**Fix**: Added `CYNTHIA` to all these guards. The key mechanism:
- zI ping is handled in pure C (no scheduler needed)
- zP messages must be deferred to MicroPython's main loop via `mp_sched_schedule()`
- Without CYNTHIA in the guards, zP fell through to `amy_add_message()` which doesn't execute Python or send ACKs

### 12. Web UI Stopped Working Again — amy Submodule Reset Lost the Guards (REGRESSION)
**Symptom**: Web editor (amyboard.com) stopped working with CYNTHIA even though issues #10/#11 were previously fixed and verified.
**Root cause**: The fixes for #10/#11 live in the `amy` submodule, which is pinned by commit. When the submodule was updated/reset to a new pin, the local CYNTHIA guard edits were discarded — the fresh checkout had zero `CYNTHIA` references (`git grep CYNTHIA amy/src/` was empty). Additionally, four inner hook guards in `tulip/shared/amy_connector.c` (`mp_exec_hook`, `mp_update_file_hook`, `mp_file_transfer_done_hook`, `mp_reboot_hook`) were missing CYNTHIA even though the outer section guard included it — so zP/zA/zT/zB would have been no-ops regardless.
**General lesson**: amy-submodule CYNTHIA guards are NOT optional patches — they are part of the CYNTHIA port and must be re-applied on every amy pin bump. See `PATCHES.md` in this directory for the complete, LLM-reappliable list of all 14 guard sites (10 in amy, 4 in amy_connector.c) plus every tulipcc-side change.
**Fix (systematic, this time documented for merges)**:
1. Re-applied all 10 amy guards (`amy_midi.h` ×1, `amy_midi.c` ×6, `parse.c` ×3) — each marked with a `CYNTHIA:` comment.
2. Added CYNTHIA to the 4 inner hook guards in `amy_connector.c` (zP exec, zA update-file, zT transfer-done, zB reboot).
3. Wrote `PATCHES.md` as the single source of truth for future merges.
**Verification checklist for the full web handshake** (all must pass after any amy bump):
- zI ping → `OK` sysex reply (pure C; needs `midi_out()` USB-gadget path + `check_tusb_midi()` input)
- zP → ACK + Python executes (needs sysex ring slots + `mp_sched_schedule` + `mp_exec_hook`)
- zA → ACK + sketch.py updated (`mp_update_file_hook`)
- zT → ACK + file written + sketch restart (`mp_file_transfer_done_hook`)
- zB → reboot (`mp_reboot_hook`)


### 13. Python Input Polling Bottleneck & Replacement with Dedicated C Task
**Symptom**: Polling 16 pots and 2 MCP23017 expanders in the MicroPython sequencer loop caused timing jitter, slow UI updates, and I2C collisions.
**Fix**: Implemented `cynthia_input_task` in `tulip/amyboard/boards/CYNTHIA/cynthia_input.c` running on Core 0 at priority `ESP_TASK_PRIO_MIN + 1`.
- Keys (notes 45..72) are debounced and injected directly via `midi_local()`.
- Control buttons (octave up/down, patch prev/next) are debounced with short/long press queues.
- Pots (74HC4067 MUX into ADS1115 AIN2) are scanned in round-robin fashion with EMA smoothing.
- Joystick X/Y and button are continuously sampled.
- State is exposed to MicroPython via `tulip.cynthia_*` fast C getters.

### 14. `app_main` Task Launch Guard Fixed
**Symptom**: `cynthia_input_task` was never created during boot (`cynthia_status()` returned 0 loops).
**Cause**: The task launch was nested inside `#ifdef AMYBOARD` / `#ifndef CYNTHIA` / `#else`, which was skipped when compiling for `CYNTHIA` alone.
**Fix**: Replaced the nesting with `#ifdef AMYBOARD ... #elif defined(CYNTHIA) ... #endif` in `tulip/amyboard/main.c`.

### 15. Dual I2C Bus Probing for ADS1115 & MCP23017s
**Symptom**: `s_ads_present` stayed false; knobs returned all 0.0.
**Cause**: On CYNTHIA hardware, ADS1115 sits on Bus 1 (GPIO 5 SDA, GPIO 4 SCL), while MCP23017 expanders sit on Bus 0 (GPIO 17 SDA, GPIO 16 SCL).
**Fix**: `cynthia_input_init()` initializes both `I2C_NUM_0` and `I2C_NUM_1` and dynamically detects the active port for ADS1115 and MCPs.

### 16. ADS1115 Channel & Matrix Mapping Corrections
**Symptom**: Knob movements changed joystick values; octave buttons and keyboard notes were swapped.
**Cause**:
1. ADS1115 MUX channel was configured as AIN1 instead of AIN2 (`ADC_A_2`).
2. Octave buttons PA4/PA5 were reversed.
3. Row 2 of the key matrix uses physical wiring `{56, 58, 55, 57, 59}` (G#3, A#3, G3, A3, B3) rather than linear pin order.
**Fix**: Corrected ADS MUX channel to AIN2, swapped PA4/PA5 for octave buttons, and mapped key pins strictly to the chromatic scale notes 45..72.

### 17. Juno Patch State Crash Fixed
**Symptom**: Modifying patch parameters in `juno.py` raised `AttributeError: 'JunoPatch' object has no attribute 'pwm_osc' / 'ctl_osc'`.
**Cause**: `JunoInstrument.set_patch()` recreated the patch but omitted `init_AMY()`, leaving oscillator indices unassigned.
**Fix**: Added `self.patch.init_AMY()` to `set_patch()` in `cynthia/instruments/juno.py`.
### 18. Boot Pot Overwrite and Staggered Prescan Jump
**Symptom**: On boot or preset load, knobs overrode instrument parameters immediately, and starting on DX7 reloaded a Juno sound once pots were read.
**Cause**:
1. `s_knob_values` was initialized to 0.0f (then 0.5f) and each channel was only scanned round-robin (1 channel every 5ms). Channels read later in the sequence jumped from 0.5f to their real values during the first ~80ms, triggering spurious `knob_changes()` events that called `update_pot()`.
2. `surface.start()` initialized `_last = [-1.0] * 16`, guaranteeing all 16 pots would look like massive changes on first poll.
**Fix**:
1. Added full initial prescan of all 16 channels + joystick axes in `cynthia_input_init()` with `s_first_pot_read` snap-to-real logic.
2. Initialized `_last = []` in `surface.start()`, letting `knob_changes()` align to real hardware positions on the first pass without firing changes.

### 19. U8G2 Font Engine & Exact String Width Alignment
**Symptom**: Font rendering missing on OLED; column headers and patch names misaligned.
**Cause**: Original firmware used Arduino U8G2 font data. `framebuf` standard 8x8 font lacked glyph fidelity and advance metrics.
**Fix**: Added `cynthia_ui.c` compiling 7 original U8G2 fonts into firmware with MONO_VLSB rendering (`tulip.cynthia_draw_str`), U8G2-exact advance width calculation (`tulip.cynthia_str_width`), and hardware battery ADC reading (`tulip.cynthia_battery_voltage`).

### 20. Custom User Patch System
**Symptom**: User edits to knobs were lost across reboots and patch switches.
**Cause**: Only ROM presets existed in MicroPython instruments; no persistent save/load mechanism.
**Fix**:
1. Implemented JSON-backed user patch system under `/user/patches/<instrument>/<idx>.json` with `save_patch()`, `load_patch()`, `list_user_patches()`.
2. Added `save_patch_state()` / `load_patch_state()` to `JunoInstrument` capturing all 28 synth parameters.
3. Added interactive patch browser UI to `runtime.py`: long-press patch prev saves current state to user slot; long-press patch next toggles `[Default]` vs `[User]` patch list with scroll and joystick select.

## Current Status

### Working ✓
- Boot completes without crash
- USB Serial/JTAG REPL works
- PSRAM allocation works (2MB heap)
- AMY synth engine starts and produces audio
- PCM5102A I2S audio output works
- Web UI detects the board (via "CYNTHIA (AMYboard)" port name)
- Full web-editor sysex handshake: zI ping, zP run-Python, zA knobs, zT file transfer, zB reboot
- Dedicated C FreeRTOS input scanning task (`cynthia_input_task`):
  - 28 mechanical chromatic keys (notes 45..72) debounced and injected as MIDI
  - 4 control buttons (octave up/down, patch prev/next)
  - 16 multiplexed potentiometers (74HC4067 into ADS1115 AIN2) with EMA smoothing
  - Analog joystick X/Y axes + digital button
  - Fast C getters exposed to MicroPython (`tulip.cynthia_*`)
- SH1106 I2C OLED display driver (on Bus 1)
- Juno and DX7 engine patch parameter routing
- U8G2 font rendering engine with 7 built-in fonts (`cynthia_ui.c`)
- Universal 500ms knob parameter overlay on top header bar
- Interactive 2x3 instrument grid menu (long-press joystick click to open, joystick to navigate, click to select)
- Guided joystick calibration routine with flash persistence (`/user/joystick_cal.json`)
- Custom user patch system with on-device save/load and patch list overlay
## Files Modified in tulipcc

CYNTHIA is designed to touch upstream shorepine code as little as possible:
**41 new files** (board dir + `cynthia-py/` package + bundled instruments) and
**12 modified upstream files, ~340 lines total**. Everything else is new files.

### CMake (1 line removed)
- `tulip/amyboard/esp32_common.cmake` - Removed the hardcoded `AMYBOARD` define.
  Board identity now comes solely from `${BOARD_DEFINITION1}` (AMYBOARD and
  TULIP4_R11 set their own; CYNTHIA sets `CYNTHIA`). Removed for CYNTHIA: the
  hardcoded define forced I2S *slave* mode (PCM9211/SPDIF), which hangs
  `i2s_channel_init_std_mode()` on CYNTHIA's PCM5102A (issue #8).

### C Code (additive guards only)
- `tulip/amyboard/main.c` (+20) - `mp_main_task_handle` init in `mp_task()`
  (USB-CDC ISR wake-up; fixes a latent NULL-deref on any USB-CDC board), and one
  `#elif defined(CYNTHIA)` arm to start `cynthia_input_task`. AMYBOARD's task
  block unchanged, just wrapped in `#ifdef AMYBOARD`.
- `tulip/amyboard/tasks.h` (+6) - `CYNTHIA_INPUT_TASK_*` defines + handle extern (additive).
- `tulip/amyboard/amyboard_support.c` (+8/−) - `#ifdef AMYBOARD` around the
  I2C-follower and `cv_read_task` (AMYboard-only hardware); no-op for others.
- `tulip/shared/modtulip.c` (+149) - `tulip_board()` returns `"CYNTHIA"`, the
  `ui.h` include guard, and a `#ifdef CYNTHIA` block of `cynthia_*` Python
  bindings (with `#else` stubs for other boards). No shared behavior changed.
- `tulip/shared/tulip_helpers.c` (+2/−) - additive `!defined(CYNTHIA)` on the `ui.h` include.
- `tulip/shared/amy_connector.c` (+11/−10) - additive `|| defined(CYNTHIA)` on
  6 guards, plus one `#if defined(CYNTHIA)` arm in `run_amy()` (audio_in=0,
  UART|USB_GADGET MIDI). Removed: CYNTHIA is excluded from the USB-HOST MIDI path.

### Python (board-name guards only)
- `tulip/shared/py/_boot.py` (+65) - CYNTHIA boot branch (headless, no
  `init_display()`), instrument restore, registry/ADS startup. Also a generic
  first-boot partition auto-format (helps any board, not CYNTHIA-specific).
- `tulip/shared/py/tulip.py` (+3/−1) - skip graphics/UI imports for CYNTHIA (headless).
- `tulip/shared/amyboard-py/amyboard.py` (+22/−) - CYNTHIA arms in `mount_sd()`
  (no SD), `init_pcm9211()` (no PCM9211), `init_gp8413()` (no GP8413), and
  `start_amy()` (MIDI UART skipped).
- `tulip/amyboard/boards/manifest.py` (+1) - `package("cynthia", ...)` (additive).
- `tulip/fs_create.py` (+72/−1) - CYNTHIA-gated 1200bps-touch flashing helper
  plus a generic `flash-notouch` mode. Runtime-gated: `if MICROPY_BOARD == 'CYNTHIA'`.

### amy submodule (3 files, 10 additive guard sites — see PATCHES.md)
Must be re-applied on every amy pin bump; the goal is to upstream them so the
diff becomes zero (then pin bumps are lossless automatically):
- `amy/src/amy_midi.h` - 1 guard (`SYSEX_COPY_SLOTS`)
- `amy/src/amy_midi.c` - 6 guards (`py/runtime.h`, sysex extern, `mp_sched_schedule`,
  `TUD_USB_GADGET`, `check_tusb_midi`, `mp_usbd_task`)
- `amy/src/parse.c` - 3 guards (`py/runtime.h`, pcm_load extern, `mp_sched_schedule`)

### Removed during cleanup (2026-10-12)
- `tulip/esp32s3/usb.c` — reverted entirely; was **pure debug `fprintf` scaffolding**.
- Debug `fprintf` traces in `main.c`, `amy_connector.c`, `_boot.py` (~95 lines).
- `MICROPY_PY_TINYUSB=1` in the shared cmake — referenced by **nothing** (dead).
- `tulip_midi_local()` buffer-protocol widening in `modtulip.c` — reverted to
  upstream; `cynthia/midi.py` now snapshots its `bytearray` to cached `bytes`
  instead (preserves zero-alloc, keeps the shared function untouched).


## Build Command

```bash
cd tulip/amyboard
source ~/.espressif/python_env/idf5.4_py3.13_env/bin/activate
source ~/esp/esp-idf/export.sh
idf.py -DMICROPY_BOARD=CYNTHIA build
```

## Flash Command

```bash
idf.py -p /dev/cu.usbmodem* -DMICROPY_BOARD=CYNTHIA flash monitor
```

## Key Lessons Learned

1. **Check compile definitions carefully**: A hardcoded define in CMake can override board-specific configuration in subtle ways.

2. **I2S master vs slave matters**: PCM5102A (simple DAC) needs the MCU to generate clocks (master mode). PCM9211 (SPDIF transceiver) provides clocks externally (slave mode). Using the wrong mode causes silent hangs with no error messages.

3. **Order of #if checks matters**: When multiple board defines are active, the first matching `#if` wins. Put more specific checks (CYNTHIA) before general ones (AMYBOARD).

4. **USB Serial/JTAG requires mp_main_task_handle**: The ISR needs this to wake the main task. Without it, the REPL appears but crashes on any input.