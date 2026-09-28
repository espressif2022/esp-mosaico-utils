#include "sdk.h"
#include <assert.h>
#include <setjmp.h>
#include <pthread.h>
#include <time.h>
#include BRIDGE_SOURCE

#define TEST_PAIRING_EXPIRY "2026-09-11T20:10:00.123456789+08:00"

_Atomic int64_t mock_time;
wifi_ps_type_t mock_wifi_ps = WIFI_PS_MIN_MODEM;
bool mock_network, mock_stop_on_delay;
int mock_create_fail, mock_alloc_fail, mock_commit_error, mock_writes, mock_commits,
    mock_reserved, mock_abort;
void (*mock_worker)(void *);
esp_partition_t mock_factory = {
    .address = 0x20000, .size = 0x1c0000, .label = "factory"};
static esp_partition_t app = {
    .address = 0x210000, .size = 0x100000, .subtype = 16, .label = "ota_0"};
static jmp_buf worker_exit;
static int restart_count, requests, allocation_count, mock_backend_alloc_fail;
static int system_prepares;
static bool system_allow_bootloader;
static esp_iris_system_update_component_t descriptor;
static int replies_count, reply_index, test_case;
static int pairing_snapshots;
static bool expire_on_delay;
static int activate_after_delays;
static bool pause_on_poll;
static pthread_t poll_thread, result_thread;
static bool result_joinable, block_result;
static atomic_bool result_started, release_result;
static _Thread_local bool in_result;
static int persisted_results;
static int64_t restarted_at;
static bool poll_joinable, mock_keep_alive;
static _Thread_local bool in_poll;
static atomic_bool poll_sleeping, poll_blocked, release_poll;
static bool slow_poll, download_during_poll, direct_poll_time;
static int64_t request_started[32];
static int http_allocations, http_live;
static void host_pause(void)
{
    const struct timespec pause = {.tv_nsec = 1000000};
    nanosleep(&pause, NULL);
}
static void join_poll_thread(void)
{
    if (poll_joinable) {
        assert(pthread_join(poll_thread, NULL) == 0);
        poll_joinable = false;
    }
}
static void *poll_entry(void *arg)
{
    in_poll = true;
    cancel_poll_run(arg);
    return NULL;
}
static void *result_entry(void *arg)
{
    in_result = true;
    atomic_store(&result_started, true);
    report_result_run(arg);
    return NULL;
}
int xTaskCreate(void (*fn)(void *), const char *name, int stack,
                void *arg, int pri, TaskHandle_t *task)
{
    (void)name; (void)stack; (void)pri; (void)task;
    if (mock_create_fail)
        return 0;
    if (fn == cancel_poll_run) {
        join_poll_thread();
        atomic_store(&poll_sleeping, false);
        assert(pthread_create(&poll_thread, NULL, poll_entry, arg) == 0);
        poll_joinable = true;
        while (!atomic_load(&poll_sleeping)) host_pause();
    } else if (fn == report_result_run) {
        assert(persisted_results || mock_commit_error);
        atomic_store(&result_started, false);
        assert(pthread_create(&result_thread, NULL, result_entry, arg) == 0);
        result_joinable = true;
        while (!atomic_load(&result_started)) host_pause();
    } else {
        mock_worker = fn;
    }
    return pdPASS;
}
static struct reply {
    const char *path, *body;
    int status;
    bool stop;
    const char *retry_after;
    int64_t delay_us;
} replies[32];
struct mock_http {
    struct reply *reply;
    size_t offset;
    char url[768];
    void *user_data;
    esp_err_t (*event_handler)(esp_http_client_event_t *);
};

