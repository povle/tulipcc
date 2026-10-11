// cynthia_ui.c
// Implementation of U8G2 font rendering and battery ADC for CYNTHIA.
// Renders directly into SH1106 1024-byte framebuf (128x64 MONO_VLSB format).

#include <string.h>
#include <stdlib.h>
#include "cynthia_ui.h"
#include "cynthia_fonts.h"
#include "pins.h"

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "driver/adc.h"
#endif

// Fonts array
static const uint8_t *s_cynthia_fonts[CYNTHIA_FONT_COUNT] = {
    u8g2_font_5x7_tr,
    u8g2_font_4x6_tr,
    u8g2_font_7x13_tr,
    u8g2_font_tenfatguys_tf,
    u8g2_font_tenthinnerguys_tf,
    u8g2_font_resoledbold_tr,
    u8g2_font_spleen12x24_mu
};

const uint8_t *cynthia_ui_get_font(uint8_t font_idx) {
    if (font_idx >= CYNTHIA_FONT_COUNT) return NULL;
    return s_cynthia_fonts[font_idx];
}

// Minimal U8G2 font decoding definitions
typedef struct {
    uint8_t bits_per_0;
    uint8_t bits_per_1;
    uint8_t bits_per_char_width;
    uint8_t bits_per_char_height;
    uint8_t bits_per_char_x;
    uint8_t bits_per_char_y;
    uint8_t bits_per_delta_x;
    int8_t max_char_width;
    int8_t max_char_height;
    int8_t x_offset;
    int8_t y_offset;
    int8_t ascent_A;
    int8_t descent_g;
    uint16_t start_pos_upper_A;
    uint16_t start_pos_lower_a;
    uint16_t start_pos_unicode;
} cynthia_font_info_t;

typedef struct {
    const uint8_t *decode_ptr;
    uint8_t decode_bit_pos;
    uint8_t glyph_width;
    uint8_t glyph_height;
    int16_t target_x;
    int16_t target_y;
    uint8_t x;
    uint8_t y;
} cynthia_font_decode_t;

static inline uint8_t u8x8_pgm_read(const uint8_t *adr) {
    return *adr;
}

static uint16_t font_get_word(const uint8_t *font, uint8_t offset) {
    uint16_t pos;
    font += offset;
    pos = u8x8_pgm_read(font++);
    pos <<= 8;
    pos += u8x8_pgm_read(font);
    return pos;
}

static void font_read_info(cynthia_font_info_t *info, const uint8_t *font) {
    info->bits_per_0 = u8x8_pgm_read(font + 2);
    info->bits_per_1 = u8x8_pgm_read(font + 3);
    info->bits_per_char_width = u8x8_pgm_read(font + 4);
    info->bits_per_char_height = u8x8_pgm_read(font + 5);
    info->bits_per_char_x = u8x8_pgm_read(font + 6);
    info->bits_per_char_y = u8x8_pgm_read(font + 7);
    info->bits_per_delta_x = u8x8_pgm_read(font + 8);
    info->max_char_width = (int8_t)u8x8_pgm_read(font + 9);
    info->max_char_height = (int8_t)u8x8_pgm_read(font + 10);
    info->x_offset = (int8_t)u8x8_pgm_read(font + 11);
    info->y_offset = (int8_t)u8x8_pgm_read(font + 12);
    info->ascent_A = (int8_t)u8x8_pgm_read(font + 13);
    info->descent_g = (int8_t)u8x8_pgm_read(font + 14);
    info->start_pos_upper_A = font_get_word(font, 17);
    info->start_pos_lower_a = font_get_word(font, 19);
    info->start_pos_unicode = font_get_word(font, 21);
}

static uint8_t font_decode_get_unsigned_bits(cynthia_font_decode_t *f, uint8_t cnt) {
    uint8_t val;
    uint8_t bit_pos = f->decode_bit_pos;
    uint8_t bit_pos_plus_cnt = bit_pos + cnt;

    val = u8x8_pgm_read(f->decode_ptr);
    val >>= bit_pos;
    if (bit_pos_plus_cnt >= 8) {
        f->decode_ptr++;
        bit_pos_plus_cnt -= 8;
        f->decode_bit_pos = bit_pos_plus_cnt;
        if (bit_pos_plus_cnt != 0) {
            val |= u8x8_pgm_read(f->decode_ptr) << (8 - bit_pos);
        }
    } else {
        f->decode_bit_pos = bit_pos_plus_cnt;
    }
    val &= (1 << cnt) - 1;
    return val;
}

static int8_t font_decode_get_signed_bits(cynthia_font_decode_t *f, uint8_t cnt) {
    int8_t v = (int8_t)font_decode_get_unsigned_bits(f, cnt);
    int8_t d = 1 << (cnt - 1);
    v -= d;
    return v;
}

