/*
 * SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
 * SPDX-License-Identifier: Apache-2.0
 */

#include "mosaico_boot_splash.h"
#include "mosaico_boot_handoff.h"

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "sdkconfig.h"
#if CONFIG_EFUSE_VIRTUAL
#include "esp_efuse.h"
#include "esp_efuse_table.h"
#endif
#include "esp_log.h"
#include "esp_rom_gpio.h"
#include "esp_rom_sys.h"
#include "hal/gpio_ll.h"
#include "hal/spi_ll.h"
#include "soc/efuse_reg.h"
#include "soc/gpio_sig_map.h"
#include "soc/soc.h"

#define LCD_WIDTH 480U
#define LCD_HEIGHT 480U
#define LCD_POWER_GPIO 60
#define LCD_RESET_GPIO_V1_0 42
#define LCD_CLK_GPIO_V1_0 44
#define LCD_RESET_GPIO_V1_2 44
#define LCD_CLK_GPIO_V1_2 42
#define LCD_CS_GPIO 50
#define LCD_DATA0_GPIO 36
#define LCD_DATA1_GPIO 51
#define LCD_DATA2_GPIO 35
#define LCD_DATA3_GPIO 9
#define LCD_SPI_CLOCK_HZ 40000000U
#define LCD_SPI_SOURCE_HZ 40000000U
#define LCD_FIFO_BYTES 64U
#define LCD_CMD_WRITE 0x02U
#define LCD_COLOR_WRITE 0x32U
#define LCD_BOOT_BRIGHTNESS 0xFFU
#define LCD_TRANSFER_TIMEOUT_MS 100U

#define BOOT_MOTOR_GPIO 8
#define BOOT_MOTOR_ON_LEVEL 1
#define BOOT_MOTOR_OFF_LEVEL 0
#define BOOT_MOTOR_PULSE_US 60000U

#define LOGO_CELL_PX 8U
#define LOGO_GLYPH_COLS 5U
#define LOGO_GLYPH_ROWS 7U
#define LOGO_CHAR_ADVANCE 6U
#define LOGO_CHAR_COUNT 7U
#define LOGO_WIDTH                                                             \
  (((LOGO_CHAR_COUNT - 1U) * LOGO_CHAR_ADVANCE + LOGO_GLYPH_COLS) *            \
   LOGO_CELL_PX)
#define LOGO_HEIGHT (LOGO_GLYPH_ROWS * LOGO_CELL_PX)
#define LOGO_X ((LCD_WIDTH - LOGO_WIDTH) / 2U)
#define LOGO_Y ((LCD_HEIGHT - LOGO_HEIGHT) / 2U)
#define LOGO_COLOR 0xFA60U

#define MOSAICO_HW_VERSION(major, minor)                                       \
  ((uint16_t)(((uint16_t)(major) << 8) | ((uint16_t)(minor) & 0xFFU)))

static const char *TAG = "boot_splash";
static uint16_t s_hw_version;

/* Compact 5x7 lowercase "mosaico". Each set bit becomes one rounded 6x6 dot
 * in an 8x8 cell, matching the product wordmark without storing a bitmap. */
static const uint8_t s_logo[LOGO_CHAR_COUNT][LOGO_GLYPH_ROWS] = {
    {0x00, 0x1A, 0x15, 0x15, 0x15, 0x15, 0x15}, /* m */
    {0x00, 0x0E, 0x11, 0x11, 0x11, 0x11, 0x0E}, /* o */
    {0x00, 0x0F, 0x10, 0x0E, 0x01, 0x01, 0x1E}, /* s */
    {0x00, 0x0E, 0x01, 0x0F, 0x11, 0x13, 0x0D}, /* a */
    {0x04, 0x00, 0x0C, 0x04, 0x04, 0x04, 0x0E}, /* i */
    {0x00, 0x0F, 0x10, 0x10, 0x10, 0x10, 0x0F}, /* c */
    {0x00, 0x0E, 0x11, 0x11, 0x11, 0x11, 0x0E}, /* o */
};

