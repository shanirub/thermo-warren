/*
 * Stage 14 — toolchain and known-good flash.
 *
 * Deliberately minimal: a heartbeat counter over the USB Serial/JTAG
 * console, proving the build/flash/monitor loop works before any sensor,
 * Wi-Fi or MQTT code is added (stages 15-17). No DHT11 driver, no GPIO
 * reference, no network stack here by design.
 */
#include <inttypes.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "sensor_node";

/* 1000 ms: "roughly once a second" per stage 14's Definition of Done. Not
 * tied to any sensor sample rate yet — that constraint arrives at stage 15
 * with the DHT11's own minimum interval. */
#define HEARTBEAT_INTERVAL_MS 1000

void app_main(void)
{
    uint32_t counter = 0;

    while (1) {
        ESP_LOGI(TAG, "counter=%" PRIu32, counter);
        counter++;
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));
    }
}
