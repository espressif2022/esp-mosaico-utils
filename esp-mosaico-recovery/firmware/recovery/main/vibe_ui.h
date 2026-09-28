// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_gsp.h"
#include "vibe_transfer.h"

#define VIBE_AP_MAX 16
#define VIBE_BUNDLE_MAX 8

typedef enum {
    VIBE_HOME, VIBE_WIFI, VIBE_PASSWORD, VIBE_PAIRING, VIBE_BRIDGE,
    VIBE_NAND, VIBE_CONFIRM, VIBE_UPDATE, VIBE_RESULT, VIBE_PAGE_COUNT
} vibe_page_t;
typedef enum { VIBE_NET_OFFLINE, VIBE_NET_CONNECTING, VIBE_NET_CONNECTED, VIBE_NET_FAILED } vibe_net_t;
typedef enum {
    VIBE_SCAN_WIFI, VIBE_FORGET_WIFI, VIBE_CONNECT_WIFI, VIBE_OPEN_BRIDGE,
    VIBE_STOP_BRIDGE, VIBE_SCAN_NAND, VIBE_INSTALL_NAND,
    VIBE_PREFETCH_BRIDGE, VIBE_PAUSE_BRIDGE
} vibe_command_t;
typedef struct { char ssid[33]; int rssi; bool open; } vibe_ap_t;
typedef struct { char release[48]; char path[256]; uint64_t bytes; unsigned components; } vibe_bundle_t;
typedef struct {
    vibe_net_t network;
    bool credentials_saved, scanning, usb_owner, tcp_owner, session;
    char ssid[33], ip[16], hostname[48], token[65];
    unsigned tcp_port;
    uint32_t scan_generation;
    size_t ap_count;
    vibe_ap_t aps[VIBE_AP_MAX];
    bool bridge_running;
    char bridge_state[24], bridge_code[16];
    unsigned bridge_seconds;
    int bridge_error;
    uint32_t nand_generation;
    bool nand_scanning;
    int nand_error;
    size_t bundle_count, invalid_bundles;
    vibe_bundle_t bundles[VIBE_BUNDLE_MAX];
    /* A new revision identifies an update state change, including a new job.
     * Active updates own the screen; terminal states are acknowledged by Home. */
    uint32_t update_revision;
    bool updating, update_terminal, update_failed;
    unsigned progress; /* permille */
    vibe_transfer_t transfer;
    char update_title[48], update_detail[160], update_owner[16], update_verified[24];
} vibe_snapshot_t;
typedef struct {
    void *ctx;
    void (*snapshot)(void *ctx, vibe_snapshot_t *out);
    int (*command)(void *ctx, vibe_command_t command, const char *first, const char *second);
} vibe_services_t;
typedef struct {
    esp_gsp_handle_t ui;
    vibe_services_t services;
    vibe_snapshot_t snapshot;
    vibe_page_t page;
    esp_gsp_list_t networks, bundles;
    void *timer;
    bool download_pending, password_visible;
    bool prefetch_attempted;
    char selected_ssid[33], password_projection[65], password[65];
    vibe_bundle_t selected_bundle;
    uint32_t acknowledged_update;
    vibe_rate_t rate;
    unsigned tick;
} vibe_ui_t;

esp_gsp_err_t vibe_ui_init(vibe_ui_t *state, esp_gsp_handle_t ui, const vibe_services_t *services);
void vibe_ui_deinit(vibe_ui_t *state);
/* Call only on the render task, like event and timer callbacks. */
int vibe_ui_open_bridge(vibe_ui_t *state);
void vibe_ui_poll(vibe_ui_t *state);
