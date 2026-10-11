// CYNTHIA pin definitions
// Compact ESP32-S3 music controller with PCM5102A + ADS1115
// Pin assignments transcribed from ~/projects/synth/src/Board.h
// (the working Arduino project — the authoritative source for this board)

// === I2S PCM5102A DAC ===
// BCK=GPIO38, DIN=GPIO39, LRCK=GPIO40, no MCLK
#define CONFIG_I2S_MCLK (-1)
#define CONFIG_I2S_BCLK (38)
#define CONFIG_I2S_LRCLK (40)
#define CONFIG_I2S_DOUT (39)
#define CONFIG_I2S_DIN (-1)

// === I2C Bus 0 (ADS1115 + optional MCP23017s) ===
// SDA=GPIO17, SCL=GPIO16
#define I2C_MASTER_SDA (17)
#define I2C_MASTER_SCL (16)
#define I2C_NUM I2C_NUM_0
#define I2C_CLK_FREQ (400000)

// === I2C Bus 1 (optional display) ===
// SDA=GPIO5, SCL=GPIO4
#define I2C_DISPLAY_SDA (5)
#define I2C_DISPLAY_SCL (4)

// === ADS1115 ADC ===
#define ADS1115_ADDR (0x48)

// === MIDI (not present on Cynthia, dummy pins) ===
#define MIDI_OUT_PIN_A (-1)
#define MIDI_OUT_PIN_B (-1)
#define MIDI_IN_PIN (-1)

// === Battery monitoring (from Board.h) ===
#define BATT_LVL (1)
#define BATT_STAT_1_PIN (2)
#define BATT_STAT_2_PIN (44)

// === Daughterboard slot 1 (DB_1) general-purpose GPIOs A..G ===
#define DB1_A (6)
#define DB1_B (7)
#define DB1_C (15)
#define DB1_D (18)
#define DB1_E (8)
#define DB1_F (9)
#define DB1_G (10)

// === Daughterboard slot 2 (DB_2) general-purpose GPIOs A..G ===
#define DB2_A (48)
#define DB2_B (47)
#define DB2_C (21)
#define DB2_D (11)
#define DB2_E (12)
#define DB2_F (13)
#define DB2_G (14)

// === ADS1115 analog channel mapping ===
// slot1 A/B -> AIN0/AIN1, slot2 A/B -> AIN2/AIN3
#define ADC_A_1 (0)
#define ADC_B_1 (1)
#define ADC_A_2 (2)
#define ADC_B_2 (3)

// NOTE: the keyboard and LED are DAUGHTERBOARD hardware, not onboard. Their
// pins are just slot GPIOs (e.g. the LED on GPIO48 = slot 2 pin A), claimed
// by the respective daughterboard drivers through their daughterboard.json
// like any other pin. No keyboard or LED pins are reserved here.

// === SPI (not used on Cynthia, dummy pins) ===
#define SPI0_MOSI (-1)
#define SPI0_MISO (-1)
#define SPI0_CLK (-1)
#define SPI0_CS (-1)
#define SPI0_DC (-1)
#define SPI0_RST (-1)
#define SPI0_BL (-1)

// === MPIO (not used on Cynthia) ===
#define MPIO_C0 (-1)