void vTaskDelay(unsigned ms)
{
    if (in_poll) {
        atomic_store(&poll_sleeping, true);
        host_pause();
        return;
    }
    if (atomic_load(&cancel_poll_running) && !direct_poll_time) {
        host_pause();
        return;
    }
    if (atomic_load(&report_running)) host_pause();
    mock_time += (int64_t)ms * 1000;
    if (activate_after_delays && --activate_after_delays == 0) {
        assert(requests == 1); /* No background polling or re-registration. */
        iris_bridge_set_active(true);
    }
    if (expire_on_delay) {
        mock_time += 600000000;
        expire_on_delay = false;
    }
    if (mock_stop_on_delay)
        iris_bridge_stop();
    assert(mock_time < 1000000000LL);
}
void vTaskDelete(void *arg)
{
    (void)arg;
    if (in_poll || in_result) pthread_exit(NULL);
    join_poll_thread();
    longjmp(worker_exit, 1);
}
void esp_restart(void)
{
    assert(!atomic_load(&cancel_poll_running));
    assert(mock_wifi_ps == WIFI_PS_MIN_MODEM);
    join_poll_thread();
    restarted_at = mock_time;
    if (result_joinable) {
        atomic_store(&release_result, true);
        assert(pthread_join(result_thread, NULL) == 0);
        result_joinable = false;
    }
    close_client(&main_client);
    close_client(&download_client);
    restart_count++;
    longjmp(worker_exit, 2);
}
esp_err_t esp_ota_set_boot_partition(const esp_partition_t *p)
{
    assert(p);
    return 0;
}
const esp_partition_t *esp_partition_find_first(int type, int subtype,
                                                const char *label)
{
    (void)type;
    (void)subtype;
    return !strcmp(label, "factory") ? &mock_factory : &app;
}
esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *config)
{
    assert(config->disable_auto_redirect);
    assert(!strncmp(config->url, "https://flash.example.com/", 26));
    struct mock_http *h = calloc(1, sizeof(*h));
    strlcpy(h->url, config->url, sizeof(h->url));
    h->user_data = config->user_data;
    h->event_handler = config->event_handler;
    http_allocations++;
    http_live++;
    return h;
}
esp_err_t esp_http_client_set_url(esp_http_client_handle_t h, const char *url)
{
    strlcpy(h->url, url, sizeof(h->url));
    return ESP_OK;
}
esp_err_t esp_http_client_set_timeout_ms(esp_http_client_handle_t h, int timeout)
{
    (void)h;
    assert(timeout > 0);
    return ESP_OK;
}
esp_err_t esp_http_client_delete_header(esp_http_client_handle_t h, const char *name)
{
    (void)h; (void)name; return ESP_OK;
}
esp_err_t esp_http_client_open(esp_http_client_handle_t h, size_t length)
{
    (void)length;
    if (reply_index >= replies_count)
        fprintf(stderr, "case %d unexpected URL %s\n", test_case, h->url);
    assert(reply_index < replies_count);
    struct reply *r = &replies[reply_index++];
    assert(strstr(h->url, r->path));
    if (strstr(h->url, "/poll") && !in_poll) {
        iris_bridge_snapshot_t pairing;
        iris_bridge_get_snapshot(&pairing);
        if (!strcmp(pairing.state, "PAIRING")) {
            assert(!strcmp(pairing.expires_at, TEST_PAIRING_EXPIRY));
            pairing_snapshots++;
        }
    }
    h->reply = r;
    h->offset = 0;
    request_started[requests++] = mock_time;
    mock_time += r->delay_us;
    if (r->retry_after) {
        esp_http_client_event_t event = {.event_id = HTTP_EVENT_ON_HEADER,
            .header_key = "Retry-After", .header_value = r->retry_after,
            .user_data = h->user_data};
        h->event_handler(&event);
    }
    return r->status ? 0 : ESP_FAIL;
}
int esp_http_client_fetch_headers(esp_http_client_handle_t h)
{
    return strlen(h->reply->body);
}
int esp_http_client_get_status_code(esp_http_client_handle_t h)
{
    return h->reply->status;
}
int esp_http_client_read(esp_http_client_handle_t h, char *out, size_t n)
{
    if (in_result && block_result)
        while (!atomic_load(&release_result)) host_pause();
    if (in_poll && slow_poll) {
        atomic_store(&poll_blocked, true);
        while (!atomic_load(&release_poll)) host_pause();
    }
    if (!in_poll && download_during_poll && strstr(h->url, "/files/")) {
        atomic_store(&mock_time, 6000000);
        while (!atomic_load(&poll_blocked)) host_pause();
    }
    size_t left = strlen(h->reply->body) - h->offset;
    if (n > left)
        n = left;
    if (n > 4096)
        n = 4096;
    memcpy(out, h->reply->body + h->offset, n);
    h->offset += n;
    if (h->reply->stop)
        iris_bridge_stop();
    if (pause_on_poll && strstr(h->reply->path, "/poll")) {
        iris_bridge_set_active(false);
        pause_on_poll = false;
        expire_on_delay = true;
    }
    return n;
}
int esp_http_client_write(esp_http_client_handle_t h, const char *data, size_t n)
{
    (void)h;
    (void)data;
    return n;
}
bool esp_http_client_is_complete_data_received(esp_http_client_handle_t h)
{
    return h->offset == strlen(h->reply->body);
}
void esp_http_client_set_header(esp_http_client_handle_t h, const char *key,
                                const char *value)
{
    (void)h;
    (void)key;
    (void)value;
}
void esp_http_client_set_method(esp_http_client_handle_t h, int method)
{
    (void)h;
    (void)method;
}
void esp_http_client_close(esp_http_client_handle_t h)
{
    (void)h;
}
void esp_http_client_cleanup(esp_http_client_handle_t h)
{
    http_live--;
    free(h);
}
bool esp_http_client_is_persistent_connection(esp_http_client_handle_t h)
{
    (void)h;
    return mock_keep_alive;
}
esp_err_t factory_system_metadata_store_last_result(const uint8_t *op, esp_err_t result)
{
    (void)op; (void)result;
    persisted_results++;
    return ESP_OK;
}
esp_err_t factory_system_metadata_load_last_result(factory_sysmeta_record_t *out)
{
    (void)out;
    return ESP_ERR_NOT_FOUND;
}
esp_err_t factory_system_update_source_reserve(factory_system_update_owner_t owner,
                                               const uint8_t *op)
{
    (void)owner;
    (void)op;
    if (mock_reserved)
        return ESP_ERR_INVALID_STATE;
    mock_reserved = 1;
    return 0;
}
esp_err_t factory_system_update_source_prepare(factory_system_update_owner_t owner,
                                               const uint8_t *json, size_t size,
                                               const uint8_t *op)
{
    (void)owner;
    (void)size;
    (void)op;
    cJSON *root = cJSON_Parse((const char *)json);
    assert(root);
    assert(!cJSON_GetObjectItem(root, "remote_bridge"));
    const cJSON *c = cJSON_GetArrayItem(cJSON_GetObjectItem(root, "components"), 0);
    descriptor.size = num(c, "size");
    descriptor.id = 1;
    descriptor.target_offset = num(c, "target_offset");
    descriptor.kind = !strcmp(str(c, "kind"), "recovery")
                          ? ESP_IRIS_SYSTEM_UPDATE_COMPONENT_RECOVERY
                          : ESP_IRIS_SYSTEM_UPDATE_COMPONENT_DATA;
    const char *sha = str(c, "sha256");
    for (int i = 0; i < 32; i++) {
        unsigned b;
        sscanf(sha + 2 * i, "%2x", &b);
        descriptor.sha256[i] = b;
    }
    cJSON_Delete(root);
    return 0;
}
esp_err_t factory_system_update_source_prepare_bridge(const cJSON *root,
                                                     const uint8_t *op,
                                                     bool allow_bootloader)
{
    system_prepares++;
    system_allow_bootloader = allow_bootloader;
    char *json = cJSON_PrintUnformatted(root);
    assert(json);
    esp_err_t err = factory_system_update_source_prepare(
        FACTORY_SYSTEM_UPDATE_OWNER_BRIDGE, (uint8_t *)json, strlen(json), op);
    free(json);
    return err;
}
size_t factory_system_update_source_component_count(factory_system_update_owner_t owner)
{
    (void)owner;
    return 1;
}
esp_err_t factory_system_update_source_component(factory_system_update_owner_t owner,
                                                 size_t index,
                                                 esp_iris_system_update_component_t *c,
                                                 char *file)
{
    (void)owner;
    (void)index;
    *c = descriptor;
    strcpy(file, "image");
    return 0;
}
esp_err_t factory_system_update_source_begin_component(
    factory_system_update_owner_t owner, const esp_iris_system_update_component_t *c)
{
    (void)owner;
    (void)c;
    allocation_count++;
    return mock_backend_alloc_fail ? ESP_ERR_NO_MEM : 0;
}
esp_err_t factory_system_update_source_write_component(
    factory_system_update_owner_t owner, const esp_iris_system_update_component_t *c,
    uint32_t offset, const uint8_t *data, size_t n)
{
    (void)owner;
    (void)c;
    (void)offset;
    (void)data;
    (void)n;
    assert(mock_wifi_ps == WIFI_PS_NONE || !mock_reserved);
    mock_writes++;
    return 0;
}
esp_err_t
factory_system_update_source_end_component(factory_system_update_owner_t owner,
                                           const esp_iris_system_update_component_t *c,
                                           const uint8_t *sha)
{
    (void)owner;
    return memcmp(c->sha256, sha, 32) ? ESP_ERR_INVALID_CRC : 0;
}
esp_err_t factory_system_update_source_commit(factory_system_update_owner_t owner,
                                              const uint8_t *op)
{
    (void)owner;
    (void)op;
    mock_commits++;
    if (!mock_commit_error) persisted_results++;
    return mock_commit_error;
}
bool factory_system_update_source_needs_restart(factory_system_update_owner_t owner)
{
    (void)owner;
    return mock_commits > 0;
}
void factory_system_update_source_abort(factory_system_update_owner_t owner,
                                        const uint8_t *op, esp_err_t err)
{
    (void)owner;
    (void)op;
    (void)err;
    mock_reserved = 0;
    mock_abort++;
}

