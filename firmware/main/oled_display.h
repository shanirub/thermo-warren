#pragma once

#include <stdbool.h>

#include "esp_err.h"

/* Brings up I2C0 (GPIO3 SDA / GPIO10 SCL, per firmware/CLAUDE.md GPIO map)
 * and the SSD1306 panel via ESP-IDF's native esp_lcd_panel_ssd1306 driver —
 * no external component; see firmware/CLAUDE.md for why. Call once before
 * oled_show_readings(). Returns an error rather than aborting so app_main
 * can decide whether a missing/unresponsive display is fatal. */
esp_err_t oled_display_init(void);

/* Clears the framebuffer, draws "Temp: X.X°C" and "Humidity: Y.Y%", and
 * flushes the whole 128x64 buffer to the panel in one I2C transaction.
 *
 * link_up draws a square in the top-right corner: filled when the broker
 * session is up, hollow when it is not. Pass sensor_mqtt_is_connected() —
 * MQTT state rather than Wi-Fi state, since that is what decides whether
 * readings are reaching the broker, and it implies the link anyway.
 *
 * Refresh rate is whatever the caller's read cadence is (5 s at stage 17),
 * and the display is only updated after a *successful* DHT read, so the
 * marker goes stale if the sensor stops responding. */
esp_err_t oled_show_readings(float temp_c, float humidity_pct, bool link_up);

// Stage 16 Wi-Fi bring-up diagnostic, commented out along with its
// implementation and the uppercase font in oled_display.c — see the note at
// the font table there. Uncomment all three together to bring it back.
//
// /* Draws up to four lines of text, 21 characters each, and flushes. A NULL
//  * line is left blank. Uppercase renders; unsupported characters come out
//  * as blanks (see the font table in oled_display.c). */
// esp_err_t oled_show_lines(const char *l1, const char *l2, const char *l3, const char *l4);
