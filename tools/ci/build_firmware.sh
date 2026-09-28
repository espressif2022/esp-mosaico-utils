#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
iris_root="$repository_root/ESP-Iris"
build_group="${1:-all}"

usage() {
    echo "usage: $0 [all|baseline|media|ota|system]" >&2
    exit 2
}

[[ "$#" -le 1 ]] || usage

build_iris() {
    local name="$1"
    local project="$2"
    local defaults="${3:-}"
    local profile="${4:-}"
    local build_dir="$iris_root/build-ci-$name"
    local arguments=(
        -C "$iris_root/components/esp_iris/$project"
        -B "$build_dir"
        -D "SDKCONFIG=$build_dir/sdkconfig"
    )
    if [[ -n "$defaults" ]]; then
        arguments+=(-D "SDKCONFIG_DEFAULTS=$defaults")
    fi
    if [[ -n "$profile" ]]; then
        arguments+=(-D "ESP_IRIS_BUILD_PROFILE=$profile")
    fi
    idf.py "${arguments[@]}" build
}

build_baseline() {
    build_iris tcp examples/minimal "" tcp
    build_iris usb examples/minimal sdkconfig.usb.defaults usb
    build_iris usj examples/minimal sdkconfig.usj.defaults usj
    build_iris multi examples/minimal sdkconfig.multi.defaults
    build_iris tcp-wifi examples/tcp_wifi
    build_iris tcp-pairing examples/tcp_pairing
    build_iris rpc-jobs examples/rpc_jobs
    build_iris rpc-jobs-usj examples/rpc_jobs sdkconfig.usj.defaults usj
}

build_media() {
    build_iris display-input examples/display_input
    build_iris media-streams examples/media_streams
    build_iris media-rgb888 examples/media_streams 'sdkconfig.defaults;sdkconfig.rgb888.defaults'
    build_iris media-jpeg examples/media_streams 'sdkconfig.defaults;sdkconfig.jpeg.defaults'
    build_iris media-png examples/media_streams 'sdkconfig.defaults;sdkconfig.png.defaults'
    build_iris file-transfer examples/file_transfer
    build_iris file-service examples/file_service
    build_iris lifecycle examples/lifecycle
}

build_ota() {
    build_iris crash-recovery examples/crash_recovery sdkconfig.recovery.defaults
    build_iris crash-application examples/crash_recovery sdkconfig.application.defaults
    build_iris ota-recovery examples/ota sdkconfig.recovery.defaults
    build_iris ota-a examples/ota
    build_iris ota-b examples/ota sdkconfig.candidate.defaults
    build_iris ota-rollback examples/ota sdkconfig.rollback.defaults
    build_iris ota-application examples/ota sdkconfig.application.defaults
    build_iris ota-fallback examples/ota sdkconfig.fallback.defaults
    build_iris ota-project-match examples/ota sdkconfig.project-match.defaults
}

build_system() {
    build_iris services-usb test_apps/services_usb
    build_iris coredump test_apps/coredump
    build_iris system-inventory test_apps/system_inventory
    build_iris system-update test_apps/system_update
    build_iris disabled test_apps/disabled

    local recovery_build="$repository_root/build-ci-recovery-factory"
    idf.py \
        -C "$repository_root/esp-mosaico-recovery/firmware/recovery" \
        -B "$recovery_build" \
        -D "SDKCONFIG=$recovery_build/sdkconfig" \
        build
}

check_artifact_budgets() {
    python "$iris_root/tools/check_esp_iris_budgets.py" \
        --group artifacts_bytes \
        --output "$iris_root/budget-results.json"
}

case "$build_group" in
    baseline) build_baseline ;;
    media) build_media ;;
    ota) build_ota ;;
    system)
        build_system
        check_artifact_budgets
        ;;
    all)
        build_baseline
        build_media
        build_ota
        build_system
        check_artifact_budgets
        ;;
    *) usage ;;
esac