static const uint8_t *font_get_glyph_data(const uint8_t *font, const cynthia_font_info_t *info, uint16_t encoding) {
    const uint8_t *p = font + 23;
    if (encoding <= 255) {
        if (encoding >= 'a') {
            p += info->start_pos_lower_a;
        } else if (encoding >= 'A') {
            p += info->start_pos_upper_A;
        }
        for (;;) {
            if (u8x8_pgm_read(p + 1) == 0) break;
            if (u8x8_pgm_read(p) == encoding) {
                return p + 2;
            }
            p += u8x8_pgm_read(p + 1);
        }
    } else {
        const uint8_t *table = font + 23 + info->start_pos_unicode;
        uint16_t e;
        do {
            p += font_get_word(table, 0);
            e = font_get_word(table, 2);
            table += 4;
        } while (e < encoding);
        for (;;) {
            e = u8x8_pgm_read(p) << 8;
            e |= u8x8_pgm_read(p + 1);
            if (e == 0) break;
            if (e == encoding) {
                return p + 3;
            }
            p += u8x8_pgm_read(p + 2);
        }
    }
    return NULL;
}

static void font_setup_decode(cynthia_font_decode_t *decode, const cynthia_font_info_t *info, const uint8_t *glyph_data) {
    decode->decode_ptr = glyph_data;
    decode->decode_bit_pos = 0;
    decode->glyph_width = font_decode_get_unsigned_bits(decode, info->bits_per_char_width);
    decode->glyph_height = font_decode_get_unsigned_bits(decode, info->bits_per_char_height);
}

int16_t cynthia_ui_str_width(uint8_t font_idx, const char *str) {
    if (!str || !*str) return 0;
    const uint8_t *font = cynthia_ui_get_font(font_idx);
    if (!font) return 0;

    cynthia_font_info_t info;
    font_read_info(&info, font);

    int16_t w = 0;
    int16_t dx = 0;
    int8_t last_glyph_width = 0;
    int8_t last_x_offset = 0;

    while (*str) {
        uint16_t c = (uint8_t)*str++;
        const uint8_t *glyph_data = font_get_glyph_data(font, &info, c);
        if (glyph_data) {
            cynthia_font_decode_t decode;
            font_setup_decode(&decode, &info, glyph_data);
            last_x_offset = font_decode_get_signed_bits(&decode, info.bits_per_char_x);
            font_decode_get_signed_bits(&decode, info.bits_per_char_y);
            dx = font_decode_get_signed_bits(&decode, info.bits_per_delta_x);
            last_glyph_width = decode.glyph_width;
            w += dx;
        }
    }
    // U8G2 string width adjustment for the last glyph:
    // w -= dx; w += last_glyph_width + last_x_offset;
    if (last_glyph_width != 0) {
        w -= dx;
        w += last_glyph_width + last_x_offset;
    }
    return w;
}

// Set, clear, or invert pixel in MONO_VLSB buffer (128x64)
// byte offset = (y / 8) * 128 + x, bit = (y % 8)
static inline void draw_pixel_vlsb(uint8_t *buf, int16_t x, int16_t y, uint8_t color) {
    if (x < 0 || x >= 128 || y < 0 || y >= 64) return;
    uint16_t idx = (y >> 3) * 128 + x;
    uint8_t mask = 1 << (y & 7);
    if (color == 1) {
        buf[idx] |= mask;
    } else if (color == 0) {
        buf[idx] &= ~mask;
    } else { // XOR / invert
        buf[idx] ^= mask;
    }
}

static inline void draw_h_line_vlsb(uint8_t *buf, int16_t x, int16_t y, int16_t len, uint8_t color) {
    if (y < 0 || y >= 64 || len <= 0) return;
    int16_t x1 = x;
    int16_t x2 = x + len - 1;
    if (x2 < 0 || x1 >= 128) return;
    if (x1 < 0) x1 = 0;
    if (x2 >= 128) x2 = 127;

    uint16_t base_idx = (y >> 3) * 128;
    uint8_t mask = 1 << (y & 7);

    if (color == 1) {
        for (int16_t cx = x1; cx <= x2; cx++) buf[base_idx + cx] |= mask;
    } else if (color == 0) {
        uint8_t inv_mask = ~mask;
        for (int16_t cx = x1; cx <= x2; cx++) buf[base_idx + cx] &= inv_mask;
    } else {
        for (int16_t cx = x1; cx <= x2; cx++) buf[base_idx + cx] ^= mask;
    }
}

