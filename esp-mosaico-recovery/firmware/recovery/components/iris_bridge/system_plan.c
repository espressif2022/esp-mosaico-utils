#include "system_plan.h"
#include <string.h>

static const char *str(const cJSON *o, const char *key)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}
static bool same_number(const cJSON *left, const char *left_key,
                        const cJSON *right, const char *right_key)
{
    const cJSON *a = cJSON_GetObjectItemCaseSensitive(left, left_key);
    const cJSON *b = cJSON_GetObjectItemCaseSensitive(right, right_key);
    return cJSON_IsNumber(a) && cJSON_IsNumber(b) &&
           a->valuedouble == b->valuedouble;
}

/* Bind the backend-owned manifest to this session's leased files before
 * reserving the shared writer. The backend remains the single authority for
 * component shape, kind, address, ordering and image policy. */
esp_err_t iris_bridge_validate_system_plan(const cJSON *plan,
                                          bool enable_bootloader, bool enable_factory)
{
    const cJSON *manifest = cJSON_GetObjectItemCaseSensitive(plan, "system_manifest");
    const cJSON *components = cJSON_GetObjectItemCaseSensitive(manifest, "components");
    const cJSON *images = cJSON_GetObjectItemCaseSensitive(plan, "images");
    const int count = cJSON_GetArraySize(components);
    if (!cJSON_IsObject(manifest) || !cJSON_IsArray(components) ||
        !cJSON_IsArray(images) || cJSON_GetArraySize(images) != count ||
        strcmp(str(manifest, "target_layout_sha256"), str(plan, "target_table_sha256")))
        return ESP_ERR_INVALID_ARG;
    for (int i = 0; i < count; i++) {
        const cJSON *component = cJSON_GetArrayItem(components, i);
        const char *kind = str(component, "kind"), *file = str(component, "file");
        if (!strcmp(kind, "bootloader")) {
            if (!enable_bootloader)
                return ESP_ERR_NOT_SUPPORTED;
        } else if (!strcmp(kind, "recovery")) {
            if (!enable_factory)
                return ESP_ERR_NOT_SUPPORTED;
        }
        int found = 0;
        const cJSON *image = NULL;
        cJSON_ArrayForEach(image, images) {
            if (strcmp(file, str(image, "upload_id")))
                continue;
            if (!same_number(component, "id", image, "component_id") ||
                !same_number(component, "target_offset", image, "offset") ||
                !same_number(component, "size", image, "size") ||
                strcmp(kind, str(image, "kind")) ||
                strcmp(str(component, "sha256"), str(image, "sha256")))
                return ESP_ERR_INVALID_ARG;
            found++;
        }
        if (found != 1)
            return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}
