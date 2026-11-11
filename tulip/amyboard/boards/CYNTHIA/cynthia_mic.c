// cynthia_mic.c
// I2S capture from the ICS-43434 microphone.
//
// This exists because AMY's built-in audio-input path cannot drive this board's
// mic: AMY's config carries ONE set of I2S pins (i2s_bclk/i2s_lrc/i2s_din) and
// wires the receiver onto the same peripheral as the DAC, whereas CYNTHIA's mic
// is on its own bus (WS 41 / SCK 42 / SD 44, from pins.h) with the DAC at
// 38/40/39. So capture lives here - the same split the Arduino sampler uses -
// and the recorded bytes are handed to AMY's *Python* API
// (amy.load_sample_bytes), which is a much better sampling interface than
// poking pcm_load from C.
//
// The part is 24-bit I2S in a 32-bit slot on the left channel, which is why the
// reads are 32-bit and shifted down by 16 (as the Arduino firmware does); the
// ICS-43434 drives its own data but takes the clocks from us, so we are master.

#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "pins.h"

#define MIC_RATE 44100                 // matches AMY_SAMPLE_RATE; the Arduino mic
                                       // is set up for exactly this
#define MIC_MAX_SECONDS 5
#define MIC_MAX_SAMPLES (MIC_RATE * MIC_MAX_SECONDS)

static i2s_chan_handle_t s_rx = NULL;
static int16_t *s_buf = NULL;          // PSRAM, allocated on first record
static uint32_t s_len = 0;             // samples captured so far

bool cynthia_mic_init(void) {
    if (s_rx != NULL) return true;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    esp_err_t err = i2s_new_channel(&chan_cfg, NULL, &s_rx);   // RX only
    if (err != ESP_OK) {
        fprintf(stderr, "cynthia_mic: i2s_new_channel failed (%d)\n", err);
        s_rx = NULL;
        return false;
    }
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(MIC_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                       I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = (gpio_num_t)CONFIG_MIC_SCK,
            .ws = (gpio_num_t)CONFIG_MIC_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = (gpio_num_t)CONFIG_MIC_SD,
            .invert_flags = { false, false, false },
        },
    };
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;   // the mic drives the left slot
    err = i2s_channel_init_std_mode(s_rx, &std_cfg);
    if (err != ESP_OK) {
        fprintf(stderr, "cynthia_mic: init_std_mode failed (%d)\n", err);
        i2s_del_channel(s_rx);
        s_rx = NULL;
        return false;
    }
    err = i2s_channel_enable(s_rx);
    if (err != ESP_OK) {
        fprintf(stderr, "cynthia_mic: enable failed (%d)\n", err);
        i2s_del_channel(s_rx);
        s_rx = NULL;
        return false;
    }
    fprintf(stderr, "cynthia_mic: I2S RX %d Hz, WS=%d SCK=%d SD=%d\n",
            MIC_RATE, CONFIG_MIC_WS, CONFIG_MIC_SCK, CONFIG_MIC_SD);
    return true;
}

// Capture `seconds` (clamped to MIC_MAX_SECONDS). BLOCKING: the caller's task
// sits in i2s_channel_read for the whole recording. Returns samples captured,
// or 0 on failure. The caller is expected to be a UI action, not the poll.
uint32_t cynthia_mic_record(uint32_t seconds) {
    s_len = 0;
    if (seconds == 0) seconds = 1;
    if (seconds > MIC_MAX_SECONDS) seconds = MIC_MAX_SECONDS;
    if (!cynthia_mic_init()) return 0;
    if (s_buf == NULL) {
        s_buf = (int16_t *)heap_caps_malloc(MIC_MAX_SAMPLES * sizeof(int16_t),
                                            MALLOC_CAP_SPIRAM);
        if (s_buf == NULL) {
            // 5s does not fit internal RAM; try one second before giving up.
            s_buf = (int16_t *)heap_caps_malloc(MIC_RATE * sizeof(int16_t),
                                                MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
            if (s_buf == NULL) {
                fprintf(stderr, "cynthia_mic: no buffer\n");
                return 0;
            }
            if (seconds > 1) seconds = 1;
        }
    }
    uint32_t want = seconds * MIC_RATE;
    if (want > MIC_MAX_SAMPLES) want = MIC_MAX_SAMPLES;
    int32_t block[256];
    size_t got = 0;
    while (s_len < want) {
        if (i2s_channel_read(s_rx, block, sizeof(block), &got,
                             pdMS_TO_TICKS(200)) != ESP_OK || got == 0) {
            break;
        }
        int n = (int)(got / sizeof(int32_t));
        uint32_t room = want - s_len;
        if ((uint32_t)n > room) n = (int)room;
        for (int i = 0; i < n; i++) {
            s_buf[s_len++] = (int16_t)(block[i] >> 16);   // 32-bit slot -> int16
        }
    }
    return s_len;
}

uint32_t cynthia_mic_len(void) { return s_len; }

const int16_t *cynthia_mic_ptr(void) { return s_buf; }

uint32_t cynthia_mic_max_samples(void) { return MIC_MAX_SAMPLES; }

uint32_t cynthia_mic_rate(void) { return MIC_RATE; }

void cynthia_mic_release(void) {
    if (s_buf != NULL) {
        heap_caps_free(s_buf);
        s_buf = NULL;
    }
    s_len = 0;
}
