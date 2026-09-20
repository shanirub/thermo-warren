/*
 * Stage 17 — MQTT publisher.
 *
 * Publishes the cached DHT11 reading to RabbitMQ's MQTT listener at 1 Hz,
 * QoS 1, MQTT 5.0, against the payload contract frozen at stage 5. The
 * contract is not open to renegotiation here: if a consumer would need
 * changing to accept these messages, this file is wrong.
 *
 * The outage policy is the substance of this module, and it exists because
 * ESP-MQTT's defaults lose data silently. Verified in the v5.5.5 source:
 * the outbox has no message-count limit (outbox.limit is a *byte* cap,
 * default 0 = unbounded), its real bound is a per-message age of 30 s
 * (OUTBOX_EXPIRED_TIMEOUT_MS), that expiry runs every task-loop iteration
 * whether connected or not, and it runs *before* the resend step — so a
 * message can be discarded even though the link came back in that same
 * iteration. Stage 16 measured worst-case Wi-Fi reconnect at ~30 s plus
 * connect time, so the default expiry loses that race, invisibly.
 *
 * The policy: ONE bound decides what is lost -- this file's 60 s offline gate.
 *
 *   1. Offline gate, 60 s        -- the data policy. Bounds how many readings
 *                                   enter the outbox. Every drop is ours, taken
 *                                   at a known moment, logged with its seq.
 *   2. Outbox expiry, 1 h        -- memory backstop only (sdkconfig.defaults).
 *                                   Should never fire; if it does, say so.
 *   3. outbox.limit, 16 KB       -- heap guard; has never bound.
 *
 * Layers 2 and 3 are deliberately set far out of reach so that nothing the gate
 * admitted is ever dropped by the outbox. An earlier version used a 120 s expiry
 * and claimed the gate would keep it from firing, which was wrong: each queued
 * message's fuse runs from its own enqueue, so the gate bounds the backlog's
 * count and never its age. A 139 s outage duly expired seq 38-66 while seq 67
 * survived by 420 ms.
 *
 * Why the firmware should not drop its own backlog: telemetry.observe already
 * carries x-max-length 100 with drop-head while telemetry.store is unbounded
 * with a DLX. The broker is already the thing that drops, and the asymmetry
 * between those two fates is the stated lesson. Delivering everything buffered
 * lets that asymmetry be observed; dropping it here would send both queues
 * identically truncated data.
 *
 * Keeping the gate as the sole bound also keeps the loss record trustworthy. The
 * expiry cannot report accurately on this path: outbox_set_tick() is called only
 * in the publish() write path, never in the resend path used by enqueue(), so a
 * message keeps its original enqueue timestamp through transmission and can be
 * deleted *after* the broker already has it.
 *
 * Also measured, and not anticipated: the Wi-Fi driver reported an outage 8.4 s
 * after the link actually died (reason=200, beacon timeout). For those 8 s
 * s_connected was still true, so the gate did not apply and publishes logged as
 * ordinary "queued seq=N" while nothing was reaching the broker. The gate's
 * clock starts when the driver notices, not when the link fails.
 */
#include "mqtt_publisher.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
/* mqtt_client.h only, never mqtt5_client.h directly: the two include each
 * other, and with CONFIG_MQTT_PROTOCOL_5=y mqtt_client.h pulls in the MQTT 5
 * header itself. Including mqtt5_client.h first wins the include guard and
 * leaves mqtt_client.h compiling against types it has not seen yet
 * ("unknown type name 'esp_mqtt5_event_property_t'"), which reads as a
 * missing dependency rather than an ordering problem. */
#include "mqtt_client.h"
#include "nvs.h"
#include "time_sync.h"

static const char *TAG = "mqtt";

#define MQTT_NVS_NAMESPACE "mqtt"
#define MQTT_NVS_KEY_HOST  "host"
#define MQTT_NVS_KEY_PORT  "port"
#define MQTT_NVS_KEY_USER  "user"
#define MQTT_NVS_KEY_PASS  "password"

/* Must match topology_spec.py: the MQTT topic becomes the AMQP routing key
 * with '/' replaced by '.', and a topic exchange with no matching binding
 * discards messages silently -- no error, no failed PUBACK, nothing in the
 * broker log. A typo here produces two permanently empty queues. */
#define MQTT_TOPIC "sensors/esp32c3/telemetry"

/* Both must differ from the software publisher's values (telemetry-sim /
 * sim-01, src/telemetry/config.py). Two MQTT clients sharing one client id
 * makes the broker disconnect the first, and the resulting flapping is hard
 * to diagnose from the log alone. */
