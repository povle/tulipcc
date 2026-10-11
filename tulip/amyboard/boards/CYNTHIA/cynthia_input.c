#ifdef ESP_PLATFORM
#include "cynthia_input.h"
#include "pins.h"

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "esp_timer.h"

#include "amy.h"
#include "amy_midi.h"

#define TAG "cynthia_input"

#define MCP_ADDR_0 0x20
#define MCP_ADDR_1 0x21
#define ADS_ADDR   0x48

// MCP23017 registers
#define MCP_IODIRA   0x00
#define MCP_IODIRB   0x01
#define MCP_IPOLA    0x02
#define MCP_GPINTENA 0x04
#define MCP_DEFVALA  0x06
#define MCP_INTCONA  0x08
#define MCP_IOCON    0x0A
#define MCP_GPPUA    0x0C
#define MCP_GPPUB    0x0D
#define MCP_INTFA    0x0E
#define MCP_INTCAPA  0x10
#define MCP_GPIOA    0x12
#define MCP_GPIOB    0x13

// ADS1115 registers & masks
#define ADS_REG_CONVERT 0x00
#define ADS_REG_CONFIG  0x01
#define ADS_OS_SINGLE   0x8000
// ADS1115 Single-ended MUX configurations (against GND)
// Hardware.h mapping:
// ADS1115_JOY_Y_CHANNEL   ADC_A_1 = AIN0 -> 0x4000
// ADS1115_MUX_SIG_CHANNEL ADC_A_2 = AIN2 -> 0x6000
// ADS1115_JOY_X_CHANNEL   ADC_B_1 = AIN1 -> 0x5000
#define ADS_MUX_AIN0    0x4000 // Joystick Y (ADC_A_1 / AIN0)
#define ADS_MUX_AIN1    0x5000 // Joystick X (ADC_B_1 / AIN1)
#define ADS_MUX_AIN2    0x6000 // Pot MUX signal (ADC_A_2 / AIN2)
#define ADS_MUX_AIN3    0x7000
#define ADS_PGA_4_096V  0x0200
#define ADS_MODE_SINGLE 0x0100
#define ADS_DR_860SPS   0x00E0
#define ADS_CQUE_NONE   0x0003

// 28 chromatic keys: notes 45..72 (A2..C5)
typedef struct {
    uint8_t mcp;
    uint8_t pin;
} key_pin_t;

static const key_pin_t key_pins[CYNTHIA_NUM_KEYS] = {
    {MCP_ADDR_0, 8},  {MCP_ADDR_0, 9},  {MCP_ADDR_0, 10}, {MCP_ADDR_0, 12}, {MCP_ADDR_0, 11}, // 45..49: A2..C#3
    {MCP_ADDR_0, 14}, {MCP_ADDR_0, 13}, {MCP_ADDR_0, 0},  {MCP_ADDR_0, 2},  {MCP_ADDR_0, 15}, // 50..54: D3..F#3
    {MCP_ADDR_1, 8},  {MCP_ADDR_0, 1},  {MCP_ADDR_1, 10}, {MCP_ADDR_0, 3},  {MCP_ADDR_1, 12}, // 55..59: G3..B3
    {MCP_ADDR_1, 14}, {MCP_ADDR_1, 9},  {MCP_ADDR_1, 0},  {MCP_ADDR_1, 11}, {MCP_ADDR_1, 2},  // 60..64: C4..E4
    {MCP_ADDR_1, 4},  {MCP_ADDR_1, 13}, {MCP_ADDR_1, 6},  {MCP_ADDR_1, 15}, {MCP_ADDR_1, 3},  // 65..69: F4..A4
    {MCP_ADDR_1, 1},  {MCP_ADDR_1, 5},  {MCP_ADDR_1, 7}                                       // 70..72: A#4..C5
};

#define BASE_NOTE 45

