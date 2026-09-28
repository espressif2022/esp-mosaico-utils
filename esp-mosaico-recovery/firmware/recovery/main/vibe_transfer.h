// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Local UI telemetry, not part of the ESP-Iris wire protocol. */
typedef struct {
    uint8_t job[16], source;
    bool receiving;
    uint64_t now_ms, received, total;
    uint32_t component_received, component_size;
    unsigned component_id, completed_components, component_count;
} vibe_transfer_t;

typedef struct {
    uint8_t job[16], source;
    unsigned component_id;
    bool started, valid;
    uint64_t baseline_ms, baseline_bytes, last_ms, last_bytes, changed_ms;
    uint64_t bytes_per_second;
} vibe_rate_t;

/* Input time is monotonic. Reset at transfer boundaries and counter rollback.
 * The approximately one-second window reports payload accepted by the writer. */
void vibe_rate_sample(vibe_rate_t *rate, const vibe_transfer_t *transfer);
unsigned vibe_transfer_permille(uint64_t received, uint64_t total);
