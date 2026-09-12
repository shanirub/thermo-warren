# `esp-idf-lib/dht` — API reference

Transcribed from the component's own `dht.h` and `dht.c` (main branch, component
version 1.2.0). **This file replaces guessing or web-searching the API.** If
behaviour here contradicts the installed source under
`firmware/managed_components/`, the installed source wins — say so and stop.

Install:

```bash
idf.py add-dependency "esp-idf-lib/dht"
```

CI-verified against ESP-IDF v5.2–v6.0 on esp32c3.

## The whole API: one enum, two functions

```c
#include <dht.h>

typedef enum {
    DHT_TYPE_DHT11 = 0,   // our sensor
    DHT_TYPE_AM2301,      // DHT21, DHT22, AM2302, AM2321
    DHT_TYPE_SI7021
} dht_sensor_type_t;

esp_err_t dht_read_data(dht_sensor_type_t sensor_type, gpio_num_t pin,
                        int16_t *humidity, int16_t *temperature);

esp_err_t dht_read_float_data(dht_sensor_type_t sensor_type, gpio_num_t pin,
                              float *humidity, float *temperature);
```

There is **no init call, no handle, no device struct.** Both functions are
stateless and take the GPIO on every call.

### Argument order is humidity first, temperature second

This is the opposite of the payload contract's field order (`temp_c` then
`humidity_pct`) and the opposite of how most people say it. **Easy to transpose,
and a transposition still compiles** — both are pointers to the same type. Values
would simply be swapped at runtime and look like a sensor fault.

### Both out-parameters are nullable

Pass `NULL` for either to read only the other. At least one must be non-`NULL`
or the call returns `ESP_ERR_INVALID_ARG`.

### Return values

| Code | Meaning |
|---|---|
| `ESP_OK` | Success |
| `ESP_ERR_INVALID_ARG` | Both out-parameters were `NULL` |
| `ESP_ERR_TIMEOUT` | Sensor did not change line state in time — no sensor, bad wiring, or dead die |
| `ESP_ERR_INVALID_CRC` | 40 bits received but the checksum byte did not match |

`ESP_ERR_TIMEOUT` and `ESP_ERR_INVALID_CRC` mean different things and should be
logged distinctly. Timeout means the sensor never spoke; invalid CRC means it
spoke and the data was corrupt — the second suggests timing interference rather
than absent hardware.

The driver logs its own errors at `ESP_LOGE` with a phase name (`"problem in
phase 'B'"`, `"LOW bit timeout"`, `"Checksum failed"`), which is useful for
diagnosis but means a failing read is noisy on the console.

## Four behaviours that are not in the signatures

### 1. DHT11 readings are whole numbers only

`dht_convert_data()` for `DHT_TYPE_DHT11` does `data = msb * 10` — it uses only
the integer byte and **discards the fractional byte entirely.** Other sensor
types combine both.

So a real DHT11 read through this driver yields `24.0`, `25.0`, `62.0` — never
`24.4`. The payload contract's example value of `24.4` is reachable by the
software simulator but **not by the hardware.**

This is not a bug to fix. It is worth knowing at stage 17 so whole-number
readings are not mistaken for a broken conversion, and worth a note in the
contract's documentation.

### 2. The integer variant returns values ×10

`dht_read_data` gives humidity in percent × 10 and temperature in °C × 10:
`humidity=625` means 62.5 %, `temperature=244` means 24.4 °C.

`dht_read_float_data` divides by 10 internally and returns ordinary floats.
**Use the float variant** — the payload contract specifies floats, so it needs no
conversion. That was the reason for the contract's choice.

### 3. Each read disables interrupts for roughly 25 ms

`PORT_ENTER_CRITICAL()` wraps the *entire* fetch, including Phase A's
`ets_delay_us(20000)` — the 20 ms low pulse that starts a DHT11 transaction —
plus about 5 ms of bit reading. The driver must do this because it decodes bits
by polling the line every 2 µs and distinguishing a sub-50 µs high pulse from a
longer one; an interrupt mid-bit corrupts the reading.

Consequences to design around:

- **A read is not a cheap call.** It blocks its task and holds a critical
  section for ~25 ms.
- **The OLED cannot be driven concurrently.** It uses software (bit-banged) I²C,
  the C3 is single-core, and neither protocol tolerates preemption. Sequence
  them in one task, or guard both with a mutex.
- **Watch this at stages 16–17.** A 25 ms critical section is long, and Wi-Fi
  depends on timely interrupt service. Whether it actually disrupts the
  connection on this chip is **unverified** — do not assume either way, but if
  Wi-Fi behaves oddly once reads are running, this is the first suspect.
- Respect the DHT11's minimum sampling interval (~2 s; 1 s absolute floor).
  **The driver does not enforce it** — there is no rate limiting in the
  component at all. Reading faster returns stale or failed data.

### 4. The driver never configures a pull-up

There is no `gpio_set_pull_mode` call anywhere in `dht.c`. It uses only
`gpio_set_direction` with `GPIO_MODE_OUTPUT_OD` (open-drain, correct for a
single-wire bidirectional bus) and `GPIO_MODE_INPUT`, plus `gpio_set_level`.

So **"disable the internal pull-up" is not something this driver exposes, and
not something it needs.** It relies entirely on an external pull-up, which our
breakout provides as R1 (3.3 kΩ). The header states this requirement explicitly.

Nothing to do here — just don't go looking for a configuration option that
doesn't exist, and don't add an internal pull-up in application code.

## Minimal usage

```c
#include <dht.h>

float humidity = 0.0f, temperature = 0.0f;

// Note the order: humidity before temperature.
esp_err_t err = dht_read_float_data(DHT_TYPE_DHT11, DHT_DATA_GPIO,
                                    &humidity, &temperature);
if (err == ESP_OK) {
    ESP_LOGI(TAG, "temp_c=%.1f humidity_pct=%.1f", temperature, humidity);
} else {
    ESP_LOGW(TAG, "dht read failed: %s", esp_err_to_name(err));
}
```

Call no more than once every 2 s.
