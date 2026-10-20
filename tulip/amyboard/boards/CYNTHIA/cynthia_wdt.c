// cynthia_wdt.c — see cynthia_wdt.h for the rationale.
//
// Ownership note: this runs in the CYNTHIA input task, which is a separate
// FreeRTOS task on core 0 that never calls into MicroPython. That is the whole
// point — the check must survive a hung VM.

#include "cynthia_wdt.h"

#include <stdio.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_attr.h"

// amy_global.total_blocks: AMY's audio-block counter (see amy/src/api.c
// amy_sysclock). Advances only while audio actually renders, so it is a second,
// independent liveness signal to the Python feed.
#include "amy.h"

// RTC no-init memory survives a software reset (esp_restart) AND a watchdog
// reset, but not a power cycle. Sufficient here: a power cycle is a person
// deliberately power-cycling, which resets intent anyway.
static RTC_NOINIT_ATTR uint32_t s_wdt_reason;
static RTC_NOINIT_ATTR uint32_t s_wdt_reason_magic;

#define CYNTHIA_WDT_MAGIC (0x57445431u)  // "WDT1"

static inline uint32_t now_ms(void) {
    return (uint32_t)(esp_timer_get_time() / 1000ULL);
}

static volatile bool s_armed = false;
static volatile uint32_t s_timeout_ms = CYNTHIA_WDT_DEFAULT_MS;
static volatile uint32_t s_last_feed_ms = 0;
static volatile uint32_t s_feed_count = 0;
// Latch so the "about to reboot" message prints exactly once.
static volatile bool s_reported = false;
// Last observed AMY audio-block count, for the audio-liveness check.
static volatile uint32_t s_last_audio_blocks = 0;
static volatile uint32_t s_audio_ms = 0;

void cynthia_wdt_init(void) {
    // Clear any stale reason from a previous run unless this boot itself came
    // from a watchdog reset (the magic says so). Keeping it across the reset is
    // the entire mechanism by which the boot guard learns what happened.
    if (s_wdt_reason_magic != CYNTHIA_WDT_MAGIC) {
        s_wdt_reason = CYNTHIA_WDT_REASON_NONE;
        s_wdt_reason_magic = CYNTHIA_WDT_MAGIC;
    }
    s_armed = false;
    s_timeout_ms = CYNTHIA_WDT_DEFAULT_MS;
    s_last_feed_ms = now_ms();
    s_feed_count = 0;
    s_reported = false;
    fprintf(stderr, "cynthia_wdt: init (disarmed, %ums, reason=0x%08x)\n",
            (unsigned)s_timeout_ms, (unsigned)s_wdt_reason);
    fflush(stderr);
}

void cynthia_wdt_arm(uint32_t timeout_ms) {
    s_timeout_ms = timeout_ms ? timeout_ms : CYNTHIA_WDT_DEFAULT_MS;
    s_last_feed_ms = now_ms();
    s_armed = true;
    s_reported = false;
    fprintf(stderr, "cynthia_wdt: ARMED (%ums)\n", (unsigned)s_timeout_ms);
    fflush(stderr);
}

void cynthia_wdt_disarm(void) {
    s_armed = false;
    fprintf(stderr, "cynthia_wdt: disarmed\n");
    fflush(stderr);
}

bool cynthia_wdt_is_armed(void) {
    return s_armed;
}

void cynthia_wdt_feed(void) {
    s_last_feed_ms = now_ms();
    s_feed_count++;
    s_reported = false;
}

uint32_t cynthia_wdt_since_feed_ms(void) {
    return now_ms() - s_last_feed_ms;
}

uint32_t cynthia_wdt_feed_count(void) {
    return s_feed_count;
}

void cynthia_wdt_check(void) {
    if (!s_armed) {
        // Keep the baseline fresh while disarmed, so arming + the first check
        // can't see a huge stale delta and reboot immediately.
        s_last_feed_ms = now_ms();
        s_last_audio_blocks = amy_global.total_blocks;
        return;
    }

    // Audio liveness, checked separately from the Python feed. amy_sysclock()
    // advances once per AMY_BLOCK_SIZE of audio actually rendered, driven by the
    // AMY fill-buffer task — which runs at ESP_TASK_PRIO_MAX-1, ABOVE this task.
    // So a hang inside AMY would starve this check entirely and no watchdog in
    // this task could report it. Watching total_blocks lets us at least name the
    // failure: Python fed us recently but audio stopped => the audio path died,
    // which is a different bug from a hung sketch.
    uint32_t blocks = amy_global.total_blocks;
    if (blocks != s_last_audio_blocks) {
        s_last_audio_blocks = blocks;
        s_audio_ms = now_ms();
    }

    uint32_t elapsed = cynthia_wdt_since_feed_ms();
    if (elapsed < s_timeout_ms) {
        return;
    }
    // Feed went stale. Before blaming Python, is audio also dead? If audio is
    // still advancing, this is a genuine Python-side hang (the case the whole
    // mechanism exists for).
    bool audio_alive = (now_ms() - s_audio_ms) < s_timeout_ms;
    if (!s_reported) {
        s_reported = true;
        fprintf(stderr,
                "cynthia_wdt: no feed for %ums (limit %ums), audio %s — rebooting\n",
                (unsigned)elapsed, (unsigned)s_timeout_ms,
                audio_alive ? "ALIVE (python hang)" : "DEAD (audio path stalled)");
        fflush(stderr);
    }
    // Record the reason where the next boot can read it, then reboot.
    s_wdt_reason = CYNTHIA_WDT_REASON_HANG;
    s_wdt_reason_magic = CYNTHIA_WDT_MAGIC;
    esp_restart();
}

uint32_t cynthia_wdt_take_reason(void) {
    if (s_wdt_reason_magic != CYNTHIA_WDT_MAGIC) {
        return CYNTHIA_WDT_REASON_NONE;
    }
    uint32_t r = s_wdt_reason;
    s_wdt_reason = CYNTHIA_WDT_REASON_NONE;   // read-and-clear
    return r;
}
