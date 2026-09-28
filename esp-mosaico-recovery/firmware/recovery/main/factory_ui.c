#include "esp_attr.h"
// SPDX-License-Identifier: Apache-2.0
#include "factory_ui.h"
#include "factory_ui_input.h"
#include "vibe_ui.h"
#include "board_display.h"
#include "vibe_bundle.h"
#include "esp_gsp_esp_lcd.h"
#include "esp_check.h"
#include "esp_iris.h"
#include "esp_wifi.h"
#include "esp_timer.h"
#include "factory_bridge.h"
#include "factory_network.h"
#include "factory_nand_update.h"
#include "factory_system_update.h"
#include "iris_bridge.h"
#include "iris_screen_mirror.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "sdkconfig.h"
#include <stdio.h>
#include <string.h>

static const char *TAG = "vibe_ui";
static EXT_RAM_BSS_ATTR vibe_ui_t s_ui;
static EXT_RAM_BSS_ATTR factory_nand_update_snapshot_t s_nand;
static esp_iris_system_update_phase_t s_phase;
static factory_nand_update_state_t s_nand_state;
static uint32_t s_ota_job;
static int s_ota_state;
static uint32_t s_revision;
static uint8_t s_operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES];
static enum { UPDATE_NONE, UPDATE_OTA, UPDATE_SYSTEM, UPDATE_NAND } s_update_source;
static SemaphoreHandle_t s_initialized;
static esp_err_t s_init_result;
static bool s_init_attempted;
static QueueHandle_t s_open_requests;
static SemaphoreHandle_t s_open_lock, s_open_done;
static esp_err_t s_open_result;
static void *s_command_timer;

