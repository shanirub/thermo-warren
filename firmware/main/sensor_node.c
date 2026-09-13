/*
 * Stage 15 — DHT11 sensor verification.
 *
 * Deliberately minimal: read the DHT11 on a fixed interval and log
 * temperature/humidity or the read error over USB Serial/JTAG. No Wi-Fi,
 * no MQTT, no persistence — that's stages 16-17.
 */
#include <stdbool.h>

#include "dht.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

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
        } else {
            ESP_LOGW(TAG, "read failed: %s", esp_err_to_name(err));
        }

        vTaskDelay(pdMS_TO_TICKS(READ_INTERVAL_MS));
    }
}
