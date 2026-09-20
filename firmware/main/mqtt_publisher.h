#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* Reads the broker endpoint and credentials from NVS (namespace "mqtt", keys
 * "host"/"port"/"user"/"password") and initialises the MQTT 5 client, but does
 * not connect: the client is started on the first IP_EVENT_STA_GOT_IP and
 * nudged to reconnect on each later one. Starting before there is an address
 * wastes a connect attempt and then the client's own ~10 s reconnect timer —
 * see the comment at the registration site in mqtt_publisher.c.
 *
 * Returns ESP_ERR_NVS_NOT_FOUND if the device has no broker credentials
 * provisioned; see firmware/README.md for the one-time flashing step. Reading
 * them here rather than in the event handler keeps an unprovisioned board
 * failing loudly at boot. Requires nvs_flash_init() and sensor_wifi_start()
 * to have run first.
 *
 * Named sensor_mqtt_* to match sensor_wifi_* — that prefix exists because
 * wifi_station_* collides with a symbol inside Espressif's closed-source
 * libnet80211.a. No such collision is known for MQTT; the prefix is for
 * consistency. */
esp_err_t sensor_mqtt_start(void);

/* Builds one contract payload and hands it to the MQTT client's outbox.
 *
 * Non-blocking: uses esp_mqtt_client_enqueue(), so the network write happens
 * on the client's own task. esp_mqtt_client_publish() would send in this
 * caller's task and is documented as possibly blocking for several seconds,
 * which would stall the DHT read and OLED update that share this loop.
 *
 * seq is owned here and increments on every call, including calls that do not
 * reach the broker. That is deliberate: a gap in the published seq series is
 * how an outage becomes visible downstream (grep -o 'seq=[0-9]*').
 *
 * Returns ESP_OK if the message was queued, ESP_ERR_INVALID_STATE if it was
 * deliberately skipped (clock not yet synced, or the offline gate is open),
 * and ESP_FAIL if the client rejected it. Every outcome is logged here with
 * its seq, so callers may ignore the return. */
esp_err_t sensor_mqtt_publish_reading(float temp_c, float humidity_pct);

/* True while an MQTT session to the broker is established. Exposed for the
 * OLED, which is the only status surface when no serial console is attached. */
bool sensor_mqtt_is_connected(void);