static bool hardware_version_supported(void) {
#if CONFIG_EFUSE_VIRTUAL
  uint16_t version = 0;
  if (esp_efuse_read_field_blob(ESP_EFUSE_USER_DATA, &version,
                                sizeof(version) * 8U) != ESP_OK) {
    return false;
  }
#else
  /* ESP32-S31 USER_DATA starts at block 3 bit 0. The ROM has already loaded
   * its read registers before entering this bootloader. Only the low 16 bits
   * encode the board version; avoid linking the general eFuse blob walker
   * just to read this fixed field. Keep the API path for virtual eFuse tests. */
  const uint16_t version = (uint16_t)REG_READ(EFUSE_RD_USR_DATA0_REG);
#endif
  if (version != MOSAICO_HW_VERSION(1, 0) &&
      version != MOSAICO_HW_VERSION(1, 1) &&
      version != MOSAICO_HW_VERSION(1, 2)) {
    return false;
  }
  s_hw_version = version;
  return true;
}

static void gpio_output(int gpio, int level) {
  esp_rom_gpio_pad_select_gpio(gpio);
  gpio_ll_set_level(&GPIO, gpio, level);
  gpio_ll_output_enable(&GPIO, gpio);
}

static void boot_feedback_start(void) {
  /* The motor and panel share VCC_3V3.  Start tactile feedback before the LCD
   * reset and initialization delays so power-on is perceptible immediately. */
  gpio_output(LCD_POWER_GPIO, 0);
  gpio_output(BOOT_MOTOR_GPIO, BOOT_MOTOR_OFF_LEVEL);
  gpio_ll_set_level(&GPIO, BOOT_MOTOR_GPIO, BOOT_MOTOR_ON_LEVEL);
}

static void boot_feedback_stop(void) {
  gpio_ll_set_level(&GPIO, BOOT_MOTOR_GPIO, BOOT_MOTOR_OFF_LEVEL);
}

static void route_spi_output(int gpio, int signal) {
  esp_rom_gpio_pad_select_gpio(gpio);
  esp_rom_gpio_connect_out_signal(gpio, signal, false, false);
  gpio_ll_output_enable(&GPIO, gpio);
}

static bool spi_wait(void) {
  uint32_t started_ms = 0;
  unsigned polls = 0;
  while (!spi_ll_usr_is_done(&GPSPI2)) {
    /* A normal 64-byte QSPI transfer completes in a few microseconds.  Avoid
     * reading the early boot clock for every one of the ~7200 chunks; sample
     * it only if a transfer is unexpectedly slow, while retaining a bounded
     * failure path for broken hardware. */
    if ((++polls & 0xFFU) == 0U) {
      const uint32_t now = esp_log_early_timestamp();
      if (started_ms == 0U) {
        started_ms = now;
      } else if ((uint32_t)(now - started_ms) >= LCD_TRANSFER_TIMEOUT_MS) {
        return false;
      }
    }
  }
  return true;
}

static bool spi_tx(const void *data, size_t size, uint8_t lines,
                   bool keep_cs_active) {
  const uint8_t *cursor = data;
  while (size > 0) {
    uint8_t fifo[LCD_FIFO_BYTES] = {0};
    const size_t chunk = size > sizeof(fifo) ? sizeof(fifo) : size;
    memcpy(fifo, cursor, chunk);
    /* This boot-only bus has one TX-only device, no DMA/RX, and no hardware
     * command/address/dummy phases. Configure only what changes per chunk;
     * the fixed settings are established once in spi_init(). */
    spi_ll_clear_int_stat(&GPSPI2);
    spi_ll_master_set_line_mode(&GPSPI2, (spi_line_mode_t){
        .cmd_lines = 1, .addr_lines = 1, .data_lines = lines});
    spi_ll_set_mosi_bitlen(&GPSPI2, chunk * 8U);
    spi_ll_master_keep_cs(&GPSPI2, keep_cs_active || chunk < size);
    spi_ll_write_buffer(&GPSPI2, fifo, chunk * 8U);
    spi_ll_apply_config(&GPSPI2);
    spi_ll_user_start(&GPSPI2);
    if (!spi_wait()) {
      return false;
    }
    cursor += chunk;
    size -= chunk;
  }
  return true;
}

