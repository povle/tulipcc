// cynthia_wdt.h
// Software watchdog for CYNTHIA.
//
// WHY THIS EXISTS (hardware-measured, 2026-10-16):
// The IDF task watchdog was configured with CONFIG_ESP_TASK_WDT_PANIC=y and
// idle-task checking on both cores (verified in the built sdkconfig and
// sdkconfig.h). It does not fire: a Python `while True: pass` left the board
// silent for 30s against a 5s timeout, with no panic text, no reset and no USB
// re-enumeration — the board simply hung and stayed hung until a physical
// replug. On a product that is shipped to users, "press RST" is not an
// acceptable recovery, so the watchdog is owned here instead, where its
// behaviour is explicit and testable.
//
// DESIGN:
// - The CYNTHIA input task (its own FreeRTOS task on core 0, priority 1,
//   looping every 5ms) is the watchdog owner. It is independent of the
//   MicroPython VM, so a Python hang cannot stop the check from running.
// - MicroPython FEEDS it from the UI poll (cynthia/runtime.py::_poll), which
//   runs on the sequencer callback. A sketch whose loop() spins forever stops
//   the poll, the feed stops, and the watchdog reboots the board.
// - Before rebooting it records WHY in RTC no-init memory, because
//   esp_restart() reports ESP_RST_SW (MicroPython: MP_HARD_RESET), NOT
//   WDT_RESET — so the boot guard could not otherwise tell a watchdog reboot
//   from a normal one.
// - It is DISARMED by default and only armed once the poll is actually running,
//   so boot, sketch import/compile and file transfers cannot trip it.

#pragma once

#include <stdint.h>
#include <stdbool.h>

// Recorded across a software-watchdog reboot so the boot guard can react.
#define CYNTHIA_WDT_REASON_NONE   (0u)
#define CYNTHIA_WDT_REASON_HANG   (0xC11A5A5Au)  // fed stopped: code blocked

// Default timeout. Generous: covers a slow but finite render or a heavy I2C
// burst, while still recovering a genuinely stuck board in ~10s.
#define CYNTHIA_WDT_DEFAULT_MS    (10000u)

// Boot-time timeout. Must be much longer than the default: a VALID boot can
// block for seconds (I2C prescan, display init, and sketches that sleep at
// import — e.g. a 1.5s splash). Using the runtime timeout here would reboot
// perfectly good boards. 60s still recovers a sketch that hangs in its
// top-level code, which is the case this arm covers.
#define CYNTHIA_WDT_BOOT_MS       (60000u)

void cynthia_wdt_init(void);

// Arm/disarm. Disarmed by default; arming also takes an immediate feed.
void cynthia_wdt_arm(uint32_t timeout_ms);
void cynthia_wdt_disarm(void);
bool cynthia_wdt_is_armed(void);

// Called by MicroPython (usually every UI poll) to show liveness.
void cynthia_wdt_feed(void);

// Called from the input task's loop. Reboots the board if the feed went stale.
void cynthia_wdt_check(void);

// Read-and-clear the recorded reason (call once, early in boot).
uint32_t cynthia_wdt_take_reason(void);

// Diagnostics: ms since the last feed, and feed count (both cheap).
uint32_t cynthia_wdt_since_feed_ms(void);
uint32_t cynthia_wdt_feed_count(void);
