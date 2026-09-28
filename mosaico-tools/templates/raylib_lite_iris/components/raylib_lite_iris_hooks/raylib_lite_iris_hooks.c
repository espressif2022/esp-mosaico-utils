// SPDX-License-Identifier: Apache-2.0
#include "esp_err.h"
#include "esp_iris.h"
#include "esp_log.h"
#include "iris_ota_support.h"
#include "nvs_flash.h"
#include "raylib_lite_native_hooks.h"

static const char *TAG = "iris_game";

bool raylib_lite_native_boot(void)
{
    esp_err_t error = esp_iris_boot_probe();
    if (error != ESP_OK)
        ESP_LOGW(TAG, "Iris boot probe: %s", esp_err_to_name(error));
    error = nvs_flash_init();
    if (error != ESP_OK) {
        ESP_LOGE(TAG, "initialize NVS: %s", esp_err_to_name(error));
        return false;
    }
    iris_ota_support_start();
    return true;
}

void raylib_lite_native_first_present(void)
{
    esp_err_t error = esp_iris_mark_healthy();
    if (error != ESP_OK)
        ESP_LOGW(TAG, "mark Iris application healthy: %s", esp_err_to_name(error));
}