#define MQTT_CLIENT_ID "sensor-node-01"
#define DEVICE_ID      "esp32c3-01"

/* QoS 1 is a locked decision, chosen for what it does inside the broker
 * rather than on the wire: QoS 1 publishes become *persistent* AMQP messages,
 * QoS 0 become transient, and transient messages in a durable queue vanish on
 * broker restart -- which would make stage 18's resilience tests lie. */
#define PUBLISH_QOS    1
#define PUBLISH_RETAIN 0    /* explicit: retained telemetry would serve a stale reading to any future subscriber */

#define CONTENT_TYPE_JSON "application/json"

/* Stop enqueueing once offline this long: the whole data policy, and now the
 * only bound that drops anything. At 1 Hz it buffers ~60 readings (~9 KB
 * encoded) and everything after is dropped at source, named and logged.
 *
 * Note what is preserved: the gate cannot evict, and the outbox is FIFO, so the
 * 60 s kept is the FIRST minute of the outage, not the most recent. That is the
 * better half of the trade here -- it is contiguous with the pre-outage series,
 * so a replay leaves no hole between what was delivered live and what arrived
 * late, which is what a time-series store wants.
 *
 * This clock starts when the MQTT session is reported down, which the beacon
 * timeout delayed by 8.4 s in the measured run -- so the real budget before
 * readings are dropped at source is 60 s plus however long the driver takes to
 * notice. */
#define OFFLINE_GATE_MS 60000

/* Byte cap on the outbox, not a message count -- the field is documented in
 * bytes and defaults to 0, which means unbounded. ~2x what the gate above can
 * accumulate, so it never binds in normal operation and acts purely as a heap
 * guard. If it ever does bind, enqueue() returns -2 and says so. */
#define OUTBOX_LIMIT_BYTES 16384

/* Room for the gate's worth of in-flight messages plus slack. Overflowing
 * this costs only the msg_id->seq name in a later log line, never a message. */
#define PENDING_MAP_SIZE 96

/* {"seq":4294967295,"device":"esp32c3-01","temp_c":-40.0,"humidity_pct":100.0,
 *  "ts_ms":9999999999999} is ~115 bytes; 192 leaves headroom and truncation
 * is checked at build time of each payload anyway. */
#define PAYLOAD_BUF_SIZE 192

typedef struct {
    int msg_id;     /* 0 = free slot; MQTT packet identifiers start at 1 */
    uint32_t seq;
} pending_entry_t;

static esp_mqtt_client_handle_t s_client = NULL;
static uint32_t s_seq = 0;

/* Touched only from the default event loop task (on_got_ip), so it needs no
 * protection. Guards esp_mqtt_client_start(), which must run exactly once —
 * IP_EVENT_STA_GOT_IP fires again on every reconnect. */
static bool s_started = false;

/* Written by the event handler (MQTT client task), read by the publish loop
 * (main task). */
static volatile bool s_connected = false;
static volatile int64_t s_offline_since_ms = 0;

static pending_entry_t s_pending[PENDING_MAP_SIZE];
/* The map is touched from two tasks -- the publish loop adds, the event
 * handler removes -- so it needs a real lock, unlike the one-way bool flags
 * above. Held only across a bounded scan of a small array, never across a
 * network call or a log line. */
static SemaphoreHandle_t s_pending_lock = NULL;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void pending_put(int msg_id, uint32_t seq)
{
    if (msg_id <= 0 || s_pending_lock == NULL) {
        return;
    }
    xSemaphoreTake(s_pending_lock, portMAX_DELAY);
    for (size_t i = 0; i < PENDING_MAP_SIZE; i++) {
        if (s_pending[i].msg_id == 0) {
            s_pending[i].msg_id = msg_id;
            s_pending[i].seq = seq;
            xSemaphoreGive(s_pending_lock);
            return;
        }
    }
    xSemaphoreGive(s_pending_lock);
    ESP_LOGW(TAG, "pending map full, seq=%" PRIu32 " will be unnamed in its ack", seq);
}

/* Returns the seq for this msg_id and frees the slot, or 0 if unknown. */
static uint32_t pending_take(int msg_id)
{
    if (msg_id <= 0 || s_pending_lock == NULL) {
        return 0;
    }
    uint32_t seq = 0;
    xSemaphoreTake(s_pending_lock, portMAX_DELAY);
    for (size_t i = 0; i < PENDING_MAP_SIZE; i++) {
        if (s_pending[i].msg_id == msg_id) {
            seq = s_pending[i].seq;
            s_pending[i].msg_id = 0;
            break;
        }
    }
    xSemaphoreGive(s_pending_lock);
    return seq;
}

