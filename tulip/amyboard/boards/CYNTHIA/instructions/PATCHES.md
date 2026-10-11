# CYNTHIA Integration Patches — Reference for Future Merges

This file is the **single source of truth** for every change CYNTHIA requires
relative to upstream tulipcc + the `amy` submodule. When updating tulipcc or
bumping the `amy` pin, re-apply or re-verify every patch below. All patches are
**additive** (`|| defined(CYNTHIA)` guards or new `#if defined(CYNTHIA)` blocks)
so they merge cleanly unless upstream touches the exact same lines.

## How to re-apply after an amy submodule bump

```bash
cd amy
git diff HEAD -- src/amy_midi.h src/amy_midi.c src/parse.c   # see current state
# If the diff is empty (pin was reset), re-apply the patches below.
```

Every amy patch is a one-line guard change. Search for the `CYNTHIA:` comment
marker — each patch site has one.

---

## 1. amy submodule patches (3 files, 10 guard sites)

### amy/src/amy_midi.h — 1 site

**SYSEX_COPY_SLOTS** (near line 56): the sysex copy-slot ring buffer depth.
Must be 8 (not 0) for CYNTHIA or `tulip_amy_send_sysex` divides by zero and
web-editor sysex (zP/zA/zT) is never processed.

```c
// BEFORE:
#if defined(AMYBOARD)
// AFTER:
#if defined(AMYBOARD) || defined(CYNTHIA)
#define SYSEX_COPY_SLOTS 8
```

### amy/src/amy_midi.c — 6 sites

All six are the same pattern: add `|| defined(CYNTHIA)` to an existing guard.

1. **`py/runtime.h` include** (top of file, ~line 5):
   ```c
   #if defined(TULIP) || defined(AMYBOARD) || defined(CYNTHIA)
   #include "py/runtime.h"
   ```
   Needed for `mp_sched_schedule` and `mp_usbd_task` below.

2. **`tulip_amy_send_sysex_obj` extern** (~line 279):
   ```c
   #if defined(TULIP) || defined(AMYBOARD) || defined(CYNTHIA)
   extern const mp_obj_fun_builtin_var_t tulip_amy_send_sysex_obj;
   ```

3. **`mp_sched_schedule` call in `parse_sysex()`** (~line 326):
   ```c
   #if defined(TULIP) || defined(AMYBOARD) || defined(CYNTHIA)
   {
       ...
       mp_sched_schedule(MP_OBJ_FROM_PTR(&tulip_amy_send_sysex_obj), mp_const_none);
   }
   ```
   This defers SPSS z* sysex to the MicroPython main loop. Without it, zP/zA/zT
   are silently dropped.

4. **`TUD_USB_GADGET` block / `check_tusb_midi()` definition** (~line 484):
   ```c
   #if defined (AMYBOARD) || defined(AMYBOARD_ARDUINO) || defined(CYNTHIA)
   #define TUD_USB_GADGET
   ...
   void check_tusb_midi() { ... }
   ```
   Without this, USB MIDI input from the web editor is never polled.

5. **`check_tusb_midi()` call in `run_midi_task()`** (~line 563):
   ```c
   #if defined (AMYBOARD) || defined(AMYBOARD_ARDUINO) || defined(CYNTHIA)
   check_tusb_midi();
   #endif
   ```

6. **`mp_usbd_task()` call in `midi_out()`** (~line 725):
   ```c
   #if defined(TULIP) || defined(AMYBOARD) || defined(CYNTHIA)
   mp_usbd_task();
   #endif
   ```
   Without this, USB MIDI output (zI OK reply, ACKs, zD dumps) stalls and drops
   bytes because the TinyUSB FIFO is never drained from the MP main thread.

### amy/src/parse.c — 3 sites

1. **`py/runtime.h` include** (top of file, ~line 7):
   ```c
   #if defined(TULIP) || defined(AMYBOARD) || defined(CYNTHIA)
   #include "py/runtime.h"
   ```

