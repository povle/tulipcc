// cynthia_pots.c - fast sweep of the spark-pots mux pair (MCP3002 x2 behind a
// 74HC393 counter), for the Python driver in cynthia/drivers/spark_pots_new.py.
//
// WHY THIS EXISTS
// ---------------
// The Python sweep is 16 addresses x 2 channels x (dummy + real) conversions.
// Measured on the panel: 17.2 ms per scan. That is ~560 MicroPython operations
// at ~17 us each - the SPI wire time is only ~1 ms of it. Because the sweep
// shares a thread with the UI draw (~15 ms/frame), 17 ms per scan caps the whole
// control loop near 30 Hz, which is what "the pots feel slow" was.
//
// The same work here is ~3 ms: 64 conversions of 16 us of wire time plus the
// mux/ADC settles. Nothing about the *protocol* changes - same control bytes,
// same dummy-then-real transaction, same address maps - so the Python driver
// keeps its angle maths, pair checks and deadbands, and only the bus access
// moves.
//
// OWNERSHIP
// ---------
// The SPI host is claimed here (spi_bus_initialize) and held until
// cynthia_pots_deinit(). While it is up the Python driver MUST NOT create its
// own machine.SPI on those pins - hence the driver skips that path entirely
// when init() succeeds, and falls back to Python (deinit first) when the first
// sweep does not produce a sane pair.

#include <string.h>

#include "esp_attr.h"
#include "esp_err.h"
#include "esp_rom_sys.h"
#include "driver/gpio.h"
#include "driver/spi_master.h"

#define POTS_NUM_ADDRS 16

// Fixed by the panel's wiring: counter address -> sensor number (1-based).
// Kept in step with MUX0_MAP/MUX1_MAP in spark_pots_new.py.
static const uint8_t mux0_map[POTS_NUM_ADDRS] = {
    8, 7, 6, 5, 4, 3, 2, 1, 16, 15, 14, 13, 12, 11, 10, 9
};
static const uint8_t mux1_map[POTS_NUM_ADDRS] = {
    1, 2, 3, 4, 5, 6, 7, 8, 16, 15, 14, 13, 12, 11, 10, 9
};

#define MCP_CMD_CH0 0x68
#define MCP_CMD_CH1 0x78

static spi_device_handle_t pots_spi = NULL;
static spi_host_device_t pots_host = SPI2_HOST;
static int pots_clk = -1;
static int pots_rst = -1;
static int pots_cs = -1;
static int pots_addr = 0;   // where the 74HC393 currently sits

static void pots_pin_out(int gpio) {
    gpio_reset_pin(gpio);
    gpio_set_direction(gpio, GPIO_MODE_OUTPUT);
}

// 74HC393 counts on the falling edge of CLK.
static void pots_pulse(void) {
    gpio_set_level(pots_clk, 1);
    esp_rom_delay_us(10);
    gpio_set_level(pots_clk, 0);
    esp_rom_delay_us(10);
}

// One MCP3002 conversion. The dummy transaction is NOT optional: switching the
// mux selects a different source, and the sample cap holds the previous
// channel's voltage, so the first conversion after a switch reads the old
// channel. Its result is discarded here (Python keeps it as the settle
// diagnostic; that is a debug-only detail).
static uint16_t pots_convert(int channel, int settle_us) {
    uint8_t tx[2];
    uint8_t rx[2] = { 0, 0 };
    spi_transaction_t t;
    tx[0] = channel ? MCP_CMD_CH1 : MCP_CMD_CH0;
    tx[1] = 0x00;
    memset(&t, 0, sizeof(t));
    t.length = 16;
    t.tx_buffer = tx;

    gpio_set_level(pots_cs, 0);
    t.rx_buffer = rx;
    spi_device_transmit(pots_spi, &t);
    gpio_set_level(pots_cs, 1);
    if (settle_us > 0) {
        esp_rom_delay_us(settle_us);
    }
    rx[0] = 0;
    rx[1] = 0;
    gpio_set_level(pots_cs, 0);
    spi_device_transmit(pots_spi, &t);
    gpio_set_level(pots_cs, 1);
    return (uint16_t)(((rx[0] & 0x03) << 8) | rx[1]);
}

