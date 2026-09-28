// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define ESP_LOGW(tag, ...) ((void)(tag))
#define ESP_LOGI(tag, ...) ((void)(tag))
#define SPI2_HOST 1
#define SPI_CLK_SRC_XTAL 1
#define SPI2_CK_PAD_OUT_IDX 1
#define SPI2_D_PAD_OUT_IDX 2
#define SPI2_Q_PAD_OUT_IDX 3
#define SPI2_WP_PAD_OUT_IDX 4
#define SPI2_HOLD_PAD_OUT_IDX 5
#define SPI2_CS_PAD_OUT_IDX 6
#define LP_SYSTEM_REG_LP_STORE15_REG 0
#define EFUSE_RD_USR_DATA0_REG 1
#define __DECLARE_RCC_ATOMIC_ENV unused_atomic
#define ESP_OK 0

typedef struct { int cmd_lines, addr_lines, data_lines; } spi_line_mode_t;
typedef unsigned spi_ll_clock_val_t;
typedef struct {
    size_t bytes;
    uint8_t fifo[64];
    spi_line_mode_t mode;
    bool keep_cs;
} spi_dev_t;
static spi_dev_t GPSPI2;
static int GPIO;
static uint32_t regs[2], elapsed_us;
static unsigned transfers, fail_at, clock_pin, reset_pin, motor_on_us, motor_off_us;
static int motor_level;
#define REG_READ(reg) regs[reg]
#define REG_WRITE(reg, value) (regs[reg] = (value))

static inline void esp_rom_gpio_pad_select_gpio(int pin) { (void)pin; }
static inline void gpio_ll_output_enable(int *gpio, int pin) { (void)gpio; (void)pin; }
static inline void gpio_ll_set_level(int *gpio, int pin, int level) {
    (void)gpio;
    if (pin == 8) {
        motor_level = level;
        if (level) motor_on_us = elapsed_us;
        else if (!motor_off_us && elapsed_us) motor_off_us = elapsed_us;
    }
    if (pin == 42 || pin == 44) reset_pin = pin;
}
static inline void esp_rom_gpio_connect_out_signal(int pin, int signal, bool inv, bool oen) {
    assert(!inv && !oen);
    if (signal == SPI2_CK_PAD_OUT_IDX) clock_pin = pin;
}
static inline void esp_rom_delay_us(uint32_t us) { elapsed_us += us; }
static inline uint32_t esp_log_early_timestamp(void) {
    elapsed_us += 1000;
    return elapsed_us / 1000;
}
static inline void spi_ll_enable_bus_clock(int host, bool enable) { assert(host == 1 && enable); }
static inline void spi_ll_reset_register(int host) { assert(host == 1); memset(&GPSPI2, 0, sizeof GPSPI2); }
static inline void spi_ll_enable_clock(int host, bool enable) { assert(host == 1 && enable); }
static inline void spi_ll_set_clk_source(spi_dev_t *hw, int source) { (void)hw; assert(source == 1); }
static inline void spi_ll_clk_source_pre_div(spi_dev_t *hw, int hs, int mst) { (void)hw; assert(hs == 1 && mst == 1); }
static inline void spi_ll_master_init(spi_dev_t *hw) { (void)hw; }
static inline void spi_ll_master_cal_clock(unsigned source, unsigned rate, int duty, spi_ll_clock_val_t *reg) {
    assert(source == 40000000 && rate == source && duty == 128); *reg = rate;
}
static inline void spi_ll_master_set_clock_by_reg(spi_dev_t *hw, const spi_ll_clock_val_t *reg) { (void)hw; assert(*reg == 40000000); }
static inline void spi_ll_master_set_pos_cs(spi_dev_t *hw, int cs, bool positive) { (void)hw; assert(cs == 0 && !positive); }
#define FIXED_SETTER(name, expected) \
    static inline void name(spi_dev_t *hw, int value) { (void)hw; assert(value == (expected)); }
FIXED_SETTER(spi_ll_master_set_mode, 0)
FIXED_SETTER(spi_ll_set_tx_lsbfirst, 0)
FIXED_SETTER(spi_ll_set_rx_lsbfirst, 0)
FIXED_SETTER(spi_ll_set_half_duplex, 1)
FIXED_SETTER(spi_ll_set_sio_mode, 0)
FIXED_SETTER(spi_ll_master_set_cs_setup, 0)
FIXED_SETTER(spi_ll_master_set_cs_hold, 0)
FIXED_SETTER(spi_ll_master_select_cs, 0)
FIXED_SETTER(spi_ll_enable_ddr_mode, 0)
FIXED_SETTER(spi_ll_set_command_bitlen, 0)
FIXED_SETTER(spi_ll_set_addr_bitlen, 0)
FIXED_SETTER(spi_ll_set_dummy, 0)
FIXED_SETTER(spi_ll_enable_mosi, 1)
FIXED_SETTER(spi_ll_enable_miso, 0)
static inline void spi_ll_clear_int_stat(spi_dev_t *hw) { (void)hw; }
static inline void spi_ll_master_set_line_mode(spi_dev_t *hw, spi_line_mode_t mode) {
    assert(mode.cmd_lines == 1 && mode.addr_lines == 1);
    assert(mode.data_lines == 1 || mode.data_lines == 4); hw->mode = mode;
}
static inline void spi_ll_set_mosi_bitlen(spi_dev_t *hw, size_t bits) {
    assert(bits && bits <= 512 && bits % 8 == 0); hw->bytes = bits / 8;
}
static inline void spi_ll_master_keep_cs(spi_dev_t *hw, bool keep) { hw->keep_cs = keep; }
static inline void spi_ll_write_buffer(spi_dev_t *hw, const uint8_t *data, size_t bits) {
    assert(bits / 8 == hw->bytes);
    /* The real LL reads whole words, even for a partial last word. */
    memcpy(hw->fifo, data, ((hw->bytes + 3) / 4) * 4);
}
static inline void spi_ll_apply_config(spi_dev_t *hw) { (void)hw; }
static inline void spi_ll_user_start(spi_dev_t *hw) {
    ++transfers;
    if (!fail_at) {
        const uint8_t header[] = {hw->mode.data_lines, hw->keep_cs, hw->bytes};
        assert(fwrite(header, sizeof header, 1, stdout) == 1);
        assert(fwrite(hw->fifo, hw->bytes, 1, stdout) == 1);
    }
}
static inline bool spi_ll_usr_is_done(spi_dev_t *hw) { (void)hw; return !fail_at || transfers < fail_at; }

#define ESP_EFUSE_USER_DATA 0
static inline int esp_efuse_read_field_blob(int field, void *dest, size_t bits) {
    assert(field == 0 && bits == 16); uint16_t version = regs[1]; memcpy(dest, &version, 2); return 0;
}