// 4 MCP buttons on 0x20 (PA4..PA7)
// 0: Octave Down (PA5), 1: Octave Up (PA4), 2: Patch Prev (PA7), 3: Patch Next (PA6)
static const key_pin_t btn_pins[4] = {
    {MCP_ADDR_0, 5}, // Octave Down (PA5)
    {MCP_ADDR_0, 4}, // Octave Up (PA4)
    {MCP_ADDR_0, 7}, // Patch Prev (PA7, BTN2)
    {MCP_ADDR_0, 6}  // Patch Next (PA6, BTN3)
};

#define POT_EMA_ALPHA 0.3f
#define JOY_EMA_ALPHA 0.8f
#define DEBOUNCE_MS 20
#define LONG_PRESS_MS 800

static float s_knob_values[CYNTHIA_NUM_POTS] = {0.5f};
static bool s_first_pot_read[CYNTHIA_NUM_POTS] = {false};
static float s_joy_x = 0.5f;
static float s_joy_y = 0.5f;
static bool s_first_joy_x_read = false;
static bool s_first_joy_y_read = false;
static bool s_joy_btn = false;
static bool s_joy_btn_last = false;
static uint32_t s_joy_debounce_time = 0;
static uint8_t s_btn_state = 0; // bitmask of buttons currently pressed
static int8_t s_octave_shift = 0;
static uint32_t s_task_loop_count = 0;
static uint32_t s_ads_read_count = 0;

static bool s_key_state[CYNTHIA_NUM_KEYS] = {false};
static uint32_t s_key_last_change[CYNTHIA_NUM_KEYS] = {0};
static bool s_mcp_btn_state[4] = {false};
static uint32_t s_btn_press_time[5] = {0};
static bool s_btn_long_fired[5] = {false};

// Button event ring buffer
#define BTN_EVT_QUEUE_SIZE 16
typedef struct {
    uint8_t btn;
    uint8_t kind; // 0 = short, 1 = long
} btn_event_t;

static btn_event_t s_evt_queue[BTN_EVT_QUEUE_SIZE];
static uint8_t s_evt_head = 0;
static uint8_t s_evt_tail = 0;

static bool s_mcp0_present = false;
static bool s_mcp1_present = false;
static bool s_ads_present = false;
static i2c_port_t s_ads_port = I2C_NUM_1;

static void queue_btn_event(uint8_t btn, uint8_t kind) {
    uint8_t next = (s_evt_head + 1) % BTN_EVT_QUEUE_SIZE;
    if (next != s_evt_tail) {
        s_evt_queue[s_evt_head].btn = btn;
        s_evt_queue[s_evt_head].kind = kind;
        s_evt_head = next;
    }
}

static esp_err_t i2c_write_reg16(i2c_port_t port, uint8_t addr, uint8_t reg, uint16_t val) {
    uint8_t data[3] = { reg, (uint8_t)(val & 0xFF), (uint8_t)(val >> 8) };
    return i2c_master_write_to_device(port, addr, data, 3, pdMS_TO_TICKS(10));
}

static esp_err_t i2c_read_reg16(i2c_port_t port, uint8_t addr, uint8_t reg, uint16_t *val) {
    uint8_t data[2] = {0, 0};
    esp_err_t ret = i2c_master_write_read_device(port, addr, &reg, 1, data, 2, pdMS_TO_TICKS(10));
    if (ret == ESP_OK) {
        *val = data[0] | ((uint16_t)data[1] << 8);
    }
    return ret;
}

static esp_err_t ads_write_config_port(i2c_port_t port, uint16_t config) {
    uint8_t data[3] = { ADS_REG_CONFIG, (uint8_t)(config >> 8), (uint8_t)(config & 0xFF) };
    return i2c_master_write_to_device(port, ADS_ADDR, data, 3, pdMS_TO_TICKS(10));
}

static esp_err_t ads_read_conversion_port(i2c_port_t port, int16_t *val) {
    uint8_t reg = ADS_REG_CONVERT;
    uint8_t data[2] = {0, 0};
    esp_err_t ret = i2c_master_write_read_device(port, ADS_ADDR, &reg, 1, data, 2, pdMS_TO_TICKS(10));
    if (ret == ESP_OK) {
        *val = (int16_t)(((uint16_t)data[0] << 8) | data[1]);
    }
    return ret;
}

