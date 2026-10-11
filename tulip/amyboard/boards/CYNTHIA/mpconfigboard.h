// CYNTHIA board configuration
// Compact ESP32-S3 music controller with PCM5102A + ADS1115

#include "pins.h"

#define MICROPY_HW_BOARD_NAME "CYNTHIA (AMYboard)"
#define MICROPY_HW_MCU_NAME "ESP32S3"

// TinyUSB CDC for REPL + USB MIDI (required for web UI compatibility)
#define MICROPY_HW_ENABLE_UART_REPL (0)
#define MICROPY_HW_USB_CDC (1)
#define MICROPY_HW_ESP_USB_SERIAL_JTAG (0)
#define MICROPY_HW_ENABLE_USBDEV (1)
#define MICROPY_HW_USB_CDC_1200BPS_TOUCH (1)

// No DAC on Cynthia
#define MICROPY_PY_MACHINE_DAC (0)

// TinyUSB buffer sizes
#define CFG_TUD_MIDI_RX_BUFSIZE 512
#define CFG_TUD_MIDI_TX_BUFSIZE 64