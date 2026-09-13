#pragma once

#include "esp_err.h"

/* Brings up I2C0 (GPIO3 SDA / GPIO10 SCL, per firmware/CLAUDE.md GPIO map)
 * and the SSD1306 panel via ESP-IDF's native esp_lcd_panel_ssd1306 driver —
 * no external component; see firmware/CLAUDE.md for why. Call once before
 * oled_show_readings(). Returns an error rather than aborting so app_main
 * can decide whether a missing/unresponsive display is fatal. */
esp_err_t oled_display_init(void);

/* Clears the framebuffer, draws "Temp: X.X°C" and "Humidity: Y.Y%", and
 * flushes the whole 128x64 buffer to the panel in one I2C transaction. */
esp_err_t oled_show_readings(float temp_c, float humidity_pct);
