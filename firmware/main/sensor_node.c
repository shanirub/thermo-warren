/*
 * Stage 15  — DHT11 sensor verification.
 * Stage 15b — adds an SSD1306 OLED display of the current reading.
 * Stage 16  — brings up Wi-Fi station mode before entering the sensor loop.
 * Stage 17  — adds SNTP and the MQTT publisher; the loop now runs at 1 Hz and
 *             publishes a cached reading, replacing the software publisher.
 *
 * Still ONE task, deliberately. Stage 14 deferred "restructure into explicit
 * tasks" to whichever stage first needed real concurrency, and stage 17 does
 * not: sensor_mqtt_publish_reading() uses esp_mqtt_client_enqueue(), which
 * hands the network write to the MQTT client's own task, so a second
 * application task would buy no parallelism on this single-core chip and
 * would add a mutex around the cached reading for nothing.
 *
 * DHT read and OLED update still run sequentially and never concurrently —
 * required because the DHT driver holds a ~25 ms critical section per read
 * (docs/dht-api.md) that nothing else on this chip can run through. Stage 16
 * confirmed that critical section does not disturb Wi-Fi.
 */
#include <stdbool.h>

#include "dht.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mqtt_publisher.h"
#include "nvs_flash.h"
#include "oled_display.h"
#include "time_sync.h"
#include "wifi_station.h"

static const char *TAG = "dht11";

/* GPIO6 and GPIO7 are the only free pins with no ADC function (see
 * firmware/CLAUDE.md GPIO map); GPIO7 chosen by physical layout. */
#define DHT_DATA_GPIO GPIO_NUM_7

/* The publish rate the contract specifies. The loop period is now this, not
 * the sensor period. */
#define PUBLISH_INTERVAL_MS 1000

/* The DHT11 enforces a ~2 s minimum sampling interval and the driver does not
 * enforce it for us (docs/dht-api.md). 5 s leaves headroom, and is unchanged
 * from stage 15 so the verified read behaviour carries over: every 5th tick
 * takes a fresh sample, the other four publish the cache. A tick without a
 * fresh sample is expected behaviour, not a bug. */
#define READ_EVERY_N_TICKS 5

/* Onboard LED (firmware/CLAUDE.md GPIO map). Toggled once per sensor read
 * rather than once per tick, so it keeps the same ~5 s cadence it had at
 * stage 15 and still means "the loop is alive" at a glance. */
#define LED_GPIO GPIO_NUM_8

void app_main(void)
{
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);

    /* A dead/unresponsive OLED should fail loudly at startup rather than
     * leave the loop silently skipping display updates (stage 15b DoD). */
    ESP_ERROR_CHECK(oled_display_init());

    /* esp_wifi stores PHY calibration data here (nvs_enable defaults on in
     * WIFI_INIT_CONFIG_DEFAULT), and stages 16 and 17 read the Wi-Fi and
     * broker credentials from it. A partition too old or too full to mount is
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
     * looks healthy on the OLED while silently never reaching the network or
     * the broker. Same deliberate choice for both.
     *
     * Only the first of these actually touches the network. time_sync_start()
     * and sensor_mqtt_start() validate their configuration and then register
     * for IP_EVENT_STA_GOT_IP — neither SNTP nor MQTT is started until there
     * is an address to use. Both were previously started here, and both paid
     * a retry timeout for it: 15 s and doubling for SNTP, ~10 s for MQTT,
     * measured at 5 to 21 lost readings per boot. Order still matters:
     * sensor_wifi_start() creates the default event loop the other two
     * register against. */
    ESP_ERROR_CHECK(sensor_wifi_start());
    ESP_ERROR_CHECK(time_sync_start());
    ESP_ERROR_CHECK(sensor_mqtt_start());

    bool led_on = false;
    unsigned tick = 0;

    /* Last good reading. Published every tick; refreshed every 5th. Not valid
     * until the first successful read, so publishing waits for it — sending a
     * zeroed reading would be indistinguishable from a real 0.0 °C. */
    float cached_temp_c = 0.0f;
    float cached_humidity_pct = 0.0f;
    bool have_reading = false;

    while (1) {
        if (tick % READ_EVERY_N_TICKS == 0) {
            led_on = !led_on;
            gpio_set_level(LED_GPIO, led_on);

            float humidity = 0.0f;
            float temperature = 0.0f;

            /* Argument order is humidity, then temperature — the opposite of
             * the payload contract's field order (docs/dht-api.md). */
            esp_err_t err = dht_read_float_data(DHT_TYPE_DHT11, DHT_DATA_GPIO,
                                                 &humidity, &temperature);
            if (err == ESP_OK) {
                cached_temp_c = temperature;
                cached_humidity_pct = humidity;
                have_reading = true;

                ESP_LOGI(TAG, "temp_c=%.1f humidity_pct=%.1f", temperature, humidity);

                esp_err_t oled_err = oled_show_readings(temperature, humidity,
                                                       sensor_mqtt_is_connected());
                if (oled_err != ESP_OK) {
                    ESP_LOGW(TAG, "oled update failed: %s", esp_err_to_name(oled_err));
                }
            } else {
                /* The cache deliberately survives a failed read: the publish
                 * loop keeps sending the last good value, which is what
                 * "publish the cached reading" means. A run of these in the
                 * log is the signal that the sensor, not the link, is at
                 * fault. */
                ESP_LOGW(TAG, "read failed: %s", esp_err_to_name(err));
            }
        }

        if (have_reading) {
            /* Return ignored: every outcome, including each deliberate drop,
             * is already logged inside the publisher with its seq. */
            (void)sensor_mqtt_publish_reading(cached_temp_c, cached_humidity_pct);
        }

        tick++;
        vTaskDelay(pdMS_TO_TICKS(PUBLISH_INTERVAL_MS));
    }
}