2. **`tulip_pcm_load_file_obj` extern** (~line 315):
   ```c
   #if defined(TULIP) || defined(AMYBOARD) || defined(CYNTHIA)
   extern const mp_obj_fun_builtin_var_t tulip_pcm_load_file_obj;
   ```

3. **`mp_sched_schedule` for PCM load in `amy_parse_transfer_layer_message()`** (~line 506):
   ```c
   #if (defined AMYBOARD) || (defined TULIP) || (defined CYNTHIA)
   mp_sched_schedule(MP_OBJ_FROM_PTR(&tulip_pcm_load_file_obj), mp_const_none);
   ```

---

## 2. tulipcc patches (tulip/shared/amy_connector.c — 4 hook guards)

These are inside the `#if (defined AMYBOARD) || (defined TULIP) || (defined AMYBOARD_VCV) || (defined CYNTHIA)`
section (line ~280). The outer guard already includes CYNTHIA; the four inner
hook functions also need it:

1. **`mp_exec_hook`** (zP — run Python from web sysex):
   ```c
   #if defined(AMYBOARD) || defined(AMYBOARD_VCV) || defined(CYNTHIA)
   ```

2. **`mp_update_file_hook`** (zA — write sketch.py from web sysex):
   ```c
   #if defined(AMYBOARD) || defined(AMYBOARD_VCV) || defined(CYNTHIA)
   ```

3. **`mp_file_transfer_done_hook`** (zT completion — restart sketch after upload):
   ```c
   #if defined(AMYBOARD) || defined(AMYBOARD_VCV) || defined(CYNTHIA)
   ```

4. **`mp_reboot_hook`** (zB — reboot from web sysex):
   ```c
   #if (defined(AMYBOARD) || defined(CYNTHIA)) && defined(ESP_PLATFORM)
   ```

---

## 3. tulipcc patches (other files)

### tulip/amyboard/esp32_common.cmake

- **Remove hardcoded `AMYBOARD`** from `target_compile_definitions` (line ~342).
  The board define now comes from `BOARD_DEFINITION1` in each board's
  `mpconfigboard.cmake`. AMYBOARD board sets `BOARD_DEFINITION1 AMYBOARD`;
  CYNTHIA sets `BOARD_DEFINITION1 CYNTHIA`.
- **Add `MICROPY_PY_TINYUSB=1`** to `MICROPY_DEF_TINYUSB` list (line ~102).
  Required for USB MIDI gadget support on both boards.

### tulip/amyboard/boards/CYNTHIA/mpconfigboard.cmake

```cmake
set(BOARD_DEFINITION1 CYNTHIA)
set(BOARD_DEFINITION2 "")
set(MICROPY_PY_TINYUSB ON)

list(APPEND MICROPY_SOURCE_BOARD
    boards/CYNTHIA/cynthia_input.c
)
```

### tulip/amyboard/boards/CYNTHIA/mpconfigboard.h

```c
#define MICROPY_HW_BOARD_NAME "CYNTHIA (AMYboard)"
```
The `(AMYboard)` suffix is required: the web editor's port-detection regex
(`/amyboard/i`) must match the USB MIDI port name.

### tulip/shared/modtulip.c

- `tulip_board()`: add `#elif defined CYNTHIA` returning `"CYNTHIA"`.
- UI includes guard: add `!defined(CYNTHIA)` to the `#if !defined(AMYBOARD)...` block.
- `#ifdef AMYBOARD` → `#if defined(AMYBOARD) || defined(CYNTHIA)` for the
  amyboard_send/amyboard_start/bootloader_mode section.
- `amyboard_set_midi_out` stays `#ifdef AMYBOARD` only (CYNTHIA has no MIDI UART).
- Add C bindings and fallbacks for:
  - `tulip.cynthia_knobs()` -> 16-tuple of pot floats (0.0..1.0)
  - `tulip.cynthia_joystick()` -> `(x, y, button_pressed)`
  - `tulip.cynthia_buttons()` -> button bitmask integer
  - `tulip.cynthia_button_event()` -> `(btn_idx, kind)` or `None`
  - `tulip.cynthia_octave_shift([val])` -> get/set octave shift
  - `tulip.cynthia_status()` -> `(loops, ads_reads, ads_present, ads_port)`

