#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Font indices corresponding to Cynthia's 7 U8G2 fonts
enum {
    CYNTHIA_FONT_5X7 = 0,           // u8g2_font_5x7_tr (status bar, headers, labels)
    CYNTHIA_FONT_4X6 = 1,           // u8g2_font_4x6_tr (micro labels, slider annotations)
    CYNTHIA_FONT_7X13 = 2,          // u8g2_font_7x13_tr (sampler / status values)
    CYNTHIA_FONT_TENFATGUYS = 3,    // u8g2_font_tenfatguys_tf (patch names, bold titles)
    CYNTHIA_FONT_TENTHINNERGUYS = 4,// u8g2_font_tenthinnerguys_tf (instrument grid menu labels)
    CYNTHIA_FONT_RESOLEDBOLD = 5,   // u8g2_font_resoledbold_tr (synth name in status bar header)
    CYNTHIA_FONT_SPLEEN12X24 = 6,   // u8g2_font_spleen12x24_mu (large readout numbers)
    CYNTHIA_FONT_COUNT = 7
};

// Return font data pointer by index (or NULL if invalid)
const uint8_t *cynthia_ui_get_font(uint8_t font_idx);

// Measure string width in pixels
int16_t cynthia_ui_str_width(uint8_t font_idx, const char *str);

// Draw string directly into 1024-byte MONO_VLSB buffer (128x64)
// color: 1 = set pixels, 0 = clear pixels, 2 = invert pixels (XOR)
int16_t cynthia_ui_draw_str(uint8_t *buf, uint8_t font_idx, int16_t x, int16_t y, const char *str, uint8_t color);

// Read and smooth battery voltage from ESP32 ADC on BATT_LVL pin (GPIO 1)
float cynthia_ui_battery_voltage(void);

#ifdef __cplusplus
}
#endif
