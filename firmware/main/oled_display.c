/*
 * SSD1306 OLED display, driven by ESP-IDF's native esp_lcd_panel_ssd1306
 * (esp_lcd component, ships with the toolchain — no external component).
 * Chosen over both original stage 15b candidates: esp-idf-lib has no
 * ssd1306 component, and espressif/ssd1306 (the registry package) is
 * upstream-deprecated and built on the legacy i2c_port_t driver rather
 * than the i2c_master_bus_handle_t driver this stage requires. See
 * firmware/CLAUDE.md for the full account.
 *
 * esp_lcd_panel_ssd1306 is a raw bitmap panel — it has no font or text
 * API. The 5x7 font below covers only the characters this stage's two
 * display lines actually use ("Temp: X.X°C", "Humidity: Y.Y%"), not the
 * full ASCII set — deliberately, per the project's scope-discipline rule.
 */
#include "oled_display.h"

#include <stdio.h>
#include <string.h>

#include "driver/i2c_master.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_ssd1306.h"
#include "esp_log.h"

static const char *TAG = "oled";

/* GPIO3 (SDA) / GPIO10 (SCL): already wired to the OLED per the GPIO map
 * in firmware/CLAUDE.md. I2C0, not I2C1 — nothing else claims a hardware
 * I2C bus in this project. */
#define OLED_I2C_PORT   I2C_NUM_0
#define OLED_SDA_GPIO   GPIO_NUM_3
#define OLED_SCL_GPIO   GPIO_NUM_10
#define OLED_I2C_ADDR   0x3C     /* standard SSD1306 address */
#define OLED_I2C_HZ     400000   /* Fast Mode; SSD1306 datasheet max is 400kHz */

#define OLED_WIDTH      128
#define OLED_HEIGHT     64
#define OLED_PAGES      (OLED_HEIGHT / 8)
#define FB_SIZE         (OLED_WIDTH * OLED_PAGES) /* 1024 bytes, whole-screen buffer */

static esp_lcd_panel_handle_t s_panel = NULL;
static uint8_t s_fb[FB_SIZE];

/* 5x7 font: 5 column bytes per glyph, bit 0 = top row .. bit 6 = bottom
 * row. Generated from an ASCII-art grid, not transcribed from memory —
 * see the stage's session notes for the generator. Unlisted characters
 * render as a blank cell (see font_glyph()). */
struct font_entry {
    char ch;
    uint8_t cols[5];
};

static const struct font_entry FONT[] = {
    { ' '        , { 0x00, 0x00, 0x00, 0x00, 0x00 } },
    { '.'        , { 0x00, 0x00, 0x60, 0x00, 0x00 } },
    { ':'        , { 0x00, 0x00, 0x36, 0x00, 0x00 } },
    { '%'        , { 0x61, 0x10, 0x08, 0x04, 0x43 } },
    { (char)0xB0 , { 0x02, 0x05, 0x05, 0x02, 0x00 } }, /* degree sign */
    { '0'        , { 0x3E, 0x51, 0x49, 0x45, 0x3E } },
    { '1'        , { 0x00, 0x42, 0x7F, 0x40, 0x00 } },
    { '2'        , { 0x42, 0x61, 0x51, 0x49, 0x46 } },
    { '3'        , { 0x22, 0x41, 0x49, 0x49, 0x36 } },
    { '4'        , { 0x18, 0x14, 0x12, 0x7F, 0x10 } },
    { '5'        , { 0x2F, 0x49, 0x49, 0x49, 0x31 } },
    { '6'        , { 0x3C, 0x4A, 0x49, 0x49, 0x30 } },
    { '7'        , { 0x01, 0x71, 0x09, 0x05, 0x03 } },
    { '8'        , { 0x36, 0x49, 0x49, 0x49, 0x36 } },
    { '9'        , { 0x06, 0x49, 0x49, 0x29, 0x1E } },
    { 'C'        , { 0x3E, 0x41, 0x41, 0x41, 0x22 } },
    { 'H'        , { 0x7F, 0x08, 0x08, 0x08, 0x7F } },
    { 'T'        , { 0x01, 0x01, 0x7F, 0x01, 0x01 } },
    { 'd'        , { 0x38, 0x44, 0x44, 0x44, 0x7F } },
    { 'e'        , { 0x3C, 0x4A, 0x4A, 0x4A, 0x2C } },
    { 'i'        , { 0x00, 0x44, 0x7D, 0x40, 0x00 } },
    { 'm'        , { 0x7C, 0x00, 0x3C, 0x00, 0x7C } },
    { 'p'        , { 0x78, 0x14, 0x14, 0x14, 0x08 } },
    { 't'        , { 0x00, 0x02, 0x3F, 0x42, 0x40 } },
    { 'u'        , { 0x3C, 0x40, 0x40, 0x40, 0x7C } },
    { 'y'        , { 0x1C, 0x20, 0x20, 0x20, 0x7C } },
};

