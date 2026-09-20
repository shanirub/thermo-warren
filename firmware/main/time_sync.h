#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/* Arms SNTP and returns immediately. The service itself starts on the first
 * IP_EVENT_STA_GOT_IP, and is restarted on each later one — starting it before
 * there is an address costs a 15 s (then doubling) lwIP retry timeout. See the
 * file header in time_sync.c for the measured cost.
 *
 * Call once, after sensor_wifi_start(), which creates the default event loop
 * this registers against. The first sync arrives asynchronously afterwards. */
esp_err_t time_sync_start(void);

/* True once the clock has been set at least once this boot.
 *
 * The publish loop must gate on this: the payload contract's ts_ms is epoch
 * milliseconds, and an unsynced ESP32 boots at 1970-01-01. Publishing before
 * the first sync would write points 56 years in the past, which InfluxDB
 * would accept without complaint at stage 11. */
bool time_sync_is_ready(void);

/* Unix epoch milliseconds, matching the contract's ts_ms field.
 *
 * Milliseconds rather than seconds because InfluxDB point identity is
 * measurement + tag set + timestamp, so two points sharing a timestamp
 * overwrite rather than accumulate (src/telemetry/CLAUDE.md). Meaningless
 * before time_sync_is_ready() returns true. */
int64_t time_sync_now_ms(void);
