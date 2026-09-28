#pragma once
#include "cJSON.h"
#include "esp_err.h"
#include <stdbool.h>
/* Internal transport validation. Product backend remains Flash authority. */
esp_err_t iris_bridge_validate_system_plan(const cJSON *plan,
                                          bool enable_bootloader, bool enable_factory);