static void mcp_init(i2c_port_t port, uint8_t addr) {
    // All 16 pins inputs (0xFFFF)
    i2c_write_reg16(port, addr, MCP_IODIRA, 0xFFFF);
    // Enable 100k pull-ups on all pins
    i2c_write_reg16(port, addr, MCP_GPPUA, 0xFFFF);
    // Set open-drain mirror interrupt (IOCON)
    uint8_t iocon_data[3] = { MCP_IOCON, 0x40, 0x40 };
    i2c_master_write_to_device(port, addr, iocon_data, 3, pdMS_TO_TICKS(10));
    // Clear initial interrupt
    uint16_t dummy;
    i2c_read_reg16(port, addr, MCP_INTCAPA, &dummy);
}

void cynthia_input_init(void) {
    fprintf(stderr, "cynthia_input_init: configuring I2C buses...\n"); fflush(stderr);

    // 0a. Configure I2C Master on Bus 0 (pins 17 SDA, 16 SCL)
    i2c_config_t i2c0_conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = (gpio_num_t)I2C_MASTER_SDA,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_io_num = (gpio_num_t)I2C_MASTER_SCL,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 400000,
    };
    i2c_param_config(I2C_NUM_0, &i2c0_conf);
    i2c_driver_install(I2C_NUM_0, I2C_MODE_MASTER, 0, 0, 0);

    // 0b. Configure I2C Master on Bus 1 (pins 5 SDA, 4 SCL)
    i2c_config_t i2c1_conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = (gpio_num_t)I2C_DISPLAY_SDA,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_io_num = (gpio_num_t)I2C_DISPLAY_SCL,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 400000,
    };
    i2c_param_config(I2C_NUM_1, &i2c1_conf);
    i2c_driver_install(I2C_NUM_1, I2C_MODE_MASTER, 0, 0, 0);

    // 1. Configure MUX GPIOs
    gpio_config_t mux_conf = {
        .pin_bit_mask = (1ULL << DB2_A) | (1ULL << DB2_B) | (1ULL << DB2_C) | (1ULL << DB2_D),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&mux_conf);

    // 2. Configure Joystick Button GPIO (IO_G_1 = DB1_G = GPIO 10)
    // C++ Hardware.cpp: pinMode(BTN_JOYSTICK, INPUT_PULLDOWN), HIGH = pressed
    gpio_config_t joy_btn_conf = {
        .pin_bit_mask = (1ULL << DB1_G),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_ENABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&joy_btn_conf);

    // 3. Probe & initialize MCP23017s on Bus 0 (or Bus 1)
    uint16_t test_val;
    if (i2c_read_reg16(I2C_NUM_0, MCP_ADDR_0, MCP_GPIOA, &test_val) == ESP_OK) {
        mcp_init(I2C_NUM_0, MCP_ADDR_0);
        s_mcp0_present = true;
        fprintf(stderr, "cynthia_input: MCP23017 at 0x20 initialized on I2C0\n"); fflush(stderr);
    } else if (i2c_read_reg16(I2C_NUM_1, MCP_ADDR_0, MCP_GPIOA, &test_val) == ESP_OK) {
        mcp_init(I2C_NUM_1, MCP_ADDR_0);
        s_mcp0_present = true;
        fprintf(stderr, "cynthia_input: MCP23017 at 0x20 initialized on I2C1\n"); fflush(stderr);
    } else {
        fprintf(stderr, "cynthia_input: MCP23017 at 0x20 NOT detected\n"); fflush(stderr);
    }

    if (i2c_read_reg16(I2C_NUM_0, MCP_ADDR_1, MCP_GPIOA, &test_val) == ESP_OK) {
        mcp_init(I2C_NUM_0, MCP_ADDR_1);
        s_mcp1_present = true;
        fprintf(stderr, "cynthia_input: MCP23017 at 0x21 initialized on I2C0\n"); fflush(stderr);
    } else if (i2c_read_reg16(I2C_NUM_1, MCP_ADDR_1, MCP_GPIOA, &test_val) == ESP_OK) {
        mcp_init(I2C_NUM_1, MCP_ADDR_1);
        s_mcp1_present = true;
        fprintf(stderr, "cynthia_input: MCP23017 at 0x21 initialized on I2C1\n"); fflush(stderr);
    } else {
        fprintf(stderr, "cynthia_input: MCP23017 at 0x21 NOT detected\n"); fflush(stderr);
    }

    // 4. Probe ADS1115 on Bus 1 (display bus) then Bus 0
    uint16_t ads_cfg = ADS_OS_SINGLE | ADS_MUX_AIN0 | ADS_PGA_4_096V | ADS_MODE_SINGLE | ADS_DR_860SPS | ADS_CQUE_NONE;
    if (ads_write_config_port(I2C_NUM_1, ads_cfg) == ESP_OK) {
        s_ads_port = I2C_NUM_1;
        s_ads_present = true;
        fprintf(stderr, "cynthia_input: ADS1115 at 0x48 initialized on I2C1 (GPIO 5/4)\n"); fflush(stderr);
    } else if (ads_write_config_port(I2C_NUM_0, ads_cfg) == ESP_OK) {
        s_ads_port = I2C_NUM_0;
        s_ads_present = true;
        fprintf(stderr, "cynthia_input: ADS1115 at 0x48 initialized on I2C0 (GPIO 17/16)\n"); fflush(stderr);
    } else {
        fprintf(stderr, "cynthia_input: ADS1115 at 0x48 NOT detected on either I2C bus\n"); fflush(stderr);
    }
}