static void font_decode_len_vlsb(uint8_t *buf, cynthia_font_decode_t *decode, uint8_t len, uint8_t is_foreground, uint8_t color) {
    uint8_t cnt = len;
    uint8_t lx = decode->x;
    uint8_t ly = decode->y;

    for (;;) {
        uint8_t rem = decode->glyph_width - lx;
        uint8_t current = (cnt < rem) ? cnt : rem;

        if (current > 0 && is_foreground) {
            int16_t x = decode->target_x + lx;
            int16_t y = decode->target_y + ly;
            draw_h_line_vlsb(buf, x, y, current, color);
        }

        if (cnt < rem) break;
        cnt -= rem;
        lx = 0;
        ly++;
    }
    lx += cnt;
    decode->x = lx;
    decode->y = ly;
}

static int16_t draw_glyph_vlsb(uint8_t *buf, const uint8_t *font, const cynthia_font_info_t *info, int16_t x, int16_t y, uint16_t encoding, uint8_t color) {
    const uint8_t *glyph_data = font_get_glyph_data(font, info, encoding);
    if (!glyph_data) return 0;

    cynthia_font_decode_t decode;
    font_setup_decode(&decode, info, glyph_data);
    int8_t h = decode.glyph_height;

    int8_t gx = font_decode_get_signed_bits(&decode, info->bits_per_char_x);
    int8_t gy = font_decode_get_signed_bits(&decode, info->bits_per_char_y);
    int8_t delta_x = font_decode_get_signed_bits(&decode, info->bits_per_delta_x);

    if (decode.glyph_width > 0) {
        decode.target_x = x + gx;
        decode.target_y = y - (h + gy);
        decode.x = 0;
        decode.y = 0;

        for (;;) {
            uint8_t a = font_decode_get_unsigned_bits(&decode, info->bits_per_0);
            uint8_t b = font_decode_get_unsigned_bits(&decode, info->bits_per_1);
            do {
                font_decode_len_vlsb(buf, &decode, a, 0, color);
                font_decode_len_vlsb(buf, &decode, b, 1, color);
            } while (font_decode_get_unsigned_bits(&decode, 1) != 0);

            if (decode.y >= h) break;
        }
    }
    return delta_x;
}

int16_t cynthia_ui_draw_str(uint8_t *buf, uint8_t font_idx, int16_t x, int16_t y, const char *str, uint8_t color) {
    if (!buf || !str) return 0;
    const uint8_t *font = cynthia_ui_get_font(font_idx);
    if (!font) return 0;

    cynthia_font_info_t info;
    font_read_info(&info, font);

    int16_t start_x = x;
    while (*str) {
        uint16_t c = (uint8_t)*str++;
        int16_t delta = draw_glyph_vlsb(buf, font, &info, x, y, c, color);
        x += delta;
    }
    return x - start_x;
}

// Battery ADC
static float s_smoothed_battery_v = -1.0f;
static int64_t s_last_battery_read_us = 0;

float cynthia_ui_battery_voltage(void) {
#ifdef ESP_PLATFORM
    int64_t now_us = esp_timer_get_time();
    // Throttle reads to ~10Hz
    if (s_smoothed_battery_v >= 0.0f && (now_us - s_last_battery_read_us < 100000)) {
        return s_smoothed_battery_v;
    }
    s_last_battery_read_us = now_us;

    // GPIO 1 on ESP32-S3 is ADC1 Channel 0
    static bool adc_inited = false;
    if (!adc_inited) {
        adc1_config_width(ADC_WIDTH_BIT_12);
        adc1_config_channel_atten(ADC1_CHANNEL_0, ADC_ATTEN_DB_11);
        adc_inited = true;
    }

    int raw = adc1_get_raw(ADC1_CHANNEL_0);
    // BATT_LVL voltage divider from Hardware.cpp:
    // raw (0-4095) across 0-3.3V attenuation (11dB gives ~2600mV range or 3.3V full scale)
    // Hardware.cpp: float current_voltage = (float)raw / 500.0f * BATTERY_CALIBRATION_FACTOR (1.03f)
    float raw_mv = (float)raw * (3300.0f / 4095.0f);
    // The hardware divider divides by ~2 (e.g. 500 ohms / 1000 ohms or similar divider)
    // (float)raw / 500.0f * 1.03f in Arduino analogReadMilliVolts:
    // In Arduino ESP32, analogReadMilliVolts() returns calibrated mV at the pin (0..3100mV).
    // pin_mv / 500.0f = pin_mv * 2 / 1000.0f = battery_volts!
    float batt_v = (raw_mv * 2.0f / 1000.0f) * 1.03f;

    if (s_smoothed_battery_v < 0.0f) {
        s_smoothed_battery_v = batt_v;
    } else {
        s_smoothed_battery_v = (batt_v * 0.1f) + (s_smoothed_battery_v * 0.9f);
    }
    return s_smoothed_battery_v;
#else
    return 4.12f;
#endif
}