static uint8_t pending_boot[128];
static size_t pending_size;
static unsigned erased_boot_records;
esp_err_t nvs_open_from_partition(const char *partition, const char *ns, int mode,
                                  nvs_handle_t *n)
{
    assert(!strcmp(partition, "sysmeta") && !strcmp(ns, "iris_bridge"));
    (void)mode;
    *n = 1;
    return ESP_OK;
}
esp_err_t nvs_get_blob(nvs_handle_t n, const char *key, void *out, size_t *size)
{
    (void)n;
    (void)key;
    if (!pending_size)
        return ESP_ERR_NVS_NOT_FOUND;
    if (*size < pending_size)
        return ESP_ERR_INVALID_SIZE;
    memcpy(out, pending_boot, pending_size);
    *size = pending_size;
    return ESP_OK;
}
esp_err_t nvs_set_blob(nvs_handle_t n, const char *key, const void *out, size_t size)
{
    (void)n;
    (void)key;
    assert(size <= sizeof(pending_boot));
    memcpy(pending_boot, out, size);
    pending_size = size;
    return ESP_OK;
}
esp_err_t nvs_erase_key(nvs_handle_t n, const char *key)
{
    (void)n;
    (void)key;
    pending_size = 0;
    erased_boot_records++;
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t n)
{
    (void)n;
    return ESP_OK;
}
void nvs_close(nvs_handle_t n)
{
    (void)n;
}

