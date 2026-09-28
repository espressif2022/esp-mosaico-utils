// SPDX-License-Identifier: Apache-2.0
#include "gsp_sim_bridge.h"
#include "vibe_ui.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#endif

static vibe_ui_t state;
static vibe_snapshot_t model;
static unsigned connect_ticks;
static unsigned connect_attempts, bridge_open_calls;
static unsigned bridge_prefetch_calls;
static bool bridge_active;
static bool connecting, installing;
static bool renewed_code;
static void *trace_timer;
static bool scenario(const char *name)
{
    const char *selected = getenv("VIBE_SIM_SCENARIO");
    return selected && !strcmp(selected, name);
}
static void trace_state(esp_gsp_handle_t ui, void *ctx)
{
    (void)ui; (void)ctx;
    const char *control = getenv("VIBE_SIM_CONTROL_FILE");
    FILE *request = control ? fopen(control, "r") : NULL;
    if (request) {
        char command[256] = {0};
        (void)fgets(command, sizeof(command), request);
        fclose(request); remove(control);
        if (!strcmp(command, "open-bridge")) (void)vibe_ui_open_bridge(&state);
        if (!strcmp(command, "expire-code")) renewed_code = true;
        unsigned long long ms, received, total;
        unsigned component, count, completed, receiving, job, component_received, component_size, terminal;
        if (sscanf(command, "transfer %llu %llu %llu %u %u %u %u %u %u %u %u",
                   &ms, &received, &total, &component, &count, &completed, &receiving, &job,
                   &component_received, &component_size, &terminal) == 11) {
            model.transfer = (vibe_transfer_t){.now_ms=ms, .received=received, .total=total,
                .component_id=component, .component_count=count, .completed_components=completed,
                .receiving=receiving != 0, .component_received=component_received,
                .component_size=component_size, .source=2};
            model.transfer.job[0] = (uint8_t)job;
            model.progress = vibe_transfer_permille(received, total);
            model.updating = terminal == 0;
            model.update_terminal = terminal != 0;
            model.update_failed = terminal == 2;
            ++model.update_revision;
            snprintf(model.update_title, sizeof(model.update_title), "%s", terminal == 2 ? "Update failed" : terminal ? "Update complete" : "Updating system");
            snprintf(model.update_detail, sizeof(model.update_detail), "%s", terminal == 2 ? "Connection interrupted - retry" : terminal ? "System images verified and committed" : receiving ? "Downloading system component" : completed == count ? "Committing protected system images" : "Component verified");
            snprintf(model.update_owner, sizeof(model.update_owner), "Bridge");
            snprintf(model.update_verified, sizeof(model.update_verified), "Unsigned");
            vibe_ui_poll(&state);
        }
    }
    const char *path = getenv("VIBE_SIM_STATE_FILE");
    if (!path || !path[0]) return;
    char temporary[1024];
    if (snprintf(temporary, sizeof(temporary), "%s.tmp", path) >= (int)sizeof(temporary)) return;
    FILE *file = fopen(temporary, "w");
    if (!file) return;
    /* Observable test state never contains credentials or pairing tokens. */
    fprintf(file, "{\"page\":%d,\"password_length\":%u,\"password_visible\":%s,\"pending\":%s,\"bridge_running\":%s,\"network\":%d,\"progress\":%u,\"bridge_open_calls\":%u,\"bridge_prefetch_calls\":%u,\"bridge_active\":%s,\"code_ready\":%s,\"code_length\":%u,\"renewed_code\":%s,\"rate_valid\":%s,\"rate_bps\":%llu,\"component_id\":%u,\"sample_ms\":%llu}\n",
        state.page, (unsigned)strlen(state.password), state.password_visible ? "true" : "false",
        state.download_pending ? "true" : "false", model.bridge_running ? "true" : "false", model.network, model.progress, bridge_open_calls,
        bridge_prefetch_calls, bridge_active ? "true" : "false", model.bridge_code[0] ? "true" : "false",
        (unsigned)strlen(model.bridge_code), renewed_code ? "true" : "false",
        state.rate.valid ? "true" : "false", (unsigned long long)state.rate.bytes_per_second,
        model.transfer.component_id, (unsigned long long)model.transfer.now_ms);
    fclose(file);
#ifdef _WIN32
    /* The Windows C runtime's rename cannot replace an existing snapshot. */
    (void)MoveFileExA(temporary, path, MOVEFILE_REPLACE_EXISTING);
#else
    (void)rename(temporary, path);
#endif
}
static void snapshot(void *ctx, vibe_snapshot_t *out)
{
    (void)ctx;
    if (connecting && ++connect_ticks >= 5) {
        connecting = false;
        model.network = VIBE_NET_CONNECTED;
        snprintf(model.ip, sizeof(model.ip), "192.0.2.10");
    }
    if (installing) {
        model.progress += 50;
        model.transfer.now_ms += 250;
        model.transfer.received = model.transfer.total * model.progress / 1000;
        model.transfer.component_received = (uint32_t)model.transfer.received;
        if (scenario("update-fail") && model.progress >= 500) {
            installing = false; model.updating = false;
            model.update_terminal = model.update_failed = true;
            ++model.update_revision;
            snprintf(model.update_title, sizeof(model.update_title), "Update failed");
            snprintf(model.update_detail, sizeof(model.update_detail), "Simulated verification error");
        } else if (model.progress >= 1000) {
            installing = false; model.updating = false; model.update_terminal = true;
            ++model.update_revision;
            snprintf(model.update_title, sizeof(model.update_title), "Update complete");
            snprintf(model.update_detail, sizeof(model.update_detail), "Simulated images verified");
        }
        model.transfer.receiving = installing;
    }
    if (model.bridge_running && model.network == VIBE_NET_CONNECTED && !scenario("waiting-code")) {
        snprintf(model.bridge_state, sizeof(model.bridge_state), "PAIRING");
        snprintf(model.bridge_code, sizeof(model.bridge_code), "%s", scenario("long-code") ? "WWWWWWWWWWWWWWW" : renewed_code ? "NEW1234567" : "VIBE123456");
        model.bridge_seconds = 300;
    }
    *out = model;
}
static int command(void *ctx, vibe_command_t cmd, const char *a, const char *b)
{
    (void)ctx;
    switch (cmd) {
    case VIBE_SCAN_WIFI: ++model.scan_generation; break;
    case VIBE_FORGET_WIFI:
        model.credentials_saved = false; model.network = VIBE_NET_OFFLINE; model.ip[0] = 0; connecting = false; break;
    case VIBE_CONNECT_WIFI:
        if (strlen(b) != 0 && strlen(b) < 8) return 0x102;
        snprintf(model.ssid, sizeof(model.ssid), "%s", a);
        ++connect_attempts;
        if (!strcmp(b, "wrongpass") || (scenario("wifi-fail-once") && connect_attempts == 1)) {
            model.network = VIBE_NET_FAILED; break;
        }
        model.network = VIBE_NET_CONNECTING; model.credentials_saved = true;
        connecting = true; connect_ticks = 0; break;
    case VIBE_OPEN_BRIDGE:
        ++bridge_open_calls;
        bridge_active = true;
        if (model.bridge_running) break;
        model.bridge_running = true;
        snprintf(model.bridge_state, sizeof(model.bridge_state), "WAITING_NETWORK"); break;
    case VIBE_PREFETCH_BRIDGE:
        ++bridge_prefetch_calls;
        model.bridge_running = true; break;
    case VIBE_PAUSE_BRIDGE: bridge_active = false; break;
    case VIBE_STOP_BRIDGE: model.bridge_running = false; bridge_active = false; model.bridge_code[0] = 0; break;
    case VIBE_SCAN_NAND: ++model.nand_generation; break;
    case VIBE_INSTALL_NAND:
        installing = true; model.updating = true; model.update_terminal = false; model.progress = 0;
        model.update_failed = false;
        ++model.update_revision;
        model.transfer = (vibe_transfer_t){.source=3, .total=1024*1024,
            .component_size=1024*1024, .component_count=1, .component_id=1, .receiving=true};
        model.transfer.job[0] = (uint8_t)model.update_revision;
        snprintf(model.update_title, sizeof(model.update_title), "Updating system");
        snprintf(model.update_detail, sizeof(model.update_detail), "Reading simulated NAND component");
        snprintf(model.update_owner, sizeof(model.update_owner), "NAND");
        snprintf(model.update_verified, sizeof(model.update_verified), "Unsigned"); break;
    }
    fprintf(stderr, "vibe_sim: command=%d page=%d\n", cmd, state.page);
    return 0;
}
esp_gsp_err_t gsp_bridge_app_init(esp_gsp_handle_t ui)
{
    model.tcp_port = 7777;
    if (scenario("prefetched") || scenario("long-code") || scenario("waiting-code")) {
        model.credentials_saved = true;
        model.network = VIBE_NET_CONNECTED;
        snprintf(model.ip, sizeof(model.ip), "192.0.2.10");
    }
    snprintf(model.hostname, sizeof(model.hostname), "mosaico-simulator");
    model.ap_count = 6; model.scan_generation = 1;
    if (scenario("empty")) model.ap_count = 0;
    for (size_t i = 0; i < model.ap_count; ++i) {
        snprintf(model.aps[i].ssid, sizeof(model.aps[i].ssid), "Studio network %u", (unsigned)i + 1);
        model.aps[i].rssi = -35 - (int)i * 7;
        model.aps[i].open = i == 5;
    }
    memset(model.token, 'a', 64); model.token[64] = 0;
    model.bundle_count = 2; model.nand_generation = 1;
    if (scenario("empty")) model.bundle_count = 0;
    for (size_t i = 0; i < model.bundle_count; ++i) {
        snprintf(model.bundles[i].release, sizeof(model.bundles[i].release), "Demo release %u", (unsigned)i + 1);
        snprintf(model.bundles[i].path, sizeof(model.bundles[i].path), "/nand/system-update/demo%u/manifest.json", (unsigned)i);
        model.bundles[i].bytes = 1024 * 1024; model.bundles[i].components = 2;
    }
    const vibe_services_t services = {.snapshot = snapshot, .command = command};
    esp_gsp_err_t err = vibe_ui_init(&state, ui, &services);
    if (err == ESP_GSP_OK && getenv("VIBE_SIM_STATE_FILE"))
        trace_timer = esp_gsp_timer_create(ui, 50, trace_state, NULL);
    return err;
}
void gsp_bridge_app_deinit(esp_gsp_handle_t ui)
{
    (void)ui;
    if (trace_timer) (void)esp_gsp_timer_delete(ui, trace_timer);
    vibe_ui_deinit(&state);
}
