#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "esp_iris_system_update.h"
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

#define FACTORY_SYSTEM_UPDATE_FILENAME_BYTES 129U
#define FACTORY_SYSTEM_UPDATE_PATH_BYTES 256U

typedef enum {
    FACTORY_SYSTEM_UPDATE_OWNER_NONE = 0,
    FACTORY_SYSTEM_UPDATE_OWNER_ESP_IRIS,
    FACTORY_SYSTEM_UPDATE_OWNER_BRIDGE,
    FACTORY_SYSTEM_UPDATE_OWNER_NAND,
} factory_system_update_owner_t;

typedef struct {
    factory_system_update_owner_t owner;
    esp_iris_system_update_status_t update;
    /* Local, lock-consistent payload totals; includes the active component. */
    uint64_t total_size;
    uint64_t received_size;
    uint64_t completed_size;
} factory_system_update_status_t;

/* Register the Recovery-resident, product-owned Flash-policy backend.
 * A Recovery self-update is staged completely in PSRAM before the backend
 * temporarily unlocks and rewrites the running factory partition.
 * When the backend is disabled, this remains a successful no-op and the
 * read-only System Inventory service is still available. */
esp_err_t factory_system_update_register(void);

esp_err_t factory_system_update_start_nand(const char *manifest_path);
esp_err_t factory_system_update_nand_register(void);

esp_err_t factory_system_update_get_status(
    factory_system_update_status_t *status);

/* Reserve the shared writer before an asynchronous source performs network
 * or filesystem work. This makes admission atomic across Bridge, NAND and
 * ESP-Iris, so callers can report a busy writer before accepting a job. */
esp_err_t factory_system_update_source_reserve(
    factory_system_update_owner_t owner,
    const uint8_t operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES]);

/* Source-neutral transaction API used by the Bridge and NAND adapters. It
 * deliberately remains product-private: target addresses are authorized by
 * the manifest parser in the implementation, never by the transport. */
esp_err_t factory_system_update_source_prepare(
    factory_system_update_owner_t owner,
    const uint8_t *manifest, size_t manifest_size,
    const uint8_t operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES]);
/* Dedicated Bridge entrypoint for an already-parsed manifest. This avoids a
 * serialize/reparse cycle while retaining the shared backend policy. The
 * allow_bootloader flag is trusted local firmware policy; it must never come
 * from a remote manifest or transport parameter. The generic source API
 * continues to reject Bridge bootloader replacement. */
esp_err_t factory_system_update_source_prepare_bridge(
    const cJSON *manifest,
    const uint8_t operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES],
    bool allow_bootloader);
size_t factory_system_update_source_component_count(
    factory_system_update_owner_t owner);
esp_err_t factory_system_update_source_component(
    factory_system_update_owner_t owner, size_t index,
    esp_iris_system_update_component_t *component,
    char filename[FACTORY_SYSTEM_UPDATE_FILENAME_BYTES]);
esp_err_t factory_system_update_source_begin_component(
    factory_system_update_owner_t owner,
    const esp_iris_system_update_component_t *component);
esp_err_t factory_system_update_source_write_component(
    factory_system_update_owner_t owner,
    const esp_iris_system_update_component_t *component, uint32_t offset,
    const uint8_t *data, size_t size);
esp_err_t factory_system_update_source_end_component(
    factory_system_update_owner_t owner,
    const esp_iris_system_update_component_t *component,
    const uint8_t actual_sha256[ESP_IRIS_SYSTEM_SHA256_BYTES]);
/* True once remote critical commit begins; even failures must reload the table. */
bool factory_system_update_source_needs_restart(factory_system_update_owner_t owner);
esp_err_t factory_system_update_source_commit(
    factory_system_update_owner_t owner,
    const uint8_t operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES]);
void factory_system_update_source_abort(
    factory_system_update_owner_t owner,
    const uint8_t operation_id[ESP_IRIS_SYSTEM_OPERATION_ID_BYTES],
    esp_err_t reason);

#ifdef __cplusplus
}
#endif
