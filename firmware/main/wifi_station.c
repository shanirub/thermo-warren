/*
 * Stage 16 — Wi-Fi station mode with automatic reconnect.
 *
 * Credentials live in NVS (namespace "wifi", keys "ssid"/"password"),
 * written once by flashing a generated NVS image — they are never
 * compiled into the app binary and never enter the source tree. See
 * firmware/README.md for the provisioning procedure.
 *
 * Reconnect runs in its own task rather than in the disconnect event
 * handler: the handler executes on the default event loop task, so
 * sleeping there to back off would stall delivery of every other event on
 * that loop. esp_timer was the other candidate and was rejected —
 * esp_timer.h asks for callbacks lasting "a few microseconds", dispatched
 * from a single shared high-priority task, which esp_wifi_connect() is not
 * a good fit for.
 */
#include "wifi_station.h"

#include <inttypes.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"

static const char *TAG = "wifi";

#define WIFI_NVS_NAMESPACE "wifi"
#define WIFI_NVS_KEY_SSID  "ssid"
#define WIFI_NVS_KEY_PASS  "password"

/* Exponential backoff between reconnect attempts. Starts at 1 s so a brief
 * blip recovers almost immediately; caps at 30 s so a router that is off
 * for a long while is retried indefinitely without hammering the radio.
 * The DoD requires rejoining after an AP power-cycle of unknown duration,
 * so there is deliberately no attempt limit. */
#define RECONNECT_DELAY_MIN_MS 1000
#define RECONNECT_DELAY_MAX_MS 30000

#define RECONNECT_TASK_STACK    3072
#define RECONNECT_TASK_PRIORITY 5

static TaskHandle_t s_reconnect_task = NULL;
static uint32_t s_reconnect_delay_ms = RECONNECT_DELAY_MIN_MS;
static unsigned s_attempts = 0;

static void reconnect_task(void *arg)
{
    while (1) {
        /* Blocks until station-start or a disconnect asks for an attempt. */
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);

        if (s_attempts > 0) {
            vTaskDelay(pdMS_TO_TICKS(s_reconnect_delay_ms));
        }
        s_attempts++;

        ESP_LOGI(TAG, "connect attempt %u (backoff %" PRIu32 " ms)",
                 s_attempts, s_reconnect_delay_ms);
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "esp_wifi_connect failed: %s", esp_err_to_name(err));
        }

        s_reconnect_delay_ms *= 2;
        if (s_reconnect_delay_ms > RECONNECT_DELAY_MAX_MS) {
            s_reconnect_delay_ms = RECONNECT_DELAY_MAX_MS;
        }
    }
}

static void wifi_event_handler(void *arg, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    if (event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "station started");
        xTaskNotifyGive(s_reconnect_task);
    } else if (event_id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *ev = event_data;
        /* Reason codes are in esp_wifi_types_generic.h. Two seen during
         * stage 16 bring-up and worth recognising: 202 (AUTH_FAIL) is the
         * AP actively rejecting us — on this network, a MAC address absent
         * from the router's whitelist — and 2 (AUTH_EXPIRE) is the
         * handshake timing out, which a defective radio also produces. */
        ESP_LOGW(TAG, "disconnected, reason=%d", ev->reason);
        xTaskNotifyGive(s_reconnect_task);
    }
}

static void ip_event_handler(void *arg, esp_event_base_t base,
                             int32_t event_id, void *event_data)
{
    if (event_id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *ev = event_data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&ev->ip_info.ip));
        /* Association succeeded, so the next outage starts backing off
         * from the bottom again rather than from wherever it left off. */
        s_reconnect_delay_ms = RECONNECT_DELAY_MIN_MS;
    }
}

static esp_err_t load_credentials(wifi_config_t *config)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(WIFI_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "no '%s' namespace in NVS: %s — device not provisioned, "
                      "see firmware/README.md", WIFI_NVS_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    size_t len = sizeof(config->sta.ssid);
    err = nvs_get_str(handle, WIFI_NVS_KEY_SSID, (char *)config->sta.ssid, &len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "reading '%s' failed: %s", WIFI_NVS_KEY_SSID, esp_err_to_name(err));
        nvs_close(handle);
        return err;
    }

    len = sizeof(config->sta.password);
    err = nvs_get_str(handle, WIFI_NVS_KEY_PASS, (char *)config->sta.password, &len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "reading '%s' failed: %s", WIFI_NVS_KEY_PASS, esp_err_to_name(err));
        nvs_close(handle);
        return err;
    }

    nvs_close(handle);
    /* SSID is logged, password deliberately is not — only its length, which is
     * enough to spot a truncated or empty NVS read without leaking the value. */
    ESP_LOGI(TAG, "credentials loaded from NVS, ssid=%s pass_len=%u",
             (char *)config->sta.ssid, (unsigned)strlen((char *)config->sta.password));
    return ESP_OK;
}

esp_err_t sensor_wifi_start(void)
{
    wifi_config_t wifi_config = {
        .sta = {
            /* Explicit rather than inherited, per project convention:
             * WPA2-PSK is what this network uses; an AP offering anything
             * weaker should fail loudly instead of silently downgrading. */
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };

    esp_err_t err = load_credentials(&wifi_config);
    if (err != ESP_OK) {
        return err;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_config));

    if (xTaskCreate(reconnect_task, "wifi_reconnect", RECONNECT_TASK_STACK, NULL,
                    RECONNECT_TASK_PRIORITY, &s_reconnect_task) != pdPASS) {
        ESP_LOGE(TAG, "could not create reconnect task");
        return ESP_ERR_NO_MEM;
    }

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        ip_event_handler, NULL, NULL));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    return ESP_OK;
}
