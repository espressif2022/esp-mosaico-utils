// SPDX-License-Identifier: Apache-2.0
#include "vibe_transfer.h"
#include <string.h>

unsigned vibe_transfer_permille(uint64_t received, uint64_t total)
{
    if (!total) return 0;
    return received >= total ? 1000 : (unsigned)(received * 1000 / total);
}

void vibe_rate_sample(vibe_rate_t *r, const vibe_transfer_t *t)
{
    if (!t->receiving) {
        memset(r, 0, sizeof(*r));
        return;
    }
    if (!r->started || r->source != t->source ||
        memcmp(r->job, t->job, sizeof(r->job)) ||
        r->component_id != t->component_id || t->now_ms < r->last_ms ||
        t->received < r->last_bytes) {
        memset(r, 0, sizeof(*r));
        memcpy(r->job, t->job, sizeof(r->job));
        r->source = t->source;
        r->component_id = t->component_id;
        r->started = true;
        r->baseline_ms = r->last_ms = r->changed_ms = t->now_ms;
        r->baseline_bytes = r->last_bytes = t->received;
        return;
    }
    if (t->received != r->last_bytes) r->changed_ms = t->now_ms;
    const uint64_t elapsed = t->now_ms - r->baseline_ms;
    if (elapsed >= 1000) {
        r->bytes_per_second = (t->received - r->baseline_bytes) * 1000 / elapsed;
        r->valid = true;
        r->baseline_ms = t->now_ms;
        r->baseline_bytes = t->received;
    }
    if (t->now_ms - r->changed_ms >= 1000) {
        r->bytes_per_second = 0;
        r->valid = true;
    }
    r->last_ms = t->now_ms;
    r->last_bytes = t->received;
}
