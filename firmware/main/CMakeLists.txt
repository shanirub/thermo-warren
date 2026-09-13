idf_component_register(SRCS "sensor_node.c" "oled_display.c"
                    INCLUDE_DIRS "."
                    PRIV_REQUIRES driver esp_lcd)
