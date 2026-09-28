#include "factory_bridge.h"
#include "esp_iris.h"
#include "factory_network.h"
#include "iris_bridge.h"
#include "sdkconfig.h"

static bool network_ready(void)
{
    factory_network_snapshot_t network;
    return factory_network_get_snapshot(&network) == ESP_OK &&
           network.state == FACTORY_NETWORK_CONNECTED && network.ip[0] != '\0';
}

static esp_err_t start(bool prefetch)
{
    if (iris_bridge_is_running()) {
        if (!prefetch) iris_bridge_set_active(true);
        return ESP_OK;
    }
    char device_id[33];
    esp_err_t err = esp_iris_format_device_id(device_id);
    if (err != ESP_OK) {
        return err;
    }
    const iris_bridge_config_t config = {
        .server_url = CONFIG_IRIS_FACTORY_BRIDGE_SERVER_URL,
        .board_id = CONFIG_IRIS_FACTORY_BRIDGE_BOARD_ID,
        .device_id = device_id,
        .enable_factory_update = true,
        .prefetch_only = prefetch,
#ifdef CONFIG_IRIS_FACTORY_BRIDGE_SYSTEM_UPDATE
        .enable_system_update = true,
#endif
#ifdef CONFIG_IRIS_FACTORY_BRIDGE_BOOTLOADER_UPDATE
        .enable_bootloader_update = true,
#endif
        .network_ready = network_ready,
    };
    return iris_bridge_start(&config);
}

esp_err_t factory_bridge_open(void) { return start(false); }
esp_err_t factory_bridge_prefetch(void) { return start(true); }
