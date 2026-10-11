# CYNTHIA board configuration
# Compact ESP32-S3 music controller with PCM5102A + ADS1115

set(IDF_TARGET esp32s3)

# Enable TinyUSB for USB CDC REPL
set(MICROPY_PY_TINYUSB ON)

# Board definition passed as C preprocessor macro
set(BOARD_DEFINITION1 CYNTHIA)
set(BOARD_DEFINITION2 "")

set(SDKCONFIG_DEFAULTS
    ../../micropython/ports/esp32/boards/sdkconfig.base
    ../../micropython/ports/esp32/boards/sdkconfig.usb
    ../../micropython/ports/esp32/boards/sdkconfig.240mhz
    ../esp32s3/boards/sdkconfig.tulip
    boards/CYNTHIA/sdkconfig.board
)

list(APPEND MICROPY_SOURCE_BOARD
    boards/CYNTHIA/cynthia_input.c
    boards/CYNTHIA/cynthia_ui.c
)
