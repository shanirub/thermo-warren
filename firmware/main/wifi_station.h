#pragma once

#include "esp_err.h"

/* Reads the SSID/password from NVS (namespace "wifi"), brings up station
 * mode, and starts connecting. Returns as soon as the connect attempt is
 * under way — association is asynchronous, reported via the event handlers.
 *
 * Returns ESP_ERR_NVS_NOT_FOUND if the device has no credentials
 * provisioned; see firmware/README.md for the one-time flashing step.
 * Requires nvs_flash_init() to have succeeded first.
 *
 * Named sensor_wifi_* rather than wifi_station_*: the latter collides at
 * link time with a symbol inside Espressif's closed-source libnet80211.a
 * blob, which defines its own wifi_station_start(). */
esp_err_t sensor_wifi_start(void);