static inline void set_mux_channel(uint8_t ch) {
    gpio_set_level((gpio_num_t)DB2_A, (ch >> 0) & 1);
    gpio_set_level((gpio_num_t)DB2_B, (ch >> 1) & 1);
    gpio_set_level((gpio_num_t)DB2_C, (ch >> 2) & 1);
    gpio_set_level((gpio_num_t)DB2_D, (ch >> 3) & 1);
}

static void scan_keys_and_buttons(uint32_t now_ms) {
    uint16_t gpio0 = 0xFFFF, gpio1 = 0xFFFF;
    if (s_mcp0_present) {
        i2c_read_reg16(I2C_NUM_0, MCP_ADDR_0, MCP_GPIOA, &gpio0);
    }
    if (s_mcp1_present) {
        i2c_read_reg16(I2C_NUM_0, MCP_ADDR_1, MCP_GPIOA, &gpio1);
    }

    // 1. Scan 28 chromatic keys
    for (uint8_t i = 0; i < CYNTHIA_NUM_KEYS; i++) {
        uint16_t g = (key_pins[i].mcp == MCP_ADDR_0) ? gpio0 : gpio1;
        bool pressed = ((g & (1 << key_pins[i].pin)) == 0); // Active low

        if (pressed != s_key_state[i]) {
            if (now_ms - s_key_last_change[i] >= DEBOUNCE_MS) {
                s_key_last_change[i] = now_ms;
                s_key_state[i] = pressed;

                int note = BASE_NOTE + i + (s_octave_shift * 12);
                if (note < 0) note = 0;
                if (note > 127) note = 127;

                uint8_t midi_msg[3];
                if (pressed) {
                    midi_msg[0] = 0x90; // Note On ch 0
                    midi_msg[1] = (uint8_t)note;
                    midi_msg[2] = 100;  // Fixed velocity
                } else {
                    midi_msg[0] = 0x80; // Note Off ch 0
                    midi_msg[1] = (uint8_t)note;
                    midi_msg[2] = 0;
                }
                midi_local(midi_msg, 3);
            }
        }
    }

    // 2. Scan 4 MCP Buttons (0x20 PA4..PA7)
    uint8_t btn_mask = 0;
    for (uint8_t b = 0; b < 4; b++) {
        bool pressed = ((gpio0 & (1 << btn_pins[b].pin)) == 0); // Active low
        if (pressed) btn_mask |= (1 << b);

        if (pressed != s_mcp_btn_state[b]) {
            s_mcp_btn_state[b] = pressed;
            if (pressed) {
                s_btn_press_time[b] = now_ms;
                s_btn_long_fired[b] = false;
            } else {
                if (!s_btn_long_fired[b]) {
                    queue_btn_event(b, 0); // Short press
                }
            }
        } else if (pressed && !s_btn_long_fired[b]) {
            if (now_ms - s_btn_press_time[b] >= LONG_PRESS_MS) {
                s_btn_long_fired[b] = true;
                queue_btn_event(b, 1); // Long press
            }
        }
    }

    // 3. Scan Joystick Button with debounce and long-press detection
    {
        bool joy_pressed = (gpio_get_level((gpio_num_t)DB1_G) == 1); // PULLDOWN: HIGH = pressed
        uint8_t b = 4; // Button index 4 = joystick button
        static uint32_t s_joy_press_time = 0;
        static bool s_joy_long_fired = false;

        if (joy_pressed != s_joy_btn_last) {
            if (now_ms - s_joy_debounce_time >= DEBOUNCE_MS) {
                s_joy_debounce_time = now_ms;
                s_joy_btn_last = joy_pressed;
                if (joy_pressed) {
                    s_joy_press_time = now_ms;
                    s_joy_long_fired = false;
                } else {
                    if (!s_joy_long_fired) {
                        queue_btn_event(b, 0); // Short press
                    }
                }
            }
        } else if (joy_pressed && !s_joy_long_fired) {
            if (now_ms - s_joy_press_time >= LONG_PRESS_MS) {
                s_joy_long_fired = true;
                queue_btn_event(b, 1); // Long press
            }
        }

        if (joy_pressed) btn_mask |= (1 << 4);
        s_joy_btn = joy_pressed;
    }

    s_btn_state = btn_mask;
}

