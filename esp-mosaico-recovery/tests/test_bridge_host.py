"""Compile the actual Bridge worker against deterministic HTTP/RTOS/Flash fakes."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
FIXTURE = ROOT / "tests" / "host_bridge"
BRIDGE = ROOT / "firmware" / "recovery" / "components" / "iris_bridge"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux host fixtures require OpenSSL development headers")
@pytest.mark.parametrize("kind", ["worker", "backend", "control"])
def test_bridge_worker_and_transaction_gates(tmp_path, kind):
    backend = kind == "backend"
    cc = shutil.which("cc")
    assert cc, "Host C compiler is required"
    cjson = Path(os.environ.get("CJSON_COMPONENT_DIR", str(ROOT / "firmware/recovery/managed_components/espressif__cjson")))
    headers = list(cjson.rglob("cJSON.h"))
    assert headers, "Build Recovery first or set CJSON_COMPONENT_DIR to espressif/cjson"
    source = ROOT / "firmware/recovery/main/factory_system_update.c" if backend else BRIDGE / "iris_bridge.c"
    if kind == "control":
        source = ROOT / "firmware/recovery/main/factory_recovery_control.c"
    includes = re.findall(r'#include "([^"]+)"', source.read_text())
    for name in includes + ["esp_err.h"]:
        if name in {"iris_bridge.h", "system_plan.h", "cJSON.h", "factory_system_metadata.h", "factory_system_update.h", "esp_iris_system_update.h", "factory_ui.h", "factory_network.h", "factory_recovery_control.h"}:
            continue
        header = tmp_path / name
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text('#include "{}"\n'.format('sdk.h' if kind == 'worker' else kind + '_sdk.h'))
    cjson_source = headers[0].with_name("cJSON.c")
    assert cjson_source.is_file()
    executable = tmp_path / "bridge-test"
    command = [cc, "-std=gnu11", "-g", "-Werror=implicit-function-declaration", "-Wno-deprecated-declarations",
               '-D{}="{}"'.format('BRIDGE_SOURCE' if kind == 'worker' else kind.upper() + '_SOURCE', source),
               "-I" + str(tmp_path), "-I" + str(FIXTURE), "-I" + str(headers[0].parent),
               "-I" + str(ROOT / "include"), "-I" + str(BRIDGE / "include"), "-I" + str(ROOT / "firmware/recovery/main"),
               "-I" + str(REPO / "ESP-Iris/components/esp_iris/include"),
               str(FIXTURE / ("main.c" if kind == "worker" else kind + ".c")), str(cjson_source), "-lcrypto", "-pthread", "-o", str(executable)]
    if kind == "worker":
        command.append(str(BRIDGE / "system_plan.c"))
    if backend:
        command.append("-DBACKEND_TEST")
    if os.environ.get("IRIS_HOST_SANITIZERS") == "1":
        command.extend(["-fsanitize=address,undefined", "-fno-omit-frame-pointer"])
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