static bool ready(void)
{
    return mock_network;
}
static iris_bridge_config_t config = {.server_url = "https://flash.example.com",
                                      .board_id = "test-s31",
                                      .device_id = "12345678901234567890123456789012",
                                      .enable_factory_update = true,
                                      .network_ready = ready};
static void reset(void)
{
    assert(mock_wifi_ps == WIFI_PS_MIN_MODEM);
    assert(!atomic_load(&cancel_poll_running));
    join_poll_thread();
    /* Direct execute() calls below emulate the worker cleanup on return. */
    close_client(&main_client);
    close_client(&download_client);
    assert(http_live == 0);
    persisted_results = 0;
    block_result = false;
    atomic_store(&release_result, false);
    write_deadline = 0;
    separate_authorization = false;
    atomic_store(&telemetry_revision, 0);
    atomic_store(&report_running, false);
    http_allocations = 0;
    mock_keep_alive = slow_poll = download_during_poll = direct_poll_time = false;
    atomic_store(&poll_sleeping, false);
    atomic_store(&poll_blocked, false);
    atomic_store(&release_poll, false);
    atomic_store(&cancel_poll_stop, false);
    test_case++;
    system_prepares = 0;
    system_allow_bootloader = false;
    cfg.enable_system_update = false;
    cfg.enable_bootloader_update = false;
    expire_on_delay = false;
    activate_after_delays = 0;
    pause_on_poll = false;
    atomic_store(&bridge_active, true);
    atomic_store(&bridge_running, false);
    atomic_store(&stop_requested, false);
    memset(session, 0, sizeof(session));
    memset(token, 0, sizeof(token));
    mock_time = 0;
    mock_network = true;
    mock_stop_on_delay = false;
    mock_create_fail = 0;
    mock_alloc_fail = 0;
    mock_commit_error = 0;
    mock_writes = 0;
    mock_commits = 0;
    mock_reserved = 0;
    mock_abort = 0;
    restart_count = 0;
    requests = 0;
    allocation_count = 0;
    mock_backend_alloc_fail = 0;
    replies_count = 0;
    reply_index = 0;
    cancelled = false;
    set_state("IDLE", 0, 0);
}
static void reply(const char *path, int status, const char *body, bool stop)
{
    replies[replies_count++] = (struct reply){path, body, status, stop};
}
static void run_worker(void)
{
    if (!setjmp(worker_exit))
        mock_worker(NULL);
}
static const char *registration =
    "{\"session_id\":\"12345678901234567890123456789012\",\"auth_token\":\"SECRET\","
    "\"device_code\":\"ABCDE-12345\",\"expires_at\":\"" TEST_PAIRING_EXPIRY "\"}";
static cJSON *plan(const char *mode)
{
    cJSON *p = cJSON_CreateObject();
    char hash[65];
    assert(flash_hash(0x8000, 4096, hash));
    cJSON_AddStringToObject(p, "mode", mode);
    cJSON_AddStringToObject(p, "source_table_sha256", hash);
    cJSON_AddStringToObject(p, "target_table_sha256", hash);
    cJSON_AddStringToObject(p, "boot_partition", "factory");
    cJSON *images = cJSON_AddArrayToObject(p, "images"), *im = cJSON_CreateObject();
    cJSON_AddItemToArray(images, im);
    cJSON_AddStringToObject(im, "partition",
                            !strcmp(mode, "factory") ? "factory" : "data");
    cJSON_AddStringToObject(im, "upload_id", "image");
    cJSON_AddNumberToObject(im, "offset", 0x210000);
    cJSON_AddNumberToObject(im, "size", 4);
    uint8_t digest_bytes[32];
    digest("data", 4, digest_bytes);
    hex(digest_bytes, 32, hash);
    cJSON_AddStringToObject(im, "sha256", hash);
    cJSON *m = cJSON_AddObjectToObject(p, "factory_manifest");
    cJSON_AddStringToObject(m, "board_id", "test-s31");
    cJSON_AddStringToObject(m, "profile_id", "iris-s31-layout-v1");
    cJSON_AddStringToObject(m, "recovery_version", "3.0");
    cJSON_AddNumberToObject(m, "protocol_version", 1);
    return p;
}

static cJSON *system_plan(void)
{
    cJSON *p = plan("system_update");
    cJSON *im = cJSON_GetArrayItem(cJSON_GetObjectItem(p, "images"), 0);
    cJSON_AddNumberToObject(im, "component_id", 1);
    cJSON_AddStringToObject(im, "kind", "data");
    cJSON *manifest = cJSON_AddObjectToObject(p, "system_manifest");
    cJSON_AddStringToObject(manifest, "schema", "esp-iris-system-update/v1");
    cJSON_AddStringToObject(manifest, "target_layout_sha256", str(p, "target_table_sha256"));
    cJSON_AddBoolToObject(manifest, "preserve_layout", true);
    cJSON *components = cJSON_AddArrayToObject(manifest, "components");
    cJSON_AddItemToArray(components, component(1, "data", 0x210000, 4,
                                             str(im, "sha256"), "image"));
    return p;
}