static void handle_published(esp_mqtt_event_handle_t event)
{
    uint32_t seq = pending_take(event->msg_id);

    /* The MQTT 5 PUBACK reason code does NOT arrive as a reason_code field --
     * esp_mqtt_event_t has none. esp_mqtt5_parse_puback() points event->data
     * at the reason-code byte with data_len == 1 before dispatching this
     * event; data_len == 0 means the PUBACK omitted the code, which MQTT 5
     * permits and which means success.
     *
     * This check is the whole reason the contract specifies MQTT 5 over
     * 3.1.1: MQTT_EVENT_PUBLISHED is dispatched regardless of the code, so
     * without reading these bytes an unroutable publish is indistinguishable
     * from a delivered one. RabbitMQ sends 16 when it could not route to any
     * queue and 131 for implementation-specific errors. This mirrors
     * _on_publish() in src/telemetry/publisher.py. */
    int reason = 0;
    if (event->data_len == 1 && event->data != NULL) {
        reason = (uint8_t)event->data[0];
    }

    if (reason != 0) {
        ESP_LOGE(TAG, "broker refused publish: seq=%" PRIu32 " reason=%d", seq, reason);
    } else {
        ESP_LOGI(TAG, "acked seq=%" PRIu32, seq);
    }
}

static void mqtt_event_handler(void *args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;

    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        s_connected = true;
        ESP_LOGI(TAG, "connected to broker, client_id=%s", MQTT_CLIENT_ID);
        break;

    case MQTT_EVENT_DISCONNECTED:
        /* Only start the gate's clock on the transition, so a run of
         * disconnect events during a long outage does not keep resetting it. */
        if (s_connected) {
            s_offline_since_ms = now_ms();
        }
        s_connected = false;
        ESP_LOGW(TAG, "disconnected from broker");
        break;

    case MQTT_EVENT_PUBLISHED:
        handle_published(event);
        break;

    case MQTT_EVENT_DELETED: {
        /* Only reachable with CONFIG_MQTT_REPORT_DELETED_MESSAGES=y: the
         * outbox expiry firing.
         *
         * With the fuse at 1 h this should never happen. If it does, it means
         * either an outage longer than an hour or a message stuck unacked for
         * one -- both genuinely worth investigating, unlike under the old 120 s
         * fuse where this fired routinely.
         *
         * Can also be a false positive. On the enqueue path the expiry clock is
         * never reset after transmission, so a message the broker already has
         * can be deleted while its PUBACK is in flight. */
        uint32_t seq = pending_take(event->msg_id);
        ESP_LOGE(TAG, "outbox expiry dropped seq=%" PRIu32
                      " — unacked for 1 h, should not happen", seq);
        break;
    }

    case MQTT_EVENT_ERROR:
        if (event->error_handle != NULL) {
            ESP_LOGE(TAG, "mqtt error: type=%d connect_return_code=%d sock_errno=%d",
                     event->error_handle->error_type,
                     event->error_handle->connect_return_code,
                     event->error_handle->esp_transport_sock_errno);
        } else {
            ESP_LOGE(TAG, "mqtt error with no error handle");
        }
        break;

    default:
        break;
    }
}

static void on_got_ip(void *args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    /* Both branches are non-blocking, which is what makes it safe to do this
     * on the default event loop task. Stage 16 moved Wi-Fi's reconnect backoff
     * off this task precisely because sleeping here stalls delivery of every
     * other event; nothing below sleeps. */
    if (!s_started) {
        esp_err_t err = esp_mqtt_client_start(s_client);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "esp_mqtt_client_start failed: %s", esp_err_to_name(err));
            return;
        }
        s_started = true;
        ESP_LOGI(TAG, "mqtt client started (address acquired)");
        return;
    }

    /* Back from an outage. Without this nudge the client sits out the rest of
     * its own reconnect timer even though the network is already up: measured
     * at 13 s and 12 lost readings on the stage 17 outage test, with DHCP
     * complete at t=339919 ms and the broker not reached until t=352899 ms.
     *
     * Guarded on !s_connected because a GOT_IP that arrives while the session
     * is somehow still live has nothing to reconnect. */
    if (!s_connected) {
        esp_err_t err = esp_mqtt_client_reconnect(s_client);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "reconnect nudge failed: %s", esp_err_to_name(err));
        } else {
            ESP_LOGI(TAG, "address reacquired, reconnecting now");
        }
    }
}