// Returns 0 on success, negative on failure. `host` is the ESP-IDF SPI host
// (2 or 3) matching the slot the daughterboard is plugged into.
int cynthia_pots_init(int host, int clk, int rst, int cs, int sck, int mosi, int miso) {
    spi_bus_config_t bus;
    spi_device_interface_config_t dev;

    if (pots_spi != NULL) {
        return 0;   // already up
    }
    if (host != 2 && host != 3) {
        return -1;
    }
    pots_host = (host == 3) ? SPI3_HOST : SPI2_HOST;
    pots_clk = clk;
    pots_rst = rst;
    pots_cs = cs;
    pots_pin_out(clk);
    pots_pin_out(rst);
    pots_pin_out(cs);
    gpio_set_level(pots_clk, 0);
    gpio_set_level(pots_rst, 0);
    gpio_set_level(pots_cs, 1);

    memset(&bus, 0, sizeof(bus));
    bus.mosi_io_num = mosi;
    bus.miso_io_num = miso;
    bus.sclk_io_num = sck;
    bus.quadwp_io_num = -1;
    bus.quadhd_io_num = -1;
    bus.max_transfer_sz = 8;
    if (spi_bus_initialize(pots_host, &bus, SPI_DMA_DISABLED) != ESP_OK) {
        return -2;
    }
    memset(&dev, 0, sizeof(dev));
    dev.clock_speed_hz = 1000000;   // MCP3002 spec limit at 2.5V
    dev.mode = 0;
    dev.spics_io_num = -1;          // ~CS is driven as a plain GPIO
    dev.queue_size = 1;
    if (spi_bus_add_device(pots_host, &dev, &pots_spi) != ESP_OK) {
        spi_bus_free(pots_host);
        pots_spi = NULL;
        return -3;
    }
    pots_addr = 0;
    return 0;
}

void cynthia_pots_deinit(void) {
    if (pots_spi != NULL) {
        spi_bus_remove_device(pots_spi);
        pots_spi = NULL;
    }
    if (pots_host != SPI2_HOST || pots_clk >= 0) {
        spi_bus_free(pots_host);
    }
    pots_clk = pots_rst = pots_cs = -1;
}

bool cynthia_pots_ready(void) {
    return pots_spi != NULL;
}

// Visit `n` addresses in ASCENDING order (the counter only counts up), writing
// each one's two wipers into out0/out1 by SENSOR index (0..15), so the caller's
// lists stay in pot order. Returns 0, or negative if the bus is not up.
int cynthia_pots_sweep(const uint8_t *addrs, int n, uint16_t *out0, uint16_t *out1,
                       int settle_us, int mux_settle_us) {
    int k;

    if (pots_spi == NULL) {
        return -1;
    }
    if (n > POTS_NUM_ADDRS) {
        n = POTS_NUM_ADDRS;
    }
    // Park the counter at 0: the last pulse of a sweep is skipped so it stops
    // at the final address, which means every sweep has to re-home.
    gpio_set_level(pots_rst, 1);
    esp_rom_delay_us(10);
    gpio_set_level(pots_rst, 0);
    esp_rom_delay_us(mux_settle_us);
    pots_addr = 0;

    for (k = 0; k < n; k++) {
        uint8_t addr = addrs[k];
        uint16_t r0, r1;
        while (pots_addr < (int)addr) {
            pots_pulse();
            pots_addr++;
        }
        // Passing THROUGH addresses costs nothing electrically - nothing reads
        // them - so only the target needs the mux settle.
        esp_rom_delay_us(mux_settle_us);
        r1 = pots_convert(1, settle_us);
        r0 = pots_convert(0, settle_us);
        out0[mux0_map[addr] - 1] = r0;
        out1[mux1_map[addr] - 1] = r1;
    }
    return 0;
}