static void run_poll_clock(void)
{
    direct_poll_time = true;
    atomic_store(&cancel_poll_running, true);
    if (!setjmp(worker_exit))
        cancel_poll_run(NULL);
    assert(!atomic_load(&cancel_poll_running));
    direct_poll_time = false;
}
static void test_cancel_poll(void)
{
    /* A slow HTTPS cancellation response must not stop payload reads/writes. */
    reset();
    descriptor = (esp_iris_system_update_component_t){
        .id = 1, .kind = ESP_IRIS_SYSTEM_UPDATE_COMPONENT_DATA, .size = 4};
    digest("data", 4, descriptor.sha256);
    slow_poll = download_during_poll = true;
    reply("/files/", 200, "data", false);
    reply("/poll", 200, "{\"flash\":{\"cancel_requested\":true}}", false);
    assert(cancel_poll_start() == ESP_OK);
    assert(transfer("image", &descriptor, NULL) == ESP_OK);
    assert(mock_writes == 1 && atomic_load(&poll_blocked) && !cancelled);
    /* Deliver remote cancellation only after the file made progress. */
    atomic_store(&release_poll, true);
    while (atomic_load(&cancel_poll_running)) host_pause();
    cancel_poll_join();
    assert(should_cancel() && !http_live);

    /* Completed polls reuse one TLS client and retain a remote cancellation. */
    reset();
    mock_keep_alive = true;
    reply("/poll", 200, "{\"flash\":{\"cancel_requested\":false}}", false);
    reply("/poll", 200, "{\"flash\":{\"cancel_requested\":true}}", false);
    run_poll_clock();
    assert(requests == 2 && http_allocations == 1 && !http_live && cancelled);
    assert(request_started[0] == 5000000 && request_started[1] == 10000000);

    /* A slow failed poll backs off from completion and respects Retry-After.
     * It cannot overwrite the foreground request's status/header state. */
    reset();
    mock_keep_alive = true;
    main_response = (http_response_t){.status = 202, .retry_after_seconds = 77};
    reply("/poll", 503, "{}", false);
    replies[0].delay_us = 7000000;
    replies[0].retry_after = "12";
    reply("/poll", 200, "{}", false);
    reply("/poll", 410, "{}", false);
    run_poll_clock();
    assert(request_started[0] == 5000000 && request_started[1] == 24000000 &&
           request_started[2] == 29000000);
    assert(http_allocations == 2 && !http_live && cancelled);
    assert(main_response.status == 202 && main_response.retry_after_seconds == 77);

    /* Malformed responses discard the connection; never reuse unread/error data. */
    reset();
    mock_keep_alive = true;
    reply("/poll", 200, "{", false);
    reply("/poll", 200, "{\"flash\":{\"cancel_requested\":true}}", false);
    run_poll_clock();
    assert(http_allocations == 2 && !http_live && cancelled);
    assert(request_started[1] == 15000000);

    /* Local stop during a blocked query joins and releases its HTTP client. */
    reset();
    slow_poll = true;
    reply("/poll", 200, "{}", false);
    assert(cancel_poll_start() == ESP_OK);
    mock_time = 6000000;
    while (!atomic_load(&poll_blocked)) host_pause();
    iris_bridge_stop();
    assert(should_cancel());
    atomic_store(&release_poll, true);
    cancel_poll_join();
    assert(!http_live);

    /* Failing to allocate the observer must abort before any component write. */
    reset();
    mock_create_fail = 1;
    cJSON *p = plan("partitions");
    assert(execute("12345678901234567890123456789012", p) == ESP_ERR_NO_MEM);
    assert(!mock_reserved && !requests && !mock_writes && !mock_commits);
    cJSON_Delete(p);
    reset();
}