static esp_err_t load_credentials(char *host, size_t host_len, uint16_t *port,
                                  char *user, size_t user_len,
                                  char *pass, size_t pass_len)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(MQTT_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "no '%s' namespace in NVS: %s — device not provisioned, "
                      "see firmware/README.md", MQTT_NVS_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    size_t len = host_len;
    err = nvs_get_str(handle, MQTT_NVS_KEY_HOST, host, &len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "reading '%s' failed: %s", MQTT_NVS_KEY_HOST, esp_err_to_name(err));
        goto out;
    }

    /* u16 rather than a string so a malformed value fails here naming the
     * key, instead of silently becoming port 0 in a conversion. */
    err = nvs_get_u16(handle, MQTT_NVS_KEY_PORT, port);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "reading '%s' failed: %s", MQTT_NVS_KEY_PORT, esp_err_to_name(err));
        goto out;
    }

    len = user_len;
    err = nvs_get_str(handle, MQTT_NVS_KEY_USER, user, &len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "reading '%s' failed: %s", MQTT_NVS_KEY_USER, esp_err_to_name(err));
        goto out;
    }

    len = pass_len;
    err = nvs_get_str(handle, MQTT_NVS_KEY_PASS, pass, &len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "reading '%s' failed: %s", MQTT_NVS_KEY_PASS, esp_err_to_name(err));
        goto out;
    }

out:
    nvs_close(handle);
    if (err == ESP_OK) {
        /* Password length only, never the value -- enough to spot a truncated
         * or empty NVS read without leaking it, same as wifi_station.c. */
        ESP_LOGI(TAG, "broker credentials loaded from NVS, host=%s port=%u user=%s pass_len=%u",
                 host, (unsigned)*port, user, (unsigned)strlen(pass));
    }
    return err;
}

esp_err_t sensor_mqtt_start(void)
{
    /* Sized to the NVS string limits that matter here rather than to
     * MQTT_MAX_*: a hostname or IPv4 literal, and RabbitMQ credentials. */
    static char host[64];
    static char user[64];
    static char pass[64];
    uint16_t port = 0;

    esp_err_t err = load_credentials(host, sizeof(host), &port,
                                     user, sizeof(user), pass, sizeof(pass));
    if (err != ESP_OK) {
        return err;
    }

    s_pending_lock = xSemaphoreCreateMutex();
    if (s_pending_lock == NULL) {
        ESP_LOGE(TAG, "could not create pending map mutex");
        return ESP_ERR_NO_MEM;
    }

    /* Gate's clock starts at boot, not at the first disconnect, so a board
     * that never reaches the broker at all stops enqueueing after 60 s
     * instead of filling the outbox until the expiry takes over. */
    s_offline_since_ms = now_ms();

    esp_mqtt_client_config_t config = {
        .broker = {
            .address = {
                .hostname = host,
                .port = port,
                /* Explicit though it is also the default: TLS on the MQTT
                 * listener is deliberately out of scope for this project. */
                .transport = MQTT_TRANSPORT_OVER_TCP,
            },
        },
        .credentials = {
            .username = user,
            .client_id = MQTT_CLIENT_ID,
            .authentication = { .password = pass },
        },
        .session = {
            /* The contract specifies 5.0. Also requires
             * CONFIG_MQTT_PROTOCOL_5=y, without which mqtt5_client.c is not
             * even compiled in and the client silently speaks 3.1.1 -- which
             * has no error channel at all, so the PUBACK reason code above
             * would never arrive. */
            .protocol_ver = MQTT_PROTOCOL_V_5,
        },
        .outbox = { .limit = OUTBOX_LIMIT_BYTES },
    };

    s_client = esp_mqtt_client_init(&config);
    if (s_client == NULL) {
        ESP_LOGE(TAG, "esp_mqtt_client_init failed");
        return ESP_FAIL;
    }

    err = esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "registering event handler failed: %s", esp_err_to_name(err));
        return err;
    }

    /* The client is initialised here but deliberately NOT started here: it is
     * started from on_got_ip(). Starting before the interface has an address
     * burns a connect attempt into a dead route ("esp-tls: connect() error:
     * Host is unreachable" at t=389 ms on hardware) and then waits out the
     * client's own ~10 s reconnect timer, which cost 13 s and 13 readings at
     * every boot.
     *
     * Credential loading stays above, at boot, rather than moving into the
     * handler: an unprovisioned board must still panic through
     * ESP_ERROR_CHECK at startup, the same deliberate choice stage 16 made
     * for Wi-Fi, instead of looking healthy until the first GOT_IP. */
    err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                              on_got_ip, NULL, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "registering ip event handler failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "mqtt client ready, broker=%s:%u topic=%s qos=%d (starts on address acquired)",
             host, (unsigned)port, MQTT_TOPIC, PUBLISH_QOS);
    return ESP_OK;
}

