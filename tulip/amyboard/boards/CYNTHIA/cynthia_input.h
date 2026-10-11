#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CYNTHIA_NUM_POTS 16
#define CYNTHIA_NUM_KEYS 28
#define CYNTHIA_NUM_BUTTONS 5

void cynthia_input_init(void);
void cynthia_input_task(void *pvParameters);

// Python/C getters for cached state
void cynthia_get_knobs(float *out_knobs, uint8_t count);
void cynthia_get_joystick(float *out_x, float *out_y, bool *out_btn);
uint8_t cynthia_get_buttons(void);
bool cynthia_get_button_event(uint8_t *btn_idx, uint8_t *kind);
int8_t cynthia_get_octave_shift(void);
void cynthia_set_octave_shift(int8_t shift);
void cynthia_get_debug_status(uint32_t *out_loops, uint32_t *out_ads_reads, uint8_t *out_ads_present, uint8_t *out_ads_port);

#ifdef __cplusplus
}
#endif