static void read_pot_mux(uint8_t pot_idx) {
    if (!s_ads_present) return;

    set_mux_channel(pot_idx);
    esp_rom_delay_us(100); // 100us settle time for MUX

    // AIN2 is MUX signal channel (ADC_A_2)
    uint16_t cfg = ADS_OS_SINGLE | ADS_MUX_AIN2 | ADS_PGA_4_096V | ADS_MODE_SINGLE | ADS_DR_860SPS | ADS_CQUE_NONE;
    ads_write_config_port(s_ads_port, cfg);
    vTaskDelay(pdMS_TO_TICKS(2)); // ~1.2ms conversion time at 860 SPS

    int16_t raw = 0;
    if (ads_read_conversion_port(s_ads_port, &raw) == ESP_OK) {
        s_ads_read_count++;
        if (raw < 0) raw = 0;
        float voltage = (float)raw * 0.125f / 1000.0f;
        float norm = voltage / 3.3f;
        if (norm > 1.0f) norm = 1.0f;
        if (norm < 0.0f) norm = 0.0f;

        // First read: snap directly to the real value (skip EMA ramp).
        if (!s_first_pot_read[pot_idx]) {
            s_knob_values[pot_idx] = norm;
            s_first_pot_read[pot_idx] = true;
        } else {
            s_knob_values[pot_idx] = POT_EMA_ALPHA * norm + (1.0f - POT_EMA_ALPHA) * s_knob_values[pot_idx];
        }
    }
}

