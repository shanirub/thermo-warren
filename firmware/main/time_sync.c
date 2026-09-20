/*
 * Stage 17 — SNTP time synchronisation.
 *
 * The payload contract stamps ts_ms at the publisher (src/telemetry/CLAUDE.md),
 * which is what costs the ESP32 an SNTP client. That cost was accepted
 * deliberately: stamping at the consumer instead would smear the timeline
 * during exactly the stage 8, 11 and 18 demonstrations those stages exist to
 * produce.
 *
 * Landed at stage 17 rather than 16 on purpose — stage 16's Definition of Done
 * is link state only and never mentions time.
 *
 * SNTP starts on IP_EVENT_STA_GOT_IP, never from app_main. Starting it
 * earlier is expensive in a way that is not obvious: lwIP waits a random
 * 0-5 s before its first request (CONFIG_LWIP_SNTP_STARTUP_DELAY, max 5000 ms
 * here), and if that request goes out before the interface has an address it
 * gets no reply — after which SNTP_RETRY_TIMEOUT (15 s, and *doubling* per
 * failure up to 150 s) governs the next attempt. Measured on hardware: one
 * boot synced 2.7 s after DHCP, another 14 s after, purely on whether the
 * random startup delay happened to land before or after association. The
 * slow boot cost 21 readings.
 */
#include "time_sync.h"

#include <sys/time.h>
#include <time.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"

static const char *TAG = "time";

/* A fixed pool server rather than server_from_dhcp: explicit over inherited,
 * per project convention. Whether this router advertises DHCP option 42 at
 * all is unverified, and a silent fallback to no time source would surface
 * much later as 1970 timestamps in InfluxDB rather than as an error here. */
#define NTP_SERVER "pool.ntp.org"

/* Written from the SNTP callback (lwIP's tcpip task), read from the publish
 * loop. A bool write is atomic on this target and the flag is one-way
 * false->true, so a lock would protect nothing; volatile only stops the
 * compiler caching it in the polling loop. */
static volatile bool s_synced = false;

/* Touched only from the default event loop task, so it needs no protection. */
static bool s_sntp_initialised = false;

static void on_time_synced(struct timeval *tv)
{
    /* Logged once rather than on every resync (SNTP re-polls roughly hourly),
     * so a later drift correction does not look like a reboot in the log. */
    if (!s_synced) {
        char buf[32];
        struct tm timeinfo;
        time_t now = tv->tv_sec;
        localtime_r(&now, &timeinfo);
        strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &timeinfo);
        ESP_LOGI(TAG, "clock set: %s (ts_ms=%lld)", buf,
                 (long long)((int64_t)tv->tv_sec * 1000 + tv->tv_usec / 1000));
    }
    s_synced = true;
}

static void on_got_ip(void *args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    if (!s_sntp_initialised) {
        esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(NTP_SERVER);

        /* The default config sets wait_for_sync = true, which allocates a
         * semaphore for esp_netif_sntp_sync_wait(). Nothing here blocks on
         * the sync — the publish loop polls time_sync_is_ready() instead, so
         * the DHT read and OLED update keep running while the clock is still
         * unset. */
        config.wait_for_sync = false;
        config.sync_cb = on_time_synced;
        config.start = true;    /* explicit, though it is also the default */

        esp_err_t err = esp_netif_sntp_init(&config);
        if (err != ESP_OK) {
            /* Not fatal and not retried: without a clock the publish loop
             * refuses to send rather than sending 1970 timestamps, which is
             * the safe failure and is visible in the log every second. */
            ESP_LOGE(TAG, "sntp init failed: %s", esp_err_to_name(err));
            return;
        }

        s_sntp_initialised = true;
        ESP_LOGI(TAG, "sntp started, server=%s (publishing waits for first sync)", NTP_SERVER);
        return;
    }

    /* A later GOT_IP means we have just come back from an outage. Restart the
     * service rather than leaving it alone: its retry timer may have doubled
     * its way out to 150 s while the link was down, and waiting that out with
     * a working network is the same mistake this handler exists to avoid.
     * Cheap — one extra request per reconnect. */
    esp_err_t err = esp_netif_sntp_start();
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "sntp restart after reconnect failed: %s", esp_err_to_name(err));
    }
}

esp_err_t time_sync_start(void)
{
    /* Registration only — see the file header for why the service itself
     * waits for an address. Requires esp_event_loop_create_default(), which
     * sensor_wifi_start() has already done by this point. */
    return esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               on_got_ip, NULL, NULL);
}

bool time_sync_is_ready(void)
{
    return s_synced;
}

int64_t time_sync_now_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}
