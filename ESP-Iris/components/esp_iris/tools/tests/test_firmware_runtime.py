"""Compile the complete device runtime/services with deterministic hardware stubs."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[2]
HOST = Path(__file__).parent / "runtime_host"


@pytest.mark.parametrize("service_profile", ["none", "ota", "inventory", "system-update",
                                            "ota-large", "system-update-large"])
@pytest.mark.parametrize("multi_transport", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("tick_rate_hz", [100, 1000])
def test_firmware_runtime(tmp_path: Path, multi_transport: bool, service_profile: str,
                          persistent: bool, tick_rate_hz: int) -> None:
    compiler = shutil.which("cc")
    if compiler is None:
        if os.environ.get("IRIS_HOST_SANITIZERS") == "1" or os.environ.get("IRIS_REQUIRE_HOST_CC") == "1":
            pytest.fail("required C compiler is missing; firmware validation cannot be skipped")
        pytest.skip("C compiler required for production firmware regression tests")
    output = tmp_path / ("runtime.exe" if os.name == "nt" else "runtime")
    flags = ["-std=c11", "-D_POSIX_C_SOURCE=200809L", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
    flags += [f"-DconfigTICK_RATE_HZ={tick_rate_hz}"]
    if os.environ.get("IRIS_HOST_SANITIZERS") == "1":
        flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-g"]
    if service_profile in {"ota", "system-update", "ota-large", "system-update-large"}:
        flags += ["-DCONFIG_ESP_IRIS_OTA=1"]
    if service_profile in {"inventory", "system-update", "system-update-large"}:
        flags += ["-DCONFIG_ESP_IRIS_SYSTEM_INVENTORY=1"]
    if service_profile in {"system-update", "system-update-large"}:
        flags += ["-DCONFIG_ESP_IRIS_SYSTEM_UPDATE=1"]
    if service_profile in {"ota-large", "system-update-large"}:
        flags += ["-DCONFIG_ESP_IRIS_OTA_CHUNK_BYTES=3968"]
    if service_profile == "system-update-large":
        flags += ["-DCONFIG_ESP_IRIS_SYSTEM_UPDATE_MANIFEST_BYTES=3072",
                  "-DCONFIG_ESP_IRIS_SYSTEM_UPDATE_SIGNATURE_BYTES=512",
                  "-DCONFIG_ESP_IRIS_SYSTEM_UPDATE_CHUNK_BYTES=2048"]
    if multi_transport:
        flags += ["-DCONFIG_ESP_IRIS_TRANSPORT_USB=1"]
    if persistent:
        flags += ["-DCONFIG_ESP_IRIS_SERVICE_PERSISTENT=1"]
        if service_profile in {"none", "inventory"}:
            flags += ["-DCONFIG_ESP_IRIS_RPC_RESPONSE_PSRAM=1",
                      "-DCONFIG_ESP_IRIS_SERVICE_STATE_PSRAM=1",
                      "-DCONFIG_ESP_IRIS_SERVICE_CONTEXT_PSRAM=1"]
    command = [compiler, *flags, "-include", str(HOST / "sdkconfig.h"),
               "-I", str(HOST), "-I", str(COMPONENT / "include"),
               "-I", str(COMPONENT / "src"), str(HOST / "runtime_test.c"),
               str(COMPONENT / "src" / "esp_iris_codec.c"),
               str(COMPONENT / "src" / "esp_iris_state.c"), "-o", str(output)]
    build = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(output)], capture_output=True, text=True, timeout=30, check=False)
    assert run.returncode == 0, run.stdout + run.stderr


def test_crash_recovery_runtime(tmp_path: Path) -> None:
    compiler = shutil.which("cc")
    if compiler is None:
        pytest.skip("C compiler required for crash recovery regression tests")
    output = tmp_path / ("crash-recovery.exe" if os.name == "nt" else "crash-recovery")
    command = [
        compiler,
        "-std=c11",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-Wno-unused-parameter",
        "-include",
        str(HOST / "sdkconfig.h"),
        "-I",
        str(HOST),
        "-I",
        str(COMPONENT / "include"),
        "-I",
        str(COMPONENT / "src"),
        str(HOST / "crash_recovery_test.c"),
        "-o",
        str(output),
    ]
    build = subprocess.run(
        command, capture_output=True, text=True, timeout=60, check=False
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(output)], capture_output=True, text=True, timeout=30, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
