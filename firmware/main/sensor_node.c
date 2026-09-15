/*
 * Stage 15 — DHT11 sensor verification.
 * Stage 15b — adds an SSD1306 OLED display of the current reading.
 * Stage 16 — brings up Wi-Fi station mode before entering the sensor loop.
 *
 * Read the DHT11 on a fixed interval, log temperature/humidity (or the read
 * error) over USB Serial/JTAG, and show the latest reading on the OLED.
 * Wi-Fi runs itself once started — association and reconnect are handled by
 * wifi_station.c's own task and event handlers, not from this loop. No MQTT
 * and no publishing yet; that's stage 17.
 *
 * DHT read and OLED update run sequentially in this one task, never
 * concurrently — required because the DHT driver holds a ~25 ms critical
 * section per read (docs/dht-api.md) that nothing else on this single-core
 * chip can run through. Hardware I2C (oled_display.c) tolerates that
 * critical section fine when the two simply don't overlap in time.
 */
#include <stdbool.h>

#include "dht.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "oled_display.h"
#include "wifi_station.h"

static const char *TAG = "dht11";

/* GPIO6 and GPIO7 are the only free pins with no ADC function (see
 * firmware/CLAUDE.md GPIO map); GPIO7 chosen by physical layout. */
#define DHT_DATA_GPIO GPIO_NUM_7

/* The driver enforces no minimum sampling interval itself (docs/dht-api.md);
 * below the DHT11's own ~2 s floor it returns stale or failed reads. 5 s
 * leaves headroom while still giving enough samples in a short test run to
 * distinguish drift from noise. */
#define READ_INTERVAL_MS 5000

/* Onboard LED (firmware/CLAUDE.md GPIO map). Toggled once per loop
 * iteration as a console-independent heartbeat: if a run stalls after the
 * first read with no further USB Serial/JTAG output, this tells us whether
 * the loop itself is still running (LED keeps blinking) or the whole task
 * is stuck (LED freezes too) — diagnosing a suspected USB console TX stall
 * without relying on the console we don't trust yet. */
#define LED_GPIO GPIO_NUM_8

void app_main(void)
{
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);

    /* A dead/unresponsive OLED should fail loudly at startup rather than
     * leave the loop silently skipping display updates (stage 15b DoD). */
    ESP_ERROR_CHECK(oled_display_init());

    /* esp_wifi stores PHY calibration data here (nvs_enable defaults on in
     * WIFI_INIT_CONFIG_DEFAULT), and stage 16 also reads the Wi-Fi
     * credentials from it. A partition too old or too full to mount is
     * recoverable by erasing it — the credentials are re-flashed, not
     * generated, so nothing unrecoverable is lost. */
    esp_err_t nvs_err = nvs_flash_init();
    if (nvs_err == ESP_ERR_NVS_NO_FREE_PAGES || nvs_err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "erasing NVS partition: %s", esp_err_to_name(nvs_err));
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_err);

    /* Unprovisioned credentials abort here rather than leaving a board that
     * looks healthy on the OLED while silently never reaching the network. */
    ESP_ERROR_CHECK(sensor_wifi_start());

    bool led_on = false;

    while (1) {
        led_on = !led_on;
        gpio_set_level(LED_GPIO, led_on);

        float humidity = 0.0f;
        float temperature = 0.0f;

        /* Argument order is humidity, then temperature — the opposite of
         * the payload contract's field order (docs/dht-api.md). */
        esp_err_t err = dht_read_float_data(DHT_TYPE_DHT11, DHT_DATA_GPIO,
                                             &humidity, &temperature);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "temp_c=%.1f humidity_pct=%.1f", temperature, humidity);

            esp_err_t oled_err = oled_show_readings(temperature, humidity);
            if (oled_err != ESP_OK) {
                ESP_LOGW(TAG, "oled update failed: %s", esp_err_to_name(oled_err));
            }
        } else {
            ESP_LOGW(TAG, "read failed: %s", esp_err_to_name(err));
        }

        vTaskDelay(pdMS_TO_TICKS(READ_INTERVAL_MS));
    }
}
