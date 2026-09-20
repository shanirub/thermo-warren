idf_component_register(SRCS "sensor_node.c" "oled_display.c" "wifi_station.c"
                            "time_sync.c" "mqtt_publisher.c"
                    INCLUDE_DIRS "."
                    PRIV_REQUIRES driver esp_lcd esp_wifi esp_netif esp_event
                                  nvs_flash mqtt esp_timer)
