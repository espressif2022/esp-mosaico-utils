---
name: esp-iris-gateway-control
description: Observe and control ESP-Iris devices exclusively through the component-contained gateway CLI, with default credential-free loopback access, authenticated remote access, JSON output, device-operation evidence, OTA acceptance, and observe/develop mode handling.
target: esp32s31
tags: [agent, esp-iris, gateway, cli, usb-highspeed, ota, recovery, logs]
---

# Control devices through the ESP-Iris gateway

Use this Skill for every Agent action that reaches an ESP-Iris device. The
gateway is the only device-operation entry. Do not open serial/USB directly,
do not call the Hub Python modules, and do not use a device-control MCP. Code
edits, builds, and PC shell commands are outside the device operation timeline
and may use their normal repository workflows.

## Preconditions

1. Require Python 3.8 or newer and run the source entrypoint from this
   repository:

   ```bash
   python3 common_components/esp_iris/tools/esp_iris.py
   ```

   Install `tools/requirements.lock` with the active interpreter; its Python
   version markers select the compatible dependency set automatically.

2. Local loopback access is authentication-free by default. For a remote
   gateway or one started with `--require-local-auth`, read the Agent token from
   `ESP_IRIS_AGENT_TOKEN` or `ESP_IRIS_AGENT_TOKEN_FILE`. Never put a token on
   the command line, in source, or in committed defaults.
3. Configure the CLI profile with the gateway URL. Trusted-LAN access defaults
   to HTTP; when the gateway uses `--tls`, configure its HTTPS URL and a CA
   certificate or SHA-256 fingerprint.
4. Use `ctl --json` for stable machine-readable output.

## Start a run

```bash
python3 common_components/esp_iris/tools/esp_iris.py doctor --json
python3 common_components/esp_iris/tools/esp_iris.py ctl --json devices
python3 common_components/esp_iris/tools/esp_iris.py ctl --json mode
python3 common_components/esp_iris/tools/esp_iris.py ctl --json status DEVICE_ID
```

The gateway normally starts in `develop`. If it is in `observe`, either keep
the run read-only or explicitly switch the shared global mode:

```bash
python3 common_components/esp_iris/tools/esp_iris.py ctl --json mode develop
```

No separate approval step is required for Agent device operations in develop
mode. Do not bypass observe-mode rejection.

## Observe

```bash
python3 common_components/esp_iris/tools/esp_iris.py ctl --json logs \
  --device DEVICE_ID --follow
python3 common_components/esp_iris/tools/esp_iris.py ctl --json crash DEVICE_ID
python3 common_components/esp_iris/tools/esp_iris.py ctl --json coredump DEVICE_ID coredump.bin
python3 common_components/esp_iris/tools/esp_iris.py ctl --json input DEVICE_ID \
  --gesture '{"begin":{"x":5000,"y":5000},"moves":[],"end":{"x":5000,"y":5000}}'
python3 common_components/esp_iris/tools/esp_iris.py ctl --json jobs DEVICE_ID JOB_ID
```

Correlate by stable `device_id`, `boot_id`, `operation_id` and event cursor.
Treat `history_gap`, `stale`, `outcome_unknown`, disconnect and recovery mode
as explicit states. Never infer device success from an accepted HTTP request.

## Operate

```bash
python3 common_components/esp_iris/tools/esp_iris.py ctl --json rpc \
  DEVICE_ID system.info --params '{}'
python3 common_components/esp_iris/tools/esp_iris.py ctl --json rpc-raw \
  DEVICE_ID SERVICE_ID METHOD_ID --payload '{}'
python3 common_components/esp_iris/tools/esp_iris.py ctl --json console \
  DEVICE_ID iris_info
python3 common_components/esp_iris/tools/esp_iris.py ctl --json cancel DEVICE_ID JOB_ID
python3 common_components/esp_iris/tools/esp_iris.py ctl --json screenshot DEVICE_ID device.png
python3 common_components/esp_iris/tools/esp_iris.py ctl --json mirror DEVICE_ID start --fps 5
python3 common_components/esp_iris/tools/esp_iris.py ctl --json mirror DEVICE_ID stop
python3 common_components/esp_iris/tools/esp_iris.py ctl --json restart DEVICE_ID
python3 common_components/esp_iris/tools/esp_iris.py ctl --json factory DEVICE_ID
```

Prefer RPC Catalog methods. Use raw RPC only when the method is not yet in the
catalog and report that compatibility remains caller-owned. A high-frequency
pointer/touch gesture must be submitted as one begin/moves/end operation, not
one operation per point. Console input is line-oriented and bounded; never mix
raw text into the framed CDC0 transport, and do not submit credentials through
the Console because command output and audit metadata are retained.

## OTA and recovery

```bash
python3 common_components/esp_iris/tools/esp_iris.py ctl --json ota \
  DEVICE_ID build/application.bin
python3 common_components/esp_iris/tools/esp_iris.py ctl --json operation-status OPERATION_ID
python3 common_components/esp_iris/tools/esp_iris.py ctl --json operation-watch OPERATION_ID
```

The CLI archives the matching BIN, ELF and map under the verified ELF SHA, and
the Gateway immediately returns a durable operation ID. Recovery-assisted OTA
is the default and has no silent application fallback. Query or watch the
operation for recovery entry, device Job ID, bytes, permille transfer progress,
slot verification, reconnect, new boot ID, expected project/version and
healthy acceptance. An upload response alone is not success.

Crash evidence is independent from OTA. Collect it when the crash workflow
requires it, and decode only with the exact archived ELF SHA; do not infer that
an adjacent OTA operation caused a crash.

On `outcome_unknown`, do not replay the write. Query status/operations and
observe device events to establish the result. In recovery firmware, keep
product services disabled and deploy only the intended normal application.

## Finish

Report the gateway URL/profile name, device ID, old/new boot IDs, operation
IDs, final structured statuses, version, healthy evidence, coredump artifact
if preserved, and unresolved gaps. The developer can observe the same evidence
in the Web workbench; do not create a separate Agent-only log.