bool sensor_mqtt_is_connected(void)
{
    return s_connected;
}

esp_err_t sensor_mqtt_publish_reading(float temp_c, float humidity_pct)
{
    if (s_client == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    /* seq advances even when the reading is not sent, so a hole in the
     * published series is exactly the set of readings that were lost. */
    uint32_t seq = ++s_seq;

    if (!time_sync_is_ready()) {
        ESP_LOGW(TAG, "skipping seq=%" PRIu32 ": clock not synced yet", seq);
        return ESP_ERR_INVALID_STATE;
    }

    if (!s_connected) {
        int64_t offline_ms = now_ms() - s_offline_since_ms;
        if (offline_ms > OFFLINE_GATE_MS) {
            ESP_LOGW(TAG, "dropping seq=%" PRIu32 ": offline %" PRId64 " s, past the %d s gate",
                     seq, offline_ms / 1000, OFFLINE_GATE_MS / 1000);
            return ESP_ERR_INVALID_STATE;
        }
    }

    /* Field order matches the contract in src/telemetry/CLAUDE.md. Note that
     * dht_read_float_data() returns humidity *first*, the reverse of this
     * order -- the swap happens at the call site in sensor_node.c.
     *
     * %.1f because DHT11 readings are whole numbers: the driver discards the
     * fractional byte for this sensor type, so these are 24.0, never 24.4.
     * The type stays float to match the contract and the simulator. */
    char payload[PAYLOAD_BUF_SIZE];
    int written = snprintf(payload, sizeof(payload),
                           "{\"seq\":%" PRIu32 ",\"device\":\"%s\",\"temp_c\":%.1f,"
                           "\"humidity_pct\":%.1f,\"ts_ms\":%" PRId64 "}",
                           seq, DEVICE_ID, temp_c, humidity_pct, time_sync_now_ms());
    if (written < 0 || written >= (int)sizeof(payload)) {
        ESP_LOGE(TAG, "payload truncated for seq=%" PRIu32 ", not sending", seq);
        return ESP_FAIL;
    }

    /* One-shot property, consumed by the next publish/enqueue and then
     * cleared by make_publish(). Whether RabbitMQ's MQTT plugin maps this
     * through to the AMQP content_type field is unverified; nothing here
     * depends on it, and the simulator sets it too, so the contract matches. */
    esp_mqtt5_publish_property_config_t publish_property = {
        .content_type = CONTENT_TYPE_JSON,
    };
    esp_err_t prop_err = esp_mqtt5_client_set_publish_property(s_client, &publish_property);
    if (prop_err != ESP_OK) {
        ESP_LOGW(TAG, "could not set content type for seq=%" PRIu32 ": %s",
                 seq, esp_err_to_name(prop_err));
    }

    /* store=false is correct for QoS 1: only QoS 0 needs the flag to be
     * enqueued at all. */
    int msg_id = esp_mqtt_client_enqueue(s_client, MQTT_TOPIC, payload, written,
                                         PUBLISH_QOS, PUBLISH_RETAIN, false);

    /* Three distinct outcomes, logged separately -- lumping them into
     * "publish failed" would hide which end of the queue lost data. -2 is the
     * outbox byte limit and -1 is the MQTT 5 in-flight cap (unacked QoS>0
     * count above the broker's advertised Receive Maximum); both discard the
     * *new* message, the opposite end from the expiry, which discards the
     * oldest. */
    if (msg_id == -2) {
        ESP_LOGE(TAG, "dropping seq=%" PRIu32 ": outbox at its %d byte limit", seq, OUTBOX_LIMIT_BYTES);
        return ESP_FAIL;
    }
    if (msg_id < 0) {
        ESP_LOGE(TAG, "dropping seq=%" PRIu32 ": enqueue rejected (in-flight cap or client state)", seq);
        return ESP_FAIL;
    }

    pending_put(msg_id, seq);
    ESP_LOGI(TAG, "queued seq=%" PRIu32 " msg_id=%d%s", seq, msg_id,
             s_connected ? "" : " (offline, within gate)");
    return ESP_OK;
}