static int service_command(void *ctx, vibe_command_t cmd, const char *a, const char *b)
{
    (void)ctx;
    switch (cmd) {
    case VIBE_SCAN_WIFI: return factory_network_request_scan();
    case VIBE_FORGET_WIFI: return factory_network_forget();
    case VIBE_CONNECT_WIFI: return factory_network_connect(a, b);
    case VIBE_OPEN_BRIDGE: return factory_bridge_open();
    case VIBE_PREFETCH_BRIDGE: return factory_bridge_prefetch();
    case VIBE_PAUSE_BRIDGE: iris_bridge_set_active(false); return ESP_OK;
    case VIBE_STOP_BRIDGE: iris_bridge_stop(); return ESP_OK;
    case VIBE_SCAN_NAND: return factory_nand_update_request_scan();
    case VIBE_INSTALL_NAND: return factory_system_update_start_nand(a);
    default: return ESP_ERR_INVALID_ARG;
    }
}
#define COPY(field, value) snprintf(out->field, sizeof(out->field), "%s", value)
static void service_snapshot(void *ctx, vibe_snapshot_t *out)
{
    (void)ctx;
    factory_network_snapshot_t network = {0};
    esp_iris_status_t iris = {0};
    iris_bridge_snapshot_t bridge = {0};
    (void)factory_network_get_snapshot(&network);
    (void)esp_iris_get_status(&iris);
    iris_bridge_get_snapshot(&bridge);
    out->network = network.state == FACTORY_NETWORK_CONNECTED ? VIBE_NET_CONNECTED :
        network.state == FACTORY_NETWORK_CONNECTING ? VIBE_NET_CONNECTING :
        network.state == FACTORY_NETWORK_FAILED ? VIBE_NET_FAILED : VIBE_NET_OFFLINE;
    out->credentials_saved = network.credentials_saved;
    out->scanning = network.scanning;
    out->session = iris.session_ready;
    out->usb_owner = iris.session_ready && iris.transport == ESP_IRIS_TRANSPORT_KIND_USB;
    out->tcp_owner = iris.session_ready && iris.transport == ESP_IRIS_TRANSPORT_KIND_TCP;
    COPY(ssid, network.ssid); COPY(ip, network.ip); COPY(hostname, network.hostname);
    out->tcp_port = CONFIG_ESP_IRIS_TCP_PORT;
    out->token[0] = '\0';
    (void)esp_iris_pairing_token_get(out->token);
    out->scan_generation = network.scan_generation;
    out->ap_count = network.ap_count < VIBE_AP_MAX ? network.ap_count : VIBE_AP_MAX;
    for (size_t i = 0; i < out->ap_count; ++i) {
        snprintf(out->aps[i].ssid, sizeof(out->aps[i].ssid), "%s", network.aps[i].ssid);
        out->aps[i].rssi = network.aps[i].rssi;
        out->aps[i].open = network.aps[i].authmode == WIFI_AUTH_OPEN;
    }
    out->bridge_running = bridge.running;
    COPY(bridge_code, bridge.code); COPY(bridge_state, bridge.state);
    out->bridge_seconds = bridge.expires_in_ms / 1000;
    out->bridge_error = bridge.error;
    (void)factory_nand_update_get_snapshot(&s_nand);
    out->nand_generation = s_nand.generation;
    out->nand_scanning = s_nand.scan_state == FACTORY_NAND_SCAN_RUNNING;
    out->nand_error = s_nand.scan_state == FACTORY_NAND_SCAN_FAILED ? s_nand.scan_result : 0;
    out->bundle_count = s_nand.candidate_count < VIBE_BUNDLE_MAX ? s_nand.candidate_count : VIBE_BUNDLE_MAX;
    out->invalid_bundles = s_nand.invalid_count;
    for (size_t i = 0; i < out->bundle_count; ++i) {
        const factory_nand_update_candidate_t *c = &s_nand.candidates[i];
        snprintf(out->bundles[i].release, sizeof(out->bundles[i].release), "%s", c->release);
        snprintf(out->bundles[i].path, sizeof(out->bundles[i].path), "%s", c->manifest_path);
        out->bundles[i].bytes = c->total_size;
        out->bundles[i].components = c->component_count;
    }
    esp_iris_ota_status_t ota = {0};
    factory_system_update_status_t system = {0};
    (void)esp_iris_ota_get_status(&ota);
    (void)factory_system_update_get_status(&system);
    const bool nand_changed = s_nand_state != s_nand.update_state;
    const bool ota_changed = ota.job_id != s_ota_job || (int)ota.state != s_ota_state;
    const bool system_changed = s_phase != system.update.phase ||
        memcmp(s_operation_id, system.update.operation_id, sizeof(s_operation_id)) != 0;
    if (nand_changed || ota_changed || system_changed) ++s_revision;
    s_nand_state = s_nand.update_state;
    s_ota_job = ota.job_id; s_ota_state = ota.state;
    s_phase = system.update.phase;
    memcpy(s_operation_id, system.update.operation_id, sizeof(s_operation_id));
    const bool system_active = system.update.phase != ESP_IRIS_SYSTEM_UPDATE_PHASE_IDLE &&
        system.update.phase < ESP_IRIS_SYSTEM_UPDATE_PHASE_COMMITTED;
    if (nand_changed && s_nand.update_state == FACTORY_NAND_UPDATE_FAILED) s_update_source = UPDATE_NAND;
    if (ota_changed && ota.job_id) s_update_source = UPDATE_OTA;
    if (system_changed && system.update.phase != ESP_IRIS_SYSTEM_UPDATE_PHASE_IDLE) s_update_source = UPDATE_SYSTEM;
    /* Active writers supersede retained failures from all other sources. */
    if (s_nand.update_state == FACTORY_NAND_UPDATE_STARTING) s_update_source = UPDATE_NAND;
    if (ota.active) s_update_source = UPDATE_OTA;
    if (system_active) s_update_source = UPDATE_SYSTEM;
    out->updating = out->update_terminal = out->update_failed = false;
    out->progress = 0;
    memset(&out->transfer, 0, sizeof(out->transfer));
    out->transfer.now_ms = (uint64_t)esp_timer_get_time() / 1000;
    out->transfer.source = (uint8_t)s_update_source;
    out->update_revision = s_revision;
    if (s_update_source == UPDATE_NAND) {
        out->updating = s_nand.update_state == FACTORY_NAND_UPDATE_STARTING;
        out->update_terminal = out->update_failed = !out->updating;
        COPY(update_title, out->updating ? "Updating system" : "NAND update failed");
        if (out->updating) COPY(update_detail, "Opening NAND firmware bundle");
        else snprintf(out->update_detail, sizeof(out->update_detail), "Error 0x%08x - rescan or use USB", (unsigned)s_nand.update_result);
        COPY(update_owner, "NAND"); COPY(update_verified, "Validating");
        out->update_revision = s_revision;
        return;
    }
    if (s_update_source == UPDATE_OTA) {
        out->updating = ota.active || ota.state == ESP_IRIS_JOB_SUCCEEDED;
        out->update_terminal = ota.state == ESP_IRIS_JOB_FAILED || ota.state == ESP_IRIS_JOB_CANCELLED;
        out->update_failed = out->update_terminal;
        COPY(update_title, ota.active ? "Updating firmware" : out->update_failed ? "Firmware update failed" : "Firmware verified");
        if (ota.active) COPY(update_detail, ota.total_size && ota.received_size >= ota.total_size ? "Awaiting image verification" : "Receiving application");
        else if (out->update_failed) snprintf(out->update_detail, sizeof(out->update_detail), "Error 0x%08x - reconnect and retry", (unsigned)ota.result);
        else COPY(update_detail, "Restarting into the application");
        memcpy(out->transfer.job, &ota.job_id, sizeof(ota.job_id));
        out->transfer.total = ota.total_size;
        out->transfer.received = ota.received_size;
        out->transfer.receiving = ota.active && (!ota.total_size || ota.received_size < ota.total_size);
        out->progress = vibe_transfer_permille(ota.received_size, ota.total_size);
        COPY(update_owner, iris.transport == ESP_IRIS_TRANSPORT_KIND_USB ? "USB" : "TCP");
        COPY(update_verified, "SHA-256");
        return;
    }
    if (s_update_source != UPDATE_SYSTEM || system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_IDLE) return;
    out->updating = system_active;
    out->update_terminal = !system_active;
    out->update_failed = system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_FAILED || system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_CANCELLED;
    COPY(update_title, system_active ? "Updating system" : out->update_failed ? "Update failed" : "Update complete");
    const char *detail = "Validating unsigned update plan";
    if (system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_RECEIVING)
        detail = system.owner == FACTORY_SYSTEM_UPDATE_OWNER_BRIDGE ? "Downloading system component" : system.owner == FACTORY_SYSTEM_UPDATE_OWNER_NAND ? "Reading NAND system component" : "Receiving system component";
    else if (system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_COMPONENT_VERIFIED) detail = "Component verified";
    else if (system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_COMMITTING) detail = "Committing protected system images";
    else if (system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_COMMITTED) detail = "System images verified and committed";
    if (system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_RECEIVING &&
        system.update.component_size && system.update.component_received >= system.update.component_size)
        detail = "Awaiting component verification";
    COPY(update_detail, detail);
    if (out->update_failed) snprintf(out->update_detail, sizeof(out->update_detail), "Error 0x%08x - use USB to retry", (unsigned)system.update.result);
    memcpy(out->transfer.job, system.update.operation_id, sizeof(out->transfer.job));
    out->transfer.total = system.total_size;
    out->transfer.received = system.received_size;
    out->transfer.component_id = system.update.active_component_id;
    out->transfer.component_count = system.update.component_count;
    out->transfer.completed_components = system.update.completed_components;
    out->transfer.component_received = system.update.component_received;
    out->transfer.component_size = system.update.component_size;
    out->transfer.receiving = system.update.phase == ESP_IRIS_SYSTEM_UPDATE_PHASE_RECEIVING &&
        system.update.component_received < system.update.component_size;
    out->progress = vibe_transfer_permille(system.received_size, system.total_size);
    COPY(update_owner, system.owner == FACTORY_SYSTEM_UPDATE_OWNER_NAND ? "NAND" : system.owner == FACTORY_SYSTEM_UPDATE_OWNER_BRIDGE ? "Bridge" : out->usb_owner ? "USB" : "TCP");
    COPY(update_verified, "Unsigned");
}
static void process_commands(esp_gsp_handle_t ui, void *ctx)
{
    (void)ctx;
    if (!s_init_attempted) {
        s_init_attempted = true;
        const vibe_services_t services = {.snapshot = service_snapshot, .command = service_command};
        s_init_result = vibe_ui_init(&s_ui, ui, &services);
        xSemaphoreGive(s_initialized);
    }
    if (s_init_result != ESP_OK) return;
    uint8_t request;
    if (xQueueReceive(s_open_requests, &request, 0) == pdTRUE) {
        s_open_result = vibe_ui_open_bridge(&s_ui);
        xSemaphoreGive(s_open_done);
    }
}
esp_gsp_handle_t factory_ui_handle(void) { return s_ui.ui; }
esp_err_t factory_ui_start(void)
{
    esp_display_present_target_config_t display;
    ESP_RETURN_ON_ERROR(board_display_init(&display), TAG, "display");
    esp_lcd_touch_handle_t touch = NULL;
    ESP_RETURN_ON_ERROR(board_touch_init(&touch), TAG, "touch");
    ESP_RETURN_ON_ERROR(iris_screen_mirror_init(), TAG, "screen backend");
    s_open_requests = xQueueCreate(1, sizeof(uint8_t));
    s_open_lock = xSemaphoreCreateMutex();
    s_open_done = xSemaphoreCreateBinary();
    s_initialized = xSemaphoreCreateBinary();
    ESP_RETURN_ON_FALSE(s_open_requests && s_open_lock && s_open_done && s_initialized, ESP_ERR_NO_MEM, TAG, "UI command queue");
    esp_gsp_config_t config;
    ESP_RETURN_ON_ERROR(vibe_bundle_open(&config), TAG, "embedded GSP bundle");
    esp_gsp_esp_lcd_config_t lcd = ESP_GSP_ESP_LCD_CONFIG_INIT();
    lcd.display = display; lcd.touch = touch;
    esp_gsp_handle_t ui;
    ESP_RETURN_ON_ERROR(esp_gsp_esp_lcd_start(&config, &lcd, &ui), TAG, "GSP start");
    ESP_RETURN_ON_ERROR(iris_screen_mirror_attach(ui), TAG, "screen attach");
    s_command_timer = esp_gsp_timer_create(ui, 50, process_commands, NULL);
    ESP_RETURN_ON_FALSE(s_command_timer, ESP_ERR_NO_MEM, TAG, "command timer");
    xSemaphoreTake(s_initialized, portMAX_DELAY);
    ESP_RETURN_ON_ERROR(s_init_result, TAG, "Vibe UI");
    ESP_LOGI(TAG, "Vibe Mode GSP UI ready at 480x480");
    return ESP_OK;
}
esp_err_t factory_ui_open_bridge(void)
{
    if (!s_command_timer) return ESP_ERR_INVALID_STATE;
    if (xSemaphoreTake(s_open_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return ESP_ERR_TIMEOUT;
    const uint8_t request = 1;
    /* Serialize callers and keep the request storage alive until the render
     * task completes it; no timed-out stack pointer is ever queued. */
    xQueueSend(s_open_requests, &request, portMAX_DELAY);
    xSemaphoreTake(s_open_done, portMAX_DELAY);
    esp_err_t result = s_open_result;
    xSemaphoreGive(s_open_lock);
    return result;
}
