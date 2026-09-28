#pragma once
#include <stdint.h>
typedef int portMUX_TYPE;
typedef unsigned TickType_t;
#define portMUX_INITIALIZER_UNLOCKED 0
#define taskENTER_CRITICAL(x) ((void)(x))
#define taskEXIT_CRITICAL(x) ((void)(x))
#ifndef configTICK_RATE_HZ
#define configTICK_RATE_HZ 1000
#endif
#define pdMS_TO_TICKS(x) ((TickType_t)(x) * configTICK_RATE_HZ / 1000)
#define pdTRUE 1
#define pdPASS 1
#define taskYIELD() ((void)0)

#define portMAX_DELAY UINT32_MAX