static void test_authorization_and_result(void)
{
    /* Dropped/coalesced telemetry cannot block a protocol-2 commit. */
    reset();
    separate_authorization = true;
    mock_keep_alive = true;
    cJSON *p = plan("partitions");
    reply("/files/", 200, "data", false);
    reply("/authorize", 200, "{\"phase\":\"COMMITTING\",\"authorized\":true}", false);
    reply("/progress", 200, "{\"phase\":\"DONE\"}", false);
    if (!setjmp(worker_exit)) execute("12345678901234567890123456789012", p);
    assert(mock_commits == 1 && persisted_results == 1 && restart_count == 1);
    assert(http_allocations == 2 && !http_live); /* File and control, DONE reuses control. */
    cJSON_Delete(p);

    /* Registration negotiation and PRECHECK lease, then a denied commit. */
    reset();
    p = plan("partitions");
    cJSON_AddStringToObject(p, "phase", "PRECHECK");
    cJSON_AddNumberToObject(p, "write_authorization_ttl_ms", 60000);
    char *accepted = cJSON_PrintUnformatted(p);
    cJSON *reg = cJSON_Parse(registration);
    cJSON_AddNumberToObject(reg, "control_protocol", 2);
    char *registered = cJSON_PrintUnformatted(reg);
    cJSON_Delete(reg);
    mock_keep_alive = true;
    reply("device-sessions", 201, registered, false);
    reply("/poll", 200, "{\"flash\":{\"phase\":\"QUEUED\"}}", false);
    reply("/authorize", 200, accepted, false);
    reply("/files/", 200, "data", false);
    reply("/authorize", 409, "{}", false);
    reply("/progress", 200, "{\"phase\":\"FAILED\"}", false);
    assert(iris_bridge_start(&config) == ESP_OK);
    run_worker();
    assert(mock_writes == 1 && mock_commits == 0 && restart_count == 0 && requests == 6);
    assert(http_allocations == 3 && !http_live && write_deadline > mock_time);
    free(registered); free(accepted); cJSON_Delete(p);

    /* Fully consumed component downloads reuse one handle across file URLs. */
    reset();
    mock_keep_alive = true;
    descriptor = (esp_iris_system_update_component_t){.id=1, .kind=ESP_IRIS_SYSTEM_UPDATE_COMPONENT_DATA, .size=4};
    digest("data", 4, descriptor.sha256);
    reply("/files/one", 200, "data", false);
    reply("/files/two", 200, "data", false);
    assert(transfer("one", &descriptor, NULL) == ESP_OK);
    assert(transfer("two", &descriptor, NULL) == ESP_OK);
    assert(http_allocations == 1 && http_live == 1 && mock_writes == 2);

    /* HTTP 200 alone does not grant critical write permission. */
    reset();
    separate_authorization = true;
    p = plan("partitions");
    reply("/files/", 200, "data", false);
    reply("/authorize", 200, "{}", false);
    assert(execute("12345678901234567890123456789012", p) != ESP_OK);
    assert(mock_writes && !mock_commits && !mock_reserved);
    cJSON_Delete(p);

    /* A stalled final HTTPS body cannot postpone reboot beyond 2 seconds. */
    reset();
    separate_authorization = true;
    block_result = true;
    p = plan("partitions");
    reply("/files/", 200, "data", false);
    reply("/authorize", 200, "{\"phase\":\"COMMITTING\",\"authorized\":true}", false);
    reply("/progress", 200, "{\"phase\":\"DONE\"}", false);
    if (!setjmp(worker_exit)) execute("12345678901234567890123456789012", p);
    assert(restarted_at == 2000000 && mock_commits == 1 && persisted_results == 1);
    cJSON_Delete(p);

    /* Changed URLs reuse a fully consumed connection; failures discard it. */
    reset();
    mock_keep_alive = true;
    reply("/first", 200, "{}", false);
    reply("/second", 200, "{}", false);
    reply("/third", 503, "{}", false);
    reply("/fourth", 200, "{}", false);
    cJSON_Delete(request("/first", NULL));
    cJSON_Delete(request("/second", NULL));
    assert(http_allocations == 1 && http_live == 1);
    assert(!request("/third", NULL) && !http_live);
    cJSON_Delete(request("/fourth", NULL));
    assert(http_allocations == 2 && http_live == 1);

    /* A lost progress POST is retried; explicit remote cancellation still stops. */
    reset();
    reply("/progress", 503, "{}", false);
    reply("/progress", 200, "{\"phase\":\"WRITING\",\"cancel_requested\":true}", false);
    notify_progress(30);
    run_poll_clock();
    assert(requests == 2 && request_started[0] == 1000000 &&
           request_started[1] == 11000000 && cancelled);
    reset();
    write_deadline = 1;
    mock_time = 2;
    assert(should_cancel() && !authorize_commit() && !requests);
    reset();
}

