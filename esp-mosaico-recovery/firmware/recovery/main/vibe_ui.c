// SPDX-License-Identifier: Apache-2.0
#include "vibe_ui.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#define GSP_BUNDLE_ENABLE_RAW_IDS
#include "bundle_gsp.h"

#define TEXT(s, name, value) esp_gsp_set_text((s)->ui, GSP_VIBE_BIND_##name, value)
static void format(vibe_ui_t *s, uint16_t bind, const char *fmt, ...)
{
    char text[192];
    va_list args;
    va_start(args, fmt);
    vsnprintf(text, sizeof(text), fmt, args);
    va_end(args);
    (void)esp_gsp_set_text(s->ui, bind, text);
}
#define FORMAT(s, name, ...) format(s, GSP_VIBE_BIND_##name, __VA_ARGS__)
static void transfer_text(vibe_ui_t *s)
{
    const vibe_snapshot_t *v = &s->snapshot;
    const vibe_transfer_t *t = &v->transfer;
    const uint64_t unit = (t->total ? t->total : t->received) >= 1024 * 1024 ? 1024 * 1024 : 1024;
    const char *suffix = unit == 1024 ? "KiB" : "MiB";
    const unsigned received = (unsigned)(t->received * 100 / unit);
    const unsigned total = (unsigned)(t->total * 100 / unit);
    if (t->total) {
        FORMAT(s, UPDATE_PERCENT, "%u%%", vibe_transfer_permille(t->received, t->total) / 10);
        FORMAT(s, UPDATE_BYTES, "%u.%02u / %u.%02u %s", received / 100, received % 100, total / 100, total % 100, suffix);
    } else {
        TEXT(s, UPDATE_PERCENT, "--");
        FORMAT(s, UPDATE_BYTES, "%u.%02u %s / --", received / 100, received % 100, suffix);
    }
    (void)esp_gsp_set_value(s->ui, GSP_VIBE_BIND_PROGRESS, vibe_transfer_permille(t->received, t->total) / 10);
    const char *verb = !strcmp(v->update_owner, "Bridge") ? "Download" : !strcmp(v->update_owner, "NAND") ? "Read" : "Receive";
    if (!s->rate.valid) FORMAT(s, UPDATE_RATE, "%s: --", verb);
    else {
        const uint64_t rate_unit = s->rate.bytes_per_second >= 1024 * 1024 ? 1024 * 1024 : 1024;
        const unsigned tenths = (unsigned)(s->rate.bytes_per_second * 10 / rate_unit);
        FORMAT(s, UPDATE_RATE, "%s: %u.%u %s/s", verb, tenths / 10, tenths % 10, rate_unit == 1024 ? "KiB" : "MiB");
    }
    if (t->component_count && t->component_id)
        FORMAT(s, UPDATE_COMPONENT, "Component %u: %u%% | %u/%u verified", t->component_id,
            vibe_transfer_permille(t->component_received, t->component_size) / 10,
            t->completed_components, t->component_count);
    else if (t->component_count)
        FORMAT(s, UPDATE_COMPONENT, "%u/%u components verified", t->completed_components, t->component_count);
    else TEXT(s, UPDATE_COMPONENT, "Application image");
}
static int command(vibe_ui_t *s, vibe_command_t cmd, const char *a, const char *b)
{
    return s->services.command(s->services.ctx, cmd, a, b);
}
static void clear_secret(char *text, size_t size)
{
    volatile char *p = text;
    while (size--) *p++ = 0;
}
static void show(vibe_ui_t *s, vibe_page_t page)
{
    static const uint16_t visibility[VIBE_PAGE_COUNT] = {
        GSP_VIBE_BIND_HOME_VISIBLE, GSP_VIBE_BIND_WIFI_VISIBLE,
        GSP_VIBE_BIND_PASSWORD_VISIBLE, GSP_VIBE_BIND_PAIRING_VISIBLE,
        GSP_VIBE_BIND_BRIDGE_VISIBLE, GSP_VIBE_BIND_NAND_VISIBLE,
        GSP_VIBE_BIND_CONFIRM_VISIBLE, GSP_VIBE_BIND_UPDATE_VISIBLE,
        GSP_VIBE_BIND_RESULT_VISIBLE,
    };
    const vibe_page_t previous = s->page;
    if (previous == VIBE_PASSWORD && page != VIBE_PASSWORD) {
        clear_secret(s->password, sizeof(s->password));
        TEXT(s, PASSWORD_DISPLAY, "Enter password");
        clear_secret(s->password_projection, sizeof(s->password_projection));
        s->password_visible = false;
        TEXT(s, PASSWORD_TOGGLE, "Show");
    }
    if (page != VIBE_WIFI && page != VIBE_PASSWORD) s->download_pending = false;
    if (previous == VIBE_BRIDGE && page != VIBE_BRIDGE && page != VIBE_UPDATE && page != VIBE_RESULT)
        (void)command(s, VIBE_PAUSE_BRIDGE, NULL, NULL);
    s->page = page;
    /* Authored layers start with only HOME visible. Update only the two
     * participating layers, avoiding redundant visibility invalidations. */
    if (previous != page)
        (void)esp_gsp_set_visible(s->ui, visibility[previous], false);
    (void)esp_gsp_set_visible(s->ui, visibility[page], true);
    if (page == VIBE_WIFI) {
        TEXT(s, WIFI_NOTE, s->download_pending ? "Connect Wi-Fi to download ideas" : "USB remains available offline");
        (void)esp_gsp_list_refresh(s->ui, s->networks);
        if (previous != page) (void)command(s, VIBE_SCAN_WIFI, NULL, NULL);
    }
    if (page == VIBE_NAND) (void)esp_gsp_list_refresh(s->ui, s->bundles);
}
static void error_result(vibe_ui_t *s, const char *title, int error)
{
    TEXT(s, RESULT_TITLE, title);
    FORMAT(s, RESULT_DETAIL, "Error 0x%08x\nReturn home and retry", (unsigned)error);
    show(s, VIBE_RESULT);
}
int vibe_ui_open_bridge(vibe_ui_t *s)
{
    if (!s || !s->ui) return ESP_GSP_ERR_INVALID_ARG;
    s->services.snapshot(s->services.ctx, &s->snapshot);
    if (s->snapshot.bridge_running && s->page == VIBE_BRIDGE) return s->snapshot.bridge_error;
    int err = command(s, VIBE_OPEN_BRIDGE, NULL, NULL);
    show(s, VIBE_BRIDGE);
    vibe_ui_poll(s);
    return err;
}
static gsp_err_t network_row(esp_gsp_handle_t ui, esp_gsp_row_t row, uint32_t index, void *ctx)
{
    vibe_ui_t *s = ctx;
    if (index >= s->snapshot.ap_count) return GSP_ERR_INVALID_ARG;
    const vibe_ap_t *ap = &s->snapshot.aps[index];
    char detail[48];
    snprintf(detail, sizeof(detail), "%s - %d dBm", ap->open ? "Open" : "Secured", ap->rssi);
    esp_gsp_err_t err = gsp_vibe_networks_row_row_set_networks_title_text(ui, row, ap->ssid);
    if (err == ESP_GSP_OK) err = gsp_vibe_networks_row_row_set_networks_detail_text(ui, row, detail);
    return err == ESP_GSP_OK ? GSP_OK : GSP_ERR_INVALID_STATE;
}
static gsp_err_t bundle_row(esp_gsp_handle_t ui, esp_gsp_row_t row, uint32_t index, void *ctx)
{
    vibe_ui_t *s = ctx;
    if (index >= s->snapshot.bundle_count) return GSP_ERR_INVALID_ARG;
    const vibe_bundle_t *bundle = &s->snapshot.bundles[index];
    char detail[64];
    snprintf(detail, sizeof(detail), "%u components - %lu KB", bundle->components, (unsigned long)(bundle->bytes / 1024));
    esp_gsp_err_t err = gsp_vibe_bundles_row_row_set_bundles_title_text(ui, row, bundle->release);
    if (err == ESP_GSP_OK) err = gsp_vibe_bundles_row_row_set_bundles_detail_text(ui, row, detail);
    return err == ESP_GSP_OK ? GSP_OK : GSP_ERR_INVALID_STATE;
}
static void project_password(vibe_ui_t *s)
{
    char password[65] = {0}, display[65] = {0};
    memcpy(password, s->password, sizeof(password));
    const size_t n = strlen(password);
    if (s->password_visible) memcpy(display, password, n);
    else memset(display, '*', n);
    if (strcmp(display, s->password_projection) != 0) {
        memcpy(s->password_projection, display, sizeof(display));
        /* Keep the tail visible when a long password exceeds the field. */
        TEXT(s, PASSWORD_DISPLAY, n ? display + (n > 22 ? n - 22 : 0) : "Enter password");
    }
    clear_secret(password, sizeof(password));
    clear_secret(display, sizeof(display));
}
static void submit_password(vibe_ui_t *s)
{
    char password[65] = {0};
    memcpy(password, s->password, sizeof(password));
    int err = command(s, VIBE_CONNECT_WIFI, s->selected_ssid, password);
    clear_secret(password, sizeof(password));
    if (err) TEXT(s, PASSWORD_ERROR, "Check password and try again");
    else show(s, s->download_pending ? VIBE_WIFI : VIBE_HOME);
}
static void event(esp_gsp_handle_t ui, const esp_gsp_event_t *e, void *ctx)
{
    vibe_ui_t *s = ctx;
    if (e->type != ESP_GSP_EVENT_CALL) return;
#ifdef VIBE_UI_TRACE
    fprintf(stderr, "vibe_event: action=%u list=%u item=%u page=%d networks=%u bundles=%u\n", e->action_id, e->list, (unsigned)e->item, s->page, s->networks, s->bundles);
#endif
    if (s->snapshot.updating) return;
    int err;
    switch (e->action_id) {
    case GSP_VIBE_ACT_ID_DOWNLOAD:
        if (s->snapshot.credentials_saved || s->snapshot.network == VIBE_NET_CONNECTED) {
            (void)vibe_ui_open_bridge(s);
        } else { s->download_pending = true; show(s, VIBE_WIFI); }
        break;
    case GSP_VIBE_ACT_ID_WIFI: s->download_pending = false; show(s, VIBE_WIFI); break;
    case GSP_VIBE_ACT_ID_PAIRING: show(s, VIBE_PAIRING); break;
    case GSP_VIBE_ACT_ID_BACK_PASSWORD: show(s, VIBE_WIFI); break;
    case GSP_VIBE_ACT_ID_BACK_CONFIRM:
    case GSP_VIBE_ACT_ID_CANCEL_INSTALL: show(s, VIBE_NAND); break;
    case GSP_VIBE_ACT_ID_HOME:
    case GSP_VIBE_ACT_ID_BACK_WIFI:
    case GSP_VIBE_ACT_ID_BACK_PAIRING:
    case GSP_VIBE_ACT_ID_BACK_NAND:
    case GSP_VIBE_ACT_ID_BACK_BRIDGE:
        s->acknowledged_update = s->snapshot.update_revision;
        show(s, VIBE_HOME); break;
    case GSP_VIBE_ACT_ID_CANCEL_BRIDGE:
        (void)command(s, VIBE_STOP_BRIDGE, NULL, NULL);
        show(s, VIBE_HOME); break;
    case GSP_VIBE_ACT_ID_SCAN:
        err = command(s, VIBE_SCAN_WIFI, NULL, NULL);
        if (err) FORMAT(s, WIFI_SCAN, "Scan error 0x%x", (unsigned)err);
        break;
    case GSP_VIBE_ACT_ID_FORGET:
        (void)command(s, VIBE_STOP_BRIDGE, NULL, NULL);
        s->prefetch_attempted = false;
        (void)command(s, VIBE_FORGET_WIFI, NULL, NULL); break;
    case GSP_VIBE_ACT_ID_NAND:
    case GSP_VIBE_ACT_ID_NAND_SCAN:
        show(s, VIBE_NAND);
        err = command(s, VIBE_SCAN_NAND, NULL, NULL);
        if (err) FORMAT(s, NAND_SCAN, "Scan error 0x%x", (unsigned)err);
        break;
    case GSP_VIBE_ACT_ID_SELECT_NETWORKS:
        if (s->page != VIBE_WIFI || e->list != s->networks || e->item >= s->snapshot.ap_count) break;
        snprintf(s->selected_ssid, sizeof(s->selected_ssid), "%s", s->snapshot.aps[e->item].ssid);
        TEXT(s, SSID, s->selected_ssid); TEXT(s, PASSWORD_ERROR, "");
        clear_secret(s->password, sizeof(s->password));
        TEXT(s, PASSWORD_DISPLAY, "Enter password");
        s->password_visible = false;
        clear_secret(s->password_projection, sizeof(s->password_projection));
        show(s, VIBE_PASSWORD);
        (void)esp_gsp_component_set_visible(ui, GSP_VIBE_OBJ_KEY_PASSWORD_KEYBOARD_LOWER, true);
        (void)esp_gsp_component_set_visible(ui, GSP_VIBE_OBJ_KEY_PASSWORD_KEYBOARD_UPPER, false);
        (void)esp_gsp_component_set_visible(ui, GSP_VIBE_OBJ_KEY_PASSWORD_KEYBOARD_SYM, false);
        break;
    case GSP_VIBE_ACT_ID_PASSWORD_KEYBOARD_KEY:
        if (s->page != VIBE_PASSWORD) break;
        /* Native GSP keys emit Unicode codepoints, Backspace=8, Enter=13.
         * Own the bounded buffer so masking works identically with 1.2.0's
         * device runtime and sim_bridge (which lacks keyboard read-back). */
        if (e->arg == 13) submit_password(s);
        else {
            size_t len = strlen(s->password);
            if (e->arg == 8 && len) s->password[len - 1] = 0;
            else if (e->arg >= 32 && e->arg <= 126 && len < 64) {
                s->password[len] = (char)e->arg; s->password[len + 1] = 0;
            }
            project_password(s);
        }
        break;
    case GSP_VIBE_ACT_ID_TOGGLE_PASSWORD:
        if (s->page == VIBE_PASSWORD) { s->password_visible = !s->password_visible; TEXT(s, PASSWORD_TOGGLE, s->password_visible ? "Hide" : "Show"); project_password(s); }
        break;
    case GSP_VIBE_ACT_ID_SELECT_BUNDLES:
        if (s->page != VIBE_NAND || e->list != s->bundles || e->item >= s->snapshot.bundle_count) break;
        s->selected_bundle = s->snapshot.bundles[e->item];
        TEXT(s, RELEASE, s->selected_bundle.release);
        FORMAT(s, RELEASE_DETAIL, "%u components - %lu KB", s->selected_bundle.components, (unsigned long)(s->selected_bundle.bytes / 1024));
        TEXT(s, RELEASE_PATH, s->selected_bundle.path);
        show(s, VIBE_CONFIRM); break;
    case GSP_VIBE_ACT_ID_INSTALL:
        if (s->page != VIBE_CONFIRM) break;
        err = command(s, VIBE_INSTALL_NAND, s->selected_bundle.path, NULL);
        if (err) error_result(s, "NAND update failed", err);
        else {
            TEXT(s, UPDATE_TITLE, "Updating system"); TEXT(s, UPDATE_DETAIL, "Opening NAND firmware bundle");
            TEXT(s, UPDATE_PERCENT, "0%"); TEXT(s, UPDATE_OWNER, "NAND"); TEXT(s, UPDATE_VERIFIED, "Validating");
            (void)esp_gsp_set_value(ui, GSP_VIBE_BIND_PROGRESS, 0);
            show(s, VIBE_UPDATE);
        }
        break;
    default: break;
    }
}
void vibe_ui_poll(vibe_ui_t *s)
{
    uint32_t scan = s->snapshot.scan_generation, nand = s->snapshot.nand_generation;
    size_t ap_count = s->snapshot.ap_count, bundle_count = s->snapshot.bundle_count;
    s->services.snapshot(s->services.ctx, &s->snapshot);
    vibe_snapshot_t *v = &s->snapshot;
    vibe_rate_sample(&s->rate, &v->transfer);
    if (!s->prefetch_attempted && !v->updating &&
        v->network == VIBE_NET_CONNECTED && v->ip[0]) {
        s->prefetch_attempted = true;
        (void)command(s, VIBE_PREFETCH_BRIDGE, NULL, NULL);
    }
    if (v->ap_count > VIBE_AP_MAX) v->ap_count = VIBE_AP_MAX;
    if (v->bundle_count > VIBE_BUNDLE_MAX) v->bundle_count = VIBE_BUNDLE_MAX;
    if (scan != v->scan_generation || ap_count != v->ap_count) {
        (void)esp_gsp_list_set_total(s->ui, s->networks, v->ap_count);
        (void)esp_gsp_list_refresh(s->ui, s->networks);
    }
    if (nand != v->nand_generation || bundle_count != v->bundle_count) {
        (void)esp_gsp_list_set_total(s->ui, s->bundles, v->bundle_count);
        (void)esp_gsp_list_refresh(s->ui, s->bundles);
    }
    if (v->updating) {
        if (s->page != VIBE_UPDATE) show(s, VIBE_UPDATE);
        TEXT(s, UPDATE_TITLE, v->update_title); TEXT(s, UPDATE_DETAIL, v->update_detail);
        TEXT(s, UPDATE_OWNER, v->update_owner); TEXT(s, UPDATE_VERIFIED, v->update_verified);
        transfer_text(s);
    } else if (v->update_terminal && v->update_revision != s->acknowledged_update) {
        s->acknowledged_update = v->update_revision;
        TEXT(s, RESULT_TITLE, v->update_title);
        if (v->update_failed && v->transfer.total)
            FORMAT(s, RESULT_DETAIL, "%s\nTransferred: %u%%", v->update_detail,
                vibe_transfer_permille(v->transfer.received, v->transfer.total) / 10);
        else TEXT(s, RESULT_DETAIL, v->update_detail);
        show(s, VIBE_RESULT);
    }
    switch (s->page) {
    case VIBE_HOME:
        TEXT(s, USB_STATUS, v->usb_owner ? "Active" : v->session ? "Paused" : "Available");
        TEXT(s, NETWORK_STATUS, v->network == VIBE_NET_CONNECTED ? (v->tcp_owner ? "Active" : v->session ? "Paused" : "Available") : v->network == VIBE_NET_CONNECTING ? "Connecting" : "Offline");
        if (v->network == VIBE_NET_CONNECTED) FORMAT(s, ADDRESS, "%s.local - %s:%u", v->hostname, v->ip, v->tcp_port);
        else TEXT(s, ADDRESS, "Network setup not completed");
        break;
    case VIBE_WIFI:
        TEXT(s, WIFI_CONNECTION, v->network == VIBE_NET_CONNECTED || v->network == VIBE_NET_CONNECTING ? v->ssid : v->network == VIBE_NET_FAILED ? "Connection failed" : "Not connected");
        if (v->network == VIBE_NET_CONNECTED) FORMAT(s, WIFI_DETAIL, "%s", v->ip);
        else TEXT(s, WIFI_DETAIL, v->network == VIBE_NET_CONNECTING ? "Connecting..." : "Choose a network below");
        if (v->scanning) TEXT(s, WIFI_SCAN, "Scanning...");
        else FORMAT(s, WIFI_SCAN, "%u networks found", (unsigned)v->ap_count);
        if (s->download_pending && v->network == VIBE_NET_CONNECTED && v->ip[0]) (void)vibe_ui_open_bridge(s);
        break;
    case VIBE_PAIRING:
        if (v->network == VIBE_NET_CONNECTED) FORMAT(s, PAIRING_ENDPOINT, "%s.local\n%s:%u", v->hostname, v->ip, v->tcp_port);
        else TEXT(s, PAIRING_ENDPOINT, "Connect Wi-Fi first");
        if (v->token[0]) FORMAT(s, PAIRING_TOKEN, "%.32s\n%.32s", v->token, v->token + 32);
        else TEXT(s, PAIRING_TOKEN, "Pairing token unavailable");
        break;
    case VIBE_BRIDGE:
        {
            const char *code = v->bridge_code[0] ? v->bridge_code : "----------";
            const bool compact = strlen(code) > 10 || strspn(code, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-") != strlen(code);
            /* Use a real blank glyph: empty text can restore the authored
             * placeholder in the renderer instead of clearing this label. */
            TEXT(s, BRIDGE_CODE, compact ? " " : code);
            TEXT(s, BRIDGE_CODE_COMPACT, compact ? code : " ");
        }
        if (!strcmp(v->bridge_state, "NOT_CONFIGURED")) TEXT(s, BRIDGE_DETAIL, "Bridge URL or board ID not configured");
        else if (!strcmp(v->bridge_state, "WAITING_NETWORK")) TEXT(s, BRIDGE_DETAIL, "Waiting for Wi-Fi connection");
        else if (v->bridge_code[0]) FORMAT(s, BRIDGE_DETAIL, "Pair within %us", v->bridge_seconds);
        else FORMAT(s, BRIDGE_DETAIL, "%s%s", v->bridge_state, v->bridge_running ? "" : " - exit to retry");
        break;
    case VIBE_NAND:
        if (v->nand_scanning) TEXT(s, NAND_SCAN, "Scanning NAND...");
        else if (v->nand_error) FORMAT(s, NAND_SCAN, "NAND unavailable: 0x%x", (unsigned)v->nand_error);
        else FORMAT(s, NAND_SCAN, "%u found - %u invalid skipped", (unsigned)v->bundle_count, (unsigned)v->invalid_bundles);
        break;
    default: break;
    }
}
static void tick(esp_gsp_handle_t ui, void *ctx)
{
    (void)ui;
    vibe_ui_t *s = ctx;
    if (s->page == VIBE_PASSWORD) project_password(s);
    if (++s->tick % 5 == 0) vibe_ui_poll(s);
}
esp_gsp_err_t vibe_ui_init(vibe_ui_t *s, esp_gsp_handle_t ui, const vibe_services_t *services)
{
    if (!s || !ui || !services || !services->snapshot || !services->command) return ESP_GSP_ERR_INVALID_ARG;
    memset(s, 0, sizeof(*s)); s->ui = ui; s->services = *services;
    s->networks = esp_gsp_list_bind_component(ui, GSP_VIBE_OBJ_KEY_NETWORKS, network_row, s);
    s->bundles = esp_gsp_list_bind_component(ui, GSP_VIBE_OBJ_KEY_BUNDLES, bundle_row, s);
    if (s->networks == ESP_GSP_LIST_NONE || s->bundles == ESP_GSP_LIST_NONE) return ESP_GSP_ERR_NO_MEM;
    esp_gsp_err_t err = esp_gsp_on_event(ui, event, s);
    if (err != ESP_GSP_OK) return err;
    show(s, VIBE_HOME); vibe_ui_poll(s);
    s->timer = esp_gsp_timer_create(ui, 50, tick, s);
    return s->timer ? ESP_GSP_OK : ESP_GSP_ERR_NO_MEM;
}
void vibe_ui_deinit(vibe_ui_t *s)
{
    if (!s || !s->ui) return;
    show(s, VIBE_HOME);
    (void)command(s, VIBE_STOP_BRIDGE, NULL, NULL);
    if (s->timer) (void)esp_gsp_timer_delete(s->ui, s->timer);
    (void)esp_gsp_on_event(s->ui, NULL, NULL);
    clear_secret(s->snapshot.token, sizeof(s->snapshot.token));
    s->ui = NULL; s->timer = NULL;
}
