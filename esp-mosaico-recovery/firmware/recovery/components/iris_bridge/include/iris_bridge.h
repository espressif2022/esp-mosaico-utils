#pragma once

#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define IRIS_BRIDGE_ORIGIN_BYTES 256
#define IRIS_BRIDGE_BOARD_BYTES 64
#define IRIS_BRIDGE_CODE_BYTES 16

typedef struct {
    const char *server_url;
    const char *board_id;
    const char *device_id;
    bool enable_factory_update; /* Recovery self-update; default false. */
    bool enable_system_update; /* .irisfw transactions; default false. */
    bool enable_bootloader_update; /* Requires system_update; default false. */
    /* Register/cache a code without polling for or executing remote work. */
    bool prefetch_only;
    /* Nonblocking readiness probe; called only by the Bridge worker. */
    bool (*network_ready)(void);
} iris_bridge_config_t;

typedef struct {
    bool running;
    char state[24];
    char server_url[IRIS_BRIDGE_ORIGIN_BYTES];
    char code[IRIS_BRIDGE_CODE_BYTES];
    /* RFC 3339 with nanoseconds and a numeric timezone needs 35 characters. */
    char expires_at[40];
    uint32_t expires_in_ms;
    unsigned progress;
    esp_err_t error;
} iris_bridge_snapshot_t;

/* Call on every Recovery boot after sysmeta initialization, before UI/network. */
esp_err_t iris_bridge_resume_boot(void);
/* Copies configuration. A worker waits for IP before registering one session. */
esp_err_t iris_bridge_start(const iris_bridge_config_t *config);
/* Safe asynchronous stop; an authorized critical commit still finishes. */
void iris_bridge_stop(void);
/* Opening the download page enables polling; leaving it pauses polling.
 * An already-authorized critical commit still finishes. */
void iris_bridge_set_active(bool active);
bool iris_bridge_is_running(void);
void iris_bridge_get_snapshot(iris_bridge_snapshot_t *snapshot);

#ifdef __cplusplus
}
#endif