static bool lcd_command(uint8_t command, const void *params,
                        size_t param_size) {
  uint8_t packet[16] = {LCD_CMD_WRITE, 0x00, command, 0x00};
  if (param_size > sizeof(packet) - 4U) {
    return false;
  }
  if (params != NULL && param_size > 0) {
    memcpy(&packet[4], params, param_size);
  }
  return spi_tx(packet, 4U + param_size, 1, false);
}

static bool lcd_set_window(uint16_t x, uint16_t y, uint16_t width,
                           uint16_t height) {
  const uint16_t x_end = x + width - 1U;
  const uint16_t y_end = y + height - 1U;
  const uint8_t columns[] = {
      (uint8_t)(x >> 8),
      (uint8_t)x,
      (uint8_t)(x_end >> 8),
      (uint8_t)x_end,
  };
  const uint8_t rows[] = {
      (uint8_t)(y >> 8),
      (uint8_t)y,
      (uint8_t)(y_end >> 8),
      (uint8_t)y_end,
  };
  return lcd_command(0x2A, columns, sizeof(columns)) &&
         lcd_command(0x2B, rows, sizeof(rows));
}

static void spi_init(void) {
  int __DECLARE_RCC_ATOMIC_ENV __attribute__((unused));
  spi_ll_enable_bus_clock(SPI2_HOST, true);
  spi_ll_reset_register(SPI2_HOST);
  spi_ll_enable_clock(SPI2_HOST, true);
  spi_ll_set_clk_source(&GPSPI2, SPI_CLK_SRC_XTAL);
  spi_ll_clk_source_pre_div(&GPSPI2, 1, 1);

  const int lcd_clk_gpio = s_hw_version == MOSAICO_HW_VERSION(1, 0)
                               ? LCD_CLK_GPIO_V1_0
                               : LCD_CLK_GPIO_V1_2;
  route_spi_output(lcd_clk_gpio, SPI2_CK_PAD_OUT_IDX);
  route_spi_output(LCD_DATA0_GPIO, SPI2_D_PAD_OUT_IDX);
  route_spi_output(LCD_DATA1_GPIO, SPI2_Q_PAD_OUT_IDX);
  route_spi_output(LCD_DATA2_GPIO, SPI2_WP_PAD_OUT_IDX);
  route_spi_output(LCD_DATA3_GPIO, SPI2_HOLD_PAD_OUT_IDX);
  route_spi_output(LCD_CS_GPIO, SPI2_CS_PAD_OUT_IDX);

  spi_ll_master_init(&GPSPI2);
  spi_ll_clock_val_t clock_reg;
  spi_ll_master_cal_clock(LCD_SPI_SOURCE_HZ, LCD_SPI_CLOCK_HZ, 128,
                          &clock_reg);
  spi_ll_master_set_clock_by_reg(&GPSPI2, &clock_reg);
  spi_ll_master_set_pos_cs(&GPSPI2, 0, false);
  spi_ll_master_set_mode(&GPSPI2, 0);
  spi_ll_set_tx_lsbfirst(&GPSPI2, false);
  spi_ll_set_rx_lsbfirst(&GPSPI2, false);
  spi_ll_set_half_duplex(&GPSPI2, true);
  spi_ll_set_sio_mode(&GPSPI2, false);
  spi_ll_master_set_cs_setup(&GPSPI2, 0);
  spi_ll_master_set_cs_hold(&GPSPI2, 0);
  spi_ll_master_select_cs(&GPSPI2, 0);
  spi_ll_enable_ddr_mode(&GPSPI2, false);
  spi_ll_set_command_bitlen(&GPSPI2, 0);
  spi_ll_set_addr_bitlen(&GPSPI2, 0);
  spi_ll_set_dummy(&GPSPI2, 0);
  spi_ll_enable_mosi(&GPSPI2, true);
  spi_ll_enable_miso(&GPSPI2, false);
}

