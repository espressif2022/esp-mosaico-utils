idf_component_register(
    SRCS "iris_bridge.c" "system_plan.c"
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS "../../main" "../../../../include"
    REQUIRES efuse esp_http_client esp-tls espressif__cjson mbedtls esp_app_format
        esp_hw_support esp_partition spi_flash app_update bootloader_support
        esp_iris nvs_flash esp_timer esp_wifi
)
