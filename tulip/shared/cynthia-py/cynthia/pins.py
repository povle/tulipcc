# cynthia/pins.py
# Single source of truth for the CYNTHIA mainboard pin map.
# Transcribed from ~/projects/synth/src/Board.h (the working Arduino
# project — the authoritative source for this board revision).
#
# Keep this file in sync with:
#   tulip/amyboard/boards/CYNTHIA/pins.h   (C build)
#   tulip/amyboard/boards/CYNTHIA/pins.csv (MicroPython board pins)

# --- I2S PCM5102A DAC ---
I2S_BCLK = 38
I2S_DIN  = 39   # DAC data in
I2S_LRCK = 40

# --- I2C Bus 0 (ADS1115 + optional MCP23017s) ---
I2C_SDA = 17
I2C_SCL = 16

# --- I2C Bus 1 (optional display) ---
I2C_DISPLAY_SDA = 5
I2C_DISPLAY_SCL = 4

# --- Battery monitoring ---
BATT_LVL    = 1
BATT_STAT_1 = 2
BATT_STAT_2 = 44

# --- Daughterboard slot GPIOs A..G ---
# Each FFC connector exposes 7 general-purpose GPIOs labeled A..G.
SLOT_GPIOS = {
    0: {  # DB_1
        "A": 6, "B": 7, "C": 15, "D": 18, "E": 8, "F": 9, "G": 10,
    },
    1: {  # DB_2
        "A": 48, "B": 47, "C": 21, "D": 11, "E": 12, "F": 13, "G": 14,
    },
}

# --- ADS1115 analog channel mapping ---
# Each slot's analog pins A/B feed two ADS1115 channels.
# slot0 A/B -> AIN0/AIN1, slot1 A/B -> AIN2/AIN3
ADC_CHANNELS = {
    0: {"A": 0, "B": 1},
    1: {"A": 2, "B": 3},
}

ADS1115_ADDR = 0x48

# NOTE: the keyboard and LED are DAUGHTERBOARD hardware, not onboard. Their
# pins are just slot GPIOs (e.g. the LED on GPIO48 = slot 1 pin A), claimed
# by the respective daughterboard drivers through their daughterboard.json
# like any other pin. No keyboard or LED pins are reserved here.

NUM_SLOTS = 2