static bool panel_init(void) {
  /* Every command in this table takes exactly one byte. */
  static const uint8_t init[][2] = {
      {0xFE, 0x20}, {0x19, 0x10},
      {0x1C, 0xA0}, {0xFE, 0x00},
      {0xC4, 0x80}, {0x3A, 0x55},
#if CONFIG_BSP_CO5300_ENABLE_TE
      {0x35, 0x00},
#endif
      {0x53, 0x20}, {0x51, LCD_BOOT_BRIGHTNESS},
      {0x63, 0xFF}, {0x36, 0x00},
  };

  const int lcd_reset_gpio = s_hw_version == MOSAICO_HW_VERSION(1, 0)
                                 ? LCD_RESET_GPIO_V1_0
                                 : LCD_RESET_GPIO_V1_2;
  gpio_output(LCD_POWER_GPIO, 0);
  gpio_output(lcd_reset_gpio, 0);
  esp_rom_delay_us(10000);
  gpio_ll_set_level(&GPIO, lcd_reset_gpio, 1);
  esp_rom_delay_us(BOOT_MOTOR_PULSE_US - 10000U);
  boot_feedback_stop();
  esp_rom_delay_us(150000U - (BOOT_MOTOR_PULSE_US - 10000U));

  spi_init();
  if (!lcd_command(0x11, NULL, 0)) {
    return false;
  }
  esp_rom_delay_us(60000);
  for (size_t i = 0; i < sizeof(init) / sizeof(init[0]); ++i) {
    if (!lcd_command(init[i][0], &init[i][1], 1)) {
      return false;
    }
  }
  return true;
}

static bool lcd_write_solid(uint16_t color, size_t pixels, bool keep_cs) {
  uint8_t fifo[LCD_FIFO_BYTES];
  for (size_t i = 0; i < sizeof(fifo); i += 2U) {
    fifo[i] = (uint8_t)(color >> 8);
    fifo[i + 1U] = (uint8_t)color;
  }
  while (pixels > 0U) {
    const size_t chunk = pixels > sizeof(fifo) / 2U
                             ? sizeof(fifo) / 2U
                             : pixels;
    pixels -= chunk;
    if (!spi_tx(fifo, chunk * 2U, 4, keep_cs || pixels > 0U)) {
      return false;
    }
  }
  return true;
}

static bool draw_splash(void) {
  if (!lcd_set_window(0, 0, LCD_WIDTH, LCD_HEIGHT)) {
    return false;
  }
  const uint8_t write_command[] = {LCD_COLOR_WRITE, 0x00, 0x2C, 0x00};
  if (!spi_tx(write_command, sizeof(write_command), 1, true)) {
    return false;
  }

  if (!lcd_write_solid(0, LCD_WIDTH * LCD_HEIGHT, false)) {
    return false;
  }
  for (unsigned character = 0; character < LOGO_CHAR_COUNT; ++character) {
    for (unsigned row = 0; row < LOGO_GLYPH_ROWS; ++row) {
      for (unsigned column = 0; column < LOGO_GLYPH_COLS; ++column) {
        if ((s_logo[character][row] &
             (1U << (LOGO_GLYPH_COLS - 1U - column))) == 0U) {
          continue;
        }
        const uint16_t x = LOGO_X +
            (character * LOGO_CHAR_ADVANCE + column) * LOGO_CELL_PX + 1U;
        const uint16_t y = LOGO_Y + row * LOGO_CELL_PX + 1U;
        if (!lcd_set_window(x, y, 6, 6) ||
            !spi_tx(write_command, sizeof(write_command), 1, true) ||
            !lcd_write_solid(LOGO_COLOR, 36, false)) {
          return false;
        }
      }
    }
  }
  return lcd_command(0x29, NULL, 0);
}

bool mosaico_boot_splash_show(void) {
  mosaico_boot_handoff_clear();
  if (!hardware_version_supported()) {
    ESP_LOGW(TAG, "unsupported hardware; LCD splash skipped");
    return false;
  }
  boot_feedback_start();
  const bool splash_visible = panel_init() && draw_splash();
  /* Idempotent failure guard; the normal path stops after the short pulse. */
  boot_feedback_stop();
  if (!splash_visible) {
    ESP_LOGW(TAG, "LCD splash failed; continuing boot");
    return false;
  }
  mosaico_boot_handoff_publish();
  ESP_LOGI(TAG, "LCD boot splash visible");
  return true;
}