int main(void)
{
    iris_bridge_snapshot_t out;
    reset();
    iris_bridge_config_t empty = config;
    empty.server_url = "";
    assert(iris_bridge_start(&empty) != 0);
    iris_bridge_get_snapshot(&out);
    assert(!strcmp(out.state, "NOT_CONFIGURED"));
    assert(!requests);
    reset();
    mock_network = false;
    assert(iris_bridge_start(&config) == 0);
    assert(iris_bridge_start(&config) == ESP_ERR_INVALID_STATE);
    iris_bridge_get_snapshot(&out);
    assert(!strcmp(out.state, "WAITING_NETWORK"));
    mock_stop_on_delay = true;
    run_worker();
    assert(!requests);
    assert(!iris_bridge_is_running());
    iris_bridge_get_snapshot(&out);
    assert(!strcmp(out.state, "CANCELLED"));
    reset();
    mock_create_fail = 1;
    assert(iris_bridge_start(&config) == ESP_ERR_NO_MEM);
    assert(!iris_bridge_is_running());
    reset();
    reply("device-sessions", 201, registration, false);
    reply("/poll", 410, "{}", false);
    assert(iris_bridge_start(&config) == 0);
    run_worker();
    iris_bridge_get_snapshot(&out);
    assert(!out.running && !out.code[0] && !token[0]);
    assert(!strcmp(out.state, "ENDED"));
    assert(requests == 2);
    assert(pairing_snapshots > 0);
    /* Re-entry after completed cleanup allocates exactly one new session. */
    reply("device-sessions", 201, registration, false);
    reply("/poll", 401, "{}", false);
    assert(iris_bridge_start(&config) == 0);
    run_worker();
    assert(requests == 4);
    reset();
    reply("device-sessions", 201, registration, false);
    expire_on_delay = true;
    iris_bridge_config_t background = config;
    background.prefetch_only = true;
    assert(iris_bridge_start(&background) == 0);
    run_worker();
    iris_bridge_get_snapshot(&out);
    assert(!strcmp(out.state, "EXPIRED"));
    assert(!out.code[0] && requests == 1);
    /* Opening a prefetched session enables polling, without another POST. */
    reset();
    reply("device-sessions", 201, registration, false);
    reply("/poll", 410, "{}", false);
    activate_after_delays = 2;
    assert(iris_bridge_start(&background) == 0);
    run_worker();
    assert(requests == 2 && !mock_writes && !mock_commits);
    /* A visible expired code renews immediately; the new session is polled. */
    reset();
    reply("device-sessions", 201, registration, false);
    reply("device-sessions", 201, registration, false);
    reply("/poll", 410, "{}", false);
    expire_on_delay = true;
    assert(iris_bridge_start(&config) == 0);
    run_worker();
    assert(requests == 3);
    /* Leaving during /poll must not process its queued update or inventory. */
    reset();
    reply("device-sessions", 201, registration, false);
    reply("/poll", 200, "{\"inventory_required\":true,\"flash\":{\"phase\":\"QUEUED\"}}", false);
    pause_on_poll = true;
    assert(iris_bridge_start(&config) == 0);
    run_worker();
    assert(requests == 2 && !mock_reserved && !mock_writes && !mock_commits);
    reset();
    for (int i = 0; i < 3; ++i) reply("device-sessions", 503, "{}", false);
    assert(iris_bridge_start(&background) == 0);
    run_worker();
    iris_bridge_get_snapshot(&out);
    assert(requests == 3 && !out.running && !strcmp(out.state, "FAILED"));
    reset();
    reply("device-sessions", 201, registration, true);
    reply("/progress", 200, "{}", false);
    assert(iris_bridge_start(&config) == 0);
    run_worker();
    assert(!token[0]);
    iris_bridge_get_snapshot(&out);
    assert(!strcmp(out.state, "CANCELLED"));
    reset();
    assert(iris_bridge_start(&config) == 0);
    atomic_store(&bridge_running, false);
    cJSON *p = plan("factory");
    mock_alloc_fail = 1;
    assert(execute("12345678901234567890123456789012", p) == ESP_ERR_NO_MEM);
    assert(!mock_reserved && !mock_writes && !mock_commits);
    cJSON_Delete(p);
    reset();
    p = plan("factory");
    cJSON_ReplaceItemInObject(cJSON_GetObjectItem(p, "factory_manifest"), "board_id",
                              cJSON_CreateString("wrong"));
    assert(execute("12345678901234567890123456789012", p) == ESP_ERR_INVALID_VERSION);
    assert(!mock_reserved && !requests);
    cJSON_Delete(p);
    reset();
    p = plan("partitions");
    mock_reserved = 1;
    assert(execute("12345678901234567890123456789012", p) == ESP_ERR_INVALID_STATE);
    assert(!requests && !mock_abort);
    cJSON_Delete(p);
    reset();
    p = plan("factory");
    reply("/files/", 200, "data", true);
    assert(execute("12345678901234567890123456789012", p) != 0);
    assert(!mock_writes && !mock_commits && !mock_reserved);
    cJSON_Delete(p);
    /* A rejected critical acknowledgement must never invoke commit. */
    reset();
    p = plan("partitions");
    reply("/files/", 200, "data", false);
    reply("/progress", 200, "{\"phase\":\"WRITING\"}", false);
    reply("/progress", 200, "{\"phase\":\"VERIFYING\"}", false);
    reply("/progress", 409, "{}", false);
    assert(execute("12345678901234567890123456789012", p) != 0);
    assert(mock_writes == 1 && !mock_commits && !mock_reserved);
    cJSON_Delete(p);
    /* Once authorized, local cancellation cannot interrupt critical commit. */
    reset();
    p = plan("partitions");
    reply("/files/", 200, "data", false);
    reply("/progress", 200, "{\"phase\":\"WRITING\"}", false);
    reply("/progress", 200, "{\"phase\":\"VERIFYING\"}", false);
    reply("/progress", 200, "{\"phase\":\"COMMITTING\"}", true);
    reply("/progress", 401, "{}", false);
    if (!setjmp(worker_exit))
        execute("12345678901234567890123456789012", p);
    assert(mock_commits == 1 && restart_count == 1);
    cJSON_Delete(p);
    reset();
    p = plan("partitions");
    mock_commit_error = ESP_FAIL;
    reply("/files/", 200, "data", false);
    reply("/progress", 200, "{\"phase\":\"WRITING\"}", false);
    reply("/progress", 200, "{\"phase\":\"VERIFYING\"}", false);
    reply("/progress", 200, "{\"phase\":\"COMMITTING\"}", false);
    reply("/progress", 200, "{\"phase\":\"DONE\"}", false);
    if (!setjmp(worker_exit))
        execute("12345678901234567890123456789012", p);
    assert(mock_commits == 1 && restart_count == 1);
    cJSON_Delete(p);
    reset();
    p = plan("partitions");
    cJSON_ReplaceItemInObject(p, "boot_partition", cJSON_CreateString("ota_0"));
    assert(save_boot(p) == ESP_OK && pending_size);
    if (!setjmp(worker_exit))
        iris_bridge_resume_boot();
    assert(restart_count == 1 && !pending_size && erased_boot_records == 1);
    assert(iris_bridge_resume_boot() == ESP_OK && restart_count == 1);
    assert(save_boot(p) == ESP_OK);
    ((boot_record_t *)pending_boot)->table_hash[0] ^= 1;
    assert(iris_bridge_resume_boot() == ESP_ERR_INVALID_VERSION && !pending_size);
    pending_size = sizeof(pending_boot);
    assert(iris_bridge_resume_boot() == ESP_ERR_INVALID_SIZE && !pending_size);
    cJSON_Delete(p);
    /* Original 3072-byte transport and FF-padded 4096-byte hash are distinct. */
    reset();
    char raw[3073];
    memset(raw, 0xff, 3072);
    raw[3072] = 0;
    p = plan("layout");
    char original_hash[65];
    uint8_t sha[32], table[4096];
    digest(raw, 3072, sha);
    hex(sha, 32, original_hash);
    cJSON_AddNumberToObject(p, "table_size", 3072);
    cJSON_AddStringToObject(p, "table_upload_id", "table");
    cJSON_AddStringToObject(p, "table_sha256", original_hash);
    reply("/files/", 200, raw, false);
    assert(get_table(p, table) == ESP_OK);
    for (size_t i = 0; i < sizeof(table); i++)
        assert(table[i] == 0xff);
    cJSON_ReplaceItemInObject(p, "table_sha256", cJSON_CreateString("bad"));
    reply("/files/", 200, raw, false);
    assert(get_table(p, table) != ESP_OK);
    reply("/files/", 410, "{}", false);
    assert(get_table(p, table) != ESP_OK);
    assert(!mock_writes && !mock_commits);
    cJSON_Delete(p);
    /* Full bundle mode is locally gated before writer or network activity. */
    reset();
    p = system_plan();
    assert(execute("12345678901234567890123456789012", p) == ESP_ERR_NOT_SUPPORTED);
    assert(!mock_reserved && !requests && !system_prepares);
    cJSON_Delete(p);
    /* A leased descriptor mismatch cannot reach the backend. */
    reset();
    cfg.enable_system_update = true;
    p = system_plan();
    cJSON_ReplaceItemInObject(cJSON_GetArrayItem(cJSON_GetObjectItem(p, "images"), 0),
                              "component_id", cJSON_CreateNumber(2));
    assert(execute("12345678901234567890123456789012", p) == ESP_ERR_INVALID_ARG);
    assert(!mock_reserved && !requests && !system_prepares);
    cJSON_Delete(p);
    /* Source layout is rechecked under the shared reservation. */
    reset();
    cfg.enable_system_update = true;
    p = system_plan();
    cJSON_ReplaceItemInObject(p, "source_table_sha256", cJSON_CreateString("stale"));
    assert(execute("12345678901234567890123456789012", p) == ESP_ERR_INVALID_VERSION);
    assert(!mock_reserved && !requests && !system_prepares && mock_abort == 1);
    cJSON_Delete(p);
    /* The actual new worker branch streams through the shared backend and
     * restarts after commit; no extra Recovery-sized transport buffer exists. */
    reset();
    cfg.enable_system_update = true;
    cfg.enable_bootloader_update = true;
    p = system_plan();
    reply("/files/", 200, "data", false);
    reply("/progress", 200, "{\"phase\":\"WRITING\"}", false);
    reply("/progress", 200, "{\"phase\":\"VERIFYING\"}", false);
    reply("/progress", 200, "{\"phase\":\"COMMITTING\"}", false);
    reply("/progress", 200, "{\"phase\":\"DONE\"}", false);
    if (!setjmp(worker_exit))
        execute("12345678901234567890123456789012", p);
    assert(system_prepares == 1 && system_allow_bootloader);
    assert(mock_writes == 1 && mock_commits == 1 && restart_count == 1);
    cJSON_Delete(p);
    test_cancel_poll();
    test_authorization_and_result();
    puts("Bridge worker, cancellation and transaction gates passed");
}