static const uint8_t *font_glyph(char c)
{
    for (size_t i = 0; i < sizeof(FONT) / sizeof(FONT[0]); i++) {
        if (FONT[i].ch == c) {
            return FONT[i].cols;
        }
    }
    return FONT[0].cols; /* blank cell for anything not in this stage's charset */
}

static void fb_set_pixel(int x, int y)
{
    if (x < 0 || x >= OLED_WIDTH || y < 0 || y >= OLED_HEIGHT) {
        return;
    }
    s_fb[(y / 8) * OLED_WIDTH + x] |= (1 << (y % 8));
}

static void fb_draw_char(int x0, int y0, char c)
{
    const uint8_t *glyph = font_glyph(c);
    for (int col = 0; col < 5; col++) {
        uint8_t bits = glyph[col];
        for (int row = 0; row < 7; row++) {
            if (bits & (1 << row)) {
                fb_set_pixel(x0 + col, y0 + row);
            }
        }
    }
}

static void fb_draw_string(int x0, int y0, const char *s)
{
    int x = x0;
    for (; *s != '\0'; s++) {
        fb_draw_char(x, y0, *s);
        x += 6; /* 5px glyph + 1px spacing */
    }
}

esp_err_t oled_display_init(void)
{
    i2c_master_bus_handle_t bus = NULL;
    /* Explicit even though enable_internal_pullup matches most breakout
     * boards' own onboard pull-ups being sufficient — see project
     * convention on explicit-over-inherited. If the display is
     * unreliable, check for a board without onboard pull-ups first. */
    i2c_master_bus_config_t bus_config = {
        .i2c_port = OLED_I2C_PORT,
        .sda_io_num = OLED_SDA_GPIO,
        .scl_io_num = OLED_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t err = i2c_new_master_bus(&bus_config, &bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_new_master_bus failed: %s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_io_handle_t io = NULL;
    esp_lcd_panel_io_i2c_config_t io_config = {
        .dev_addr = OLED_I2C_ADDR,
        .scl_speed_hz = OLED_I2C_HZ,
        .control_phase_bytes = 1,   /* per SSD1306 datasheet */
        .lcd_cmd_bits = 8,          /* per SSD1306 datasheet */
        .lcd_param_bits = 8,        /* per SSD1306 datasheet */
        .dc_bit_offset = 6,         /* per SSD1306 datasheet */
    };
    err = esp_lcd_new_panel_io_i2c(bus, &io_config, &io);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_lcd_new_panel_io_i2c failed: %s", esp_err_to_name(err));
        return err;
    }

    esp_lcd_panel_ssd1306_config_t vendor_config = {
        .height = OLED_HEIGHT,
    };
    esp_lcd_panel_dev_config_t panel_config = {
        .bits_per_pixel = 1,
        .reset_gpio_num = -1, /* not wired */
        .vendor_config = &vendor_config,
    };
    err = esp_lcd_new_panel_ssd1306(io, &panel_config, &s_panel);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_lcd_new_panel_ssd1306 failed: %s", esp_err_to_name(err));
        return err;
    }

    err = esp_lcd_panel_reset(s_panel);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_lcd_panel_init(s_panel);
    if (err != ESP_OK) {
        return err;
    }
    err = esp_lcd_panel_disp_on_off(s_panel, true);
    if (err != ESP_OK) {
        return err;
    }

    ESP_LOGI(TAG, "OLED init OK — I2C0 SDA=GPIO%d SCL=GPIO%d addr=0x%02X",
             OLED_SDA_GPIO, OLED_SCL_GPIO, OLED_I2C_ADDR);
    return ESP_OK;
}

esp_err_t oled_show_readings(float temp_c, float humidity_pct)
{
    memset(s_fb, 0, sizeof(s_fb));

    char line1[24];
    char line2[24];
    snprintf(line1, sizeof(line1), "Temp: %.1f\xB0" "C", (double)temp_c);
    snprintf(line2, sizeof(line2), "Humidity: %.1f%%", (double)humidity_pct);

    /* Two lines split across the 64px height, small top margin. */
    fb_draw_string(0, 4, line1);
    fb_draw_string(0, 36, line2);

    return esp_lcd_panel_draw_bitmap(s_panel, 0, 0, OLED_WIDTH, OLED_HEIGHT, s_fb);
}