### tulip/shared/py/_boot.py

- Add `board()!="CYNTHIA"` to the edit/run import guard.
- Add `elif board()=="CYNTHIA": import amyboard` branch.
- Add `if board() == "CYNTHIA": amyboard.ensure_user_environment()` (skip display).
- Add `if board() == "CYNTHIA": amyboard.start_amy()`.
- Add `board() == "CYNTHIA"` to the MIDI callback setup guard.

### tulip/amyboard/main.c

- Guard `i2c_follower_init()` / `i2c_check_for_data` / `cv_read_task` with
  `#ifdef AMYBOARD`.
- Launch `cynthia_input_task` under `#elif defined(CYNTHIA)` in `app_main()`.
- Add `TaskHandle_t cynthia_input_handle;` declaration.
- Guard `tsequencer_init()` with `#ifndef CYNTHIA` (depends on amy_global
  which isn't ready at that point on CYNTHIA).

### tulip/amyboard/tasks.h

- Add `CYNTHIA_INPUT_TASK_PRIORITY`, `CYNTHIA_INPUT_TASK_COREID`, `CYNTHIA_INPUT_TASK_STACK_SIZE`, and `cynthia_input_handle`.

### tulip/amyboard/amyboard_support.c

- Guard `i2c_follower_init()`, `i2c_check_for_data()`, `cv_read_task()` with
  `#ifdef AMYBOARD`.

### tulip/shared/amy_connector.c — run_amy()

```c
#if defined(CYNTHIA)
    amy_config.features.audio_in = 0;  // no audio input hardware
    amy_config.midi = AMY_MIDI_IS_UART | AMY_MIDI_IS_USB_GADGET;
#elif defined(AMYBOARD)
    amy_config.features.audio_in = 1;
    amy_config.midi = AMY_MIDI_IS_UART | AMY_MIDI_IS_USB_GADGET;
```

### tulip/shared/amy_connector.c — tulip_send_midi_out()

```c
#if !defined(TDECK) && !defined(AMYBOARD) && !defined(CYNTHIA)
    if(midi_has_out) { send_usb_midi_out(buf, len); }
#endif
```

---

## 4. Why CYNTHIA cannot just define AMYBOARD

`amy/src/i2s.c` line 67: `#if !defined(AMYBOARD)` selects I2S **master** mode
(MCU generates clocks). Defining AMYBOARD selects **slave** mode with
`I2S_CLK_SRC_EXTERNAL` (expects PCM9211 SPDIF transceiver clock). CYNTHIA uses
a PCM5102A which needs master mode. Defining AMYBOARD causes `i2s_channel_init`
to hang forever waiting for an external clock that doesn't exist.

---

## 5. Web UI handshake summary (for debugging)

| Step | Sysex | Handler | CYNTHIA requirement |
|------|-------|---------|-------------------|
| Port detect | — | USB port name regex `/amyboard/i` | Board name must contain "AMYboard" |
| zI ping | `F0 00 03 45 zI Z F7` → `OK` reply | Pure C in `parse_sysex()` | `midi_out()` USB gadget path |
| zP run Python | `zP<code>Z` → ACK | `mp_sched_schedule` → `mp_exec_hook` | All amy guards + hook guard |
| zA update knobs | `zA...Z` → ACK | `mp_update_file_hook` | Hook guard |
| zT file transfer | `zT...Z` → ACK | `parse_transfer_message` + hooks | amy guards + hook guards |
| zB reboot | `zB[mode]Z` | `mp_reboot_hook` | Hook guard |
| zD dump | `zD...Z` → sysex dump | `amy_dump_*_to_sysex` | `midi_out()` USB gadget path |