static void read_joystick_axes(void) {
    if (!s_ads_present) return;

    // Joystick Y (AIN0 = ADC_A_1)
    uint16_t cfg = ADS_OS_SINGLE | ADS_MUX_AIN0 | ADS_PGA_4_096V | ADS_MODE_SINGLE | ADS_DR_860SPS | ADS_CQUE_NONE;
    ads_write_config_port(s_ads_port, cfg);
    vTaskDelay(pdMS_TO_TICKS(2));

    int16_t raw = 0;
    if (ads_read_conversion_port(s_ads_port, &raw) == ESP_OK) {
        if (raw < 0) raw = 0;
        float norm = ((float)raw * 0.125f / 1000.0f) / 4.096f;
        if (norm > 1.0f) norm = 1.0f;
        if (norm < 0.0f) norm = 0.0f;
        if (!s_first_joy_y_read) {
            s_joy_y = norm;
            s_first_joy_y_read = true;
        } else {
            s_joy_y = JOY_EMA_ALPHA * norm + (1.0f - JOY_EMA_ALPHA) * s_joy_y;
        }
    }

    // Joystick X (AIN1 = ADC_B_1)
    cfg = ADS_OS_SINGLE | ADS_MUX_AIN1 | ADS_PGA_4_096V | ADS_MODE_SINGLE | ADS_DR_860SPS | ADS_CQUE_NONE;
    ads_write_config_port(s_ads_port, cfg);
    vTaskDelay(pdMS_TO_TICKS(2));

    if (ads_read_conversion_port(s_ads_port, &raw) == ESP_OK) {
        if (raw < 0) raw = 0;
        float norm = ((float)raw * 0.125f / 1000.0f) / 4.096f;
        if (norm > 1.0f) norm = 1.0f;
        if (norm < 0.0f) norm = 0.0f;
        if (!s_first_joy_x_read) {
            s_joy_x = norm;
            s_first_joy_x_read = true;
        } else {
            s_joy_x = JOY_EMA_ALPHA * norm + (1.0f - JOY_EMA_ALPHA) * s_joy_x;
        }
    }
}

void cynthia_input_task(void *pvParameters) {
    fprintf(stderr, "cynthia_input_task: starting...\n"); fflush(stderr);
    cynthia_input_init();
    fprintf(stderr, "cynthia_input_task: init complete, entering loop (ads_present=%d, mcp0=%d, mcp1=%d)\n",
            s_ads_present, s_mcp0_present, s_mcp1_present);
    fflush(stderr);

    uint8_t pot_cursor = 0;
    uint32_t last_joy_poll = 0;

    for (;;) {
        uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000ULL);
        s_task_loop_count++;

        // 1. Fast Key & Button Scan (100Hz)
        scan_keys_and_buttons(now_ms);

        // 2. Interleaved Pot Scan (1 channel per iteration)
        read_pot_mux(pot_cursor);
        pot_cursor = (pot_cursor + 1) % CYNTHIA_NUM_POTS;

        // 3. Joystick scan at ~30Hz
        if (now_ms - last_joy_poll >= 33) {
            read_joystick_axes();
            last_joy_poll = now_ms;
        }

        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

// Thread-safe cached getters
void cynthia_get_knobs(float *out_knobs, uint8_t count) {
    if (!out_knobs) return;
    uint8_t n = (count < CYNTHIA_NUM_POTS) ? count : CYNTHIA_NUM_POTS;
    for (uint8_t i = 0; i < n; i++) {
        out_knobs[i] = s_knob_values[i];
    }
}

void cynthia_get_joystick(float *out_x, float *out_y, bool *out_btn) {
    if (out_x) *out_x = s_joy_x;
    if (out_y) *out_y = s_joy_y;
    if (out_btn) *out_btn = s_joy_btn;
}

uint8_t cynthia_get_buttons(void) {
    return s_btn_state;
}

bool cynthia_get_button_event(uint8_t *btn_idx, uint8_t *kind) {
    if (s_evt_head == s_evt_tail) return false;
    if (btn_idx) *btn_idx = s_evt_queue[s_evt_tail].btn;
    if (kind) *kind = s_evt_queue[s_evt_tail].kind;
    s_evt_tail = (s_evt_tail + 1) % BTN_EVT_QUEUE_SIZE;
    return true;
}

int8_t cynthia_get_octave_shift(void) {
    return s_octave_shift;
}

void cynthia_set_octave_shift(int8_t shift) {
    if (shift < -4) shift = -4;
    if (shift > 4) shift = 4;
    s_octave_shift = shift;
}

void cynthia_get_debug_status(uint32_t *out_loops, uint32_t *out_ads_reads, uint8_t *out_ads_present, uint8_t *out_ads_port) {
    if (out_loops) *out_loops = s_task_loop_count;
    if (out_ads_reads) *out_ads_reads = s_ads_read_count;
    if (out_ads_present) *out_ads_present = s_ads_present ? 1 : 0;
    if (out_ads_port) *out_ads_port = (uint8_t)s_ads_port;
}

#endif // ESP_PLATFORM
