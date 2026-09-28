# ESP-Iris Developer Gateway and Web Workbench

The Developer Gateway is the PC-side fleet hub for ESP-Iris. It supervises
multiple USB and TCP device endpoints concurrently, identifies devices by
stable `device_id`, exposes REST/WebSocket APIs, stores durable engineering
records, and serves the React Web Workbench. The `ctl` command-line client and
external agents use the same API concurrently with the Workbench.

Each physical device has at most one active Gateway session, but one Gateway
can own many device sessions. Every Workbench, CLI follow command, or external
agent WebSocket receives an independent event stream. Device-changing
operations are serialized per device; observers do not compete for events.

The Gateway is source distributed with the ESP-IDF component. It is not
installed as a Python package.

When launched through ESP-Mosaico's `mosaico.py`, the Gateway is owned by a
project session: discovery is passive, acquisition is explicit, and only the
current owner reconnects a device. Local `/v1/project` APIs report ownership
and perform reserved, idempotent device transfers. Shared project Gateways use independent client leases:
CLI clients renew while their command runs, every `iris run` retains its own
client, and each Web workbench event connection retains a client. With no
clients and no active work, the service shuts down after 10 idle seconds.
HTTP status queries do not retain clients; there is no stop endpoint. The
legacy owner-pipe mode remains readable during migration. Each project
keeps separate records; API/capability compatibility governs reuse. Exact source matching is optional.
The standalone `esp_iris.py web` commands below retain their existing automatic
connection behavior and do not participate in project ownership coordination.

## Requirements

- Python 3.8 or newer
- Linux, macOS, or Windows; real-board validation currently focuses on Linux
- Node.js and npm when the Workbench must be built from source
- A dedicated serial endpoint for USB transports

Set one path for the rest of this guide:

```bash
# Source checkout
ESP_IRIS_COMPONENT_DIR=components/esp_iris

# Component Manager installation
# ESP_IRIS_COMPONENT_DIR=managed_components/lisir233__esp_iris
```

PowerShell users can set the equivalent `$env:ESP_IRIS_COMPONENT_DIR` value and
use `python` when it is the Python 3 launcher.

## Install the Gateway

Create an isolated environment and install the runtime dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r "$ESP_IRIS_COMPONENT_DIR/tools/requirements.lock"
```

`requirements.lock` is shared across supported Python releases. Its PEP 508
markers make pip select the validated packages for the active interpreter
(including the Python 3.8-compatible aiohttp and zeroconf versions). This
Gateway interpreter is independent of the interpreter required by ESP-IDF.

This checkout includes a prebuilt Workbench in `tools/frontend/dist`, which
the Gateway serves directly. Node.js and npm are not required to open it.
The component file rules retain this distribution and exclude `node_modules`.
To rebuild after changing frontend source or its dependency lock:

```bash
cd "$ESP_IRIS_COMPONENT_DIR/tools/frontend"
npm ci
npm run build
cd -
```

Commit the regenerated `dist` files together with frontend source changes.
CI rebuilds from `package-lock.json` and checks that the committed distribution
matches. The Gateway displays a fallback page when `dist/index.html` is absent.

## Evaluate without hardware

Demo mode creates virtual normal/recovery devices and exercises logs, RPC/jobs,
screen/input, media, OTA, restart, disconnect, crash evidence, and the
file browser with upload and metadata mutations:

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web --demo
```

Open `http://127.0.0.1:8443/`.

## Connect devices

USB and TCP options are repeatable and may be combined. For example, one
Gateway process can supervise an application CDC device, a USB Serial/JTAG
device, and two network devices at the same time:

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web \
  --usb /dev/serial/by-id/usb-Espressif_ESP-Iris_A... \
  --usb-serial-jtag /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B... \
  --tcp 192.0.2.21:19772 \
  --tcp 192.0.2.22:19772
```

Automatic application CDC and mDNS discovery are enabled when no endpoints are
specified. With explicit USB or TCP endpoints, enable additional discovery with
`--discover-usb` and/or `--discover-mdns`. A device may wait on several configured transports, but a
validated HELLO_ACK selects exactly one active session; it never maintains
simultaneous USB and TCP sessions.

### Automatic TCP discovery with mDNS

The default process browses `_esp-iris._tcp.local.` on multicast-capable local
networks and supervises every valid advertised endpoint. Disable this when the
host should connect only to explicit endpoints:

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web --no-discover-mdns
```

Discovered services must advertise protocol version 1, a 32-character device
ID, TCP port, firmware mode, and `none` or `hmac` pairing metadata. The HELLO
identity must match the advertised device ID. Pairing tokens are never read
from mDNS; supply the existing `--pairing-token` option when required. mDNS
does not cross routed subnets unless the network provides an mDNS reflector.

### Automatic application USB discovery

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web
```

The default process discovers all visible ESP-Iris application CDC devices,
listens on `127.0.0.1:8443`, and serves the API and Workbench.

### Select an application USB port

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web \
  --usb /dev/serial/by-id/usb-Espressif_ESP-Iris_...
```

An explicit USB port may be absent at startup. The API and Workbench still
start, and the port is retried until it can be resolved and exclusively owned.
`GET /v1/endpoints` reports waiting, ambiguous, or owned-elsewhere conditions.
Aliases and automatic discovery share one physical endpoint lock and session.

Explicit `--usb` selection supports custom application VID/PID/product strings;
automatic discovery retains its ESP-Iris descriptor filter. Every connection
must complete the ESP-Iris handshake. Espressif `303A:1001` still requires the
Serial/JTAG opt-in below, and `303A:0020` ROM download ports remain reserved for
local ROM operation executor.

### Device state and local ROM operations

Device and endpoint inventories expose one `state`: `offline`, `connecting`,
`idle`, `busy`, or `needs_recovery`. `owner_session_id` and `firmware_mode`
(`normal`, `recovery`, `rom`, `unknown`) are independent fields. `busy_reasons`
identifies active operations, jobs and mirrors. Log viewers and client leases
alone do not make a device busy. An active operation remains busy across its
expected USB disconnect/re-enumeration; only live ROM descriptors justify a
`needs_recovery` diagnosis, not an HTTP timeout or a failed operation record.

Host-side ROM probes and Recovery installation use the ordinary operation
queue/history (`host.probe`, `host.recovery`). The local CLI publishes a private,
one-use request file and submits its ID to `POST /v1/host-operations`. Executable
commands and environment values are never accepted in the HTTP body or saved
in public operation parameters. Browser-origin and remote submissions are
rejected. Results are read through the existing operation status endpoint.

The operation blocks new work on all involved device/endpoint resources,
preserves available crash evidence, detaches the Iris connection, and starts a
separate worker. That worker owns the physical USB locks for the entire
foreground command lifetime. Closing the CLI or losing the Gateway does not
kill the writer or release its locks. A CLI wait timeout reports the operation
ID; it does not interrupt flash. Commands must wait for all their writers to
exit, rather than launching detached background work.

After writing, the Gateway reattaches and verifies the selected device, a new
Boot ID, Recovery mode, expected version/MAC and OTA capability. A failed write
or verification stays in operation history. Completed processes leave no
renewable reservation or token requiring manual cleanup. On Gateway restart,
interrupted writes are recorded as `outcome_unknown`, never replayed; still-live
workers remain visible as busy and keep their process-owned physical locks.

The former maintenance lease API, CLI commands and persisted lease table are
removed. All participating host tools must use this implementation; mixed
versions and resuming old lease workflows are not supported.

Application CDC0 carries framed ESP-Iris data, not a text console. Flash and
monitor through a separate UART/Serial-JTAG interface or manually enter the ROM
downloader when required.

### Select USB Serial/JTAG

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web \
  --usb-serial-jtag /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_...
```

Automatic USB Serial/JTAG probing is intentionally disabled because its fixed
descriptor does not identify the running firmware. Use the explicit option
above or opt in with `--discover-usb-serial-jtag`. Stop the Gateway before
flashing or monitoring through the same endpoint.

### Connect raw TCP explicitly

The device application must first create its network interface. Then select its
TCP endpoint in the Gateway/Workbench. Start with the
[`tcp_wifi`](../examples/tcp_wifi/README.md) or
[`tcp_pairing`](../examples/tcp_pairing/README.md) example.

## Access and authentication

Loopback clients are authentication-free by default. Use
`--require-local-auth` to exercise the login flow locally:

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web --require-local-auth
```

A new Gateway initializes the developer password to `espressif`. Override that
first-run value with `ESP_IRIS_DEVELOPER_PASSWORD` or `--password-file`, then
change it from System Settings. Do not expose the default password outside an
isolated development environment.

To serve a trusted development LAN explicitly:

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" web \
  --listen 0.0.0.0 --port 8443
```

Plain HTTP does not protect passwords, Agent Tokens, logs, media, or commands.
Enable `--tls` for a generated local certificate, or provide `--tls-cert` and
`--tls-key`. The Gateway prints the generated certificate SHA-256 fingerprint.

Do not rely on `X-Forwarded-For` to turn a remote client into a loopback client;
the Gateway authorizes the actual TCP peer.

## Workbench pages

| Page | Purpose |
| --- | --- |
| Overview | Device identity, connection, firmware, health, and current status |
| Logs | Live and retained device logs |
| Workspace | RPC, console, screen/input, media, firmware, OTA, and restart actions |
| Files | Product-registered logical volumes, paginated directories, streamed upload/download, rename, mkdir, and safe delete |
| Operations | Durable long-running operation state and progress |
| Records | Sessions, evidence, artifacts, and retained history |
| Settings | Access mode, credentials, tokens, TLS, and system configuration |

The interactive OpenAPI view is available at `/docs`; the machine-readable
contract is `/v1/openapi.json`; metrics are exposed at `/v1/metrics`.

### File API

The file endpoints address only product-registered logical volumes:

| Method and path | Behavior |
| --- | --- |
| `GET /v1/devices/{id}/files/volumes` | Volume capabilities and transfer limits |
| `GET /v1/devices/{id}/files` | Paginated directory list |
| `GET /v1/devices/{id}/files/stat` | File or directory metadata and opaque ETag |
| `GET /v1/devices/{id}/file` | Stream download, including HTTP Range |
| `PUT /v1/devices/{id}/file` | Stream create or atomic replacement |
| `POST /v1/devices/{id}/directories` | Create one directory |
| `POST /v1/devices/{id}/file-rename` | Same-volume rename without overwrite |
| `DELETE /v1/devices/{id}/file` | Delete a file or empty directory |

Upload requires `Content-Length` and is limited to 32 MiB by the Gateway. Use
`overwrite=true` with `If-Match` for replacement. The Gateway forwards request
chunks directly to the device, records only path/size/hash/result metadata,
and serializes every mutation with other writes to that device. Agent Tokens
need `files.read`, `files.write`, or `files.delete` as appropriate; new tokens
default to `files.read`.

## Command-line client

The `ctl` client talks to the same Gateway API as the Workbench. It can run at
the same time as one or more Workbench and agent clients:

```bash
IRIS="$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py"

python "$IRIS" ctl devices
python "$IRIS" ctl status DEVICE_ID
python "$IRIS" ctl logs --device DEVICE_ID --follow
python "$IRIS" ctl rpc DEVICE_ID system.info --params '{}'
python "$IRIS" ctl jobs DEVICE_ID JOB_ID
python "$IRIS" ctl cancel DEVICE_ID JOB_ID
python "$IRIS" ctl screenshot DEVICE_ID device.png
python "$IRIS" ctl mirror DEVICE_ID start --fps 5
python "$IRIS" ctl mirror DEVICE_ID stop
python "$IRIS" ctl restart DEVICE_ID
python "$IRIS" ctl firmware-add build/app.bin
python "$IRIS" ctl ota DEVICE_ID build/app.bin
python "$IRIS" ctl ota DEVICE_ID build/app.bin --validation-mode version
python "$IRIS" ctl system-update DEVICE_ID release.irisfw --wait
python "$IRIS" ctl operation-status OPERATION_ID
python "$IRIS" ctl operation-watch OPERATION_ID
python "$IRIS" ctl mode observe
```

OTA validates the firmware identity again after the device reconnects. The
default `elf_sha256` mode compares the ELF SHA-256 embedded in the uploaded BIN
with the running device's reported firmware SHA-256. Use
`--validation-mode version` only for compatibility with workflows that must
compare the project version string instead. The Workbench OTA dialog and the
REST request field `validation_mode` expose the same choice; supported values
are `elf_sha256` and `version`.

System Update supports signed and explicitly unsigned bundles. For an unsigned
development bundle, omit the manifest `signature` object and all key options:

```bash
python "$IRIS" bundle build manifest-template.json \
  --component-root build/system-update \
  --output release.irisfw
python "$IRIS" bundle inspect release.irisfw
python "$IRIS" web
```

For a signed deployment, start the Gateway with a pinned ECDSA P-256 trust key.
The private signing key is used only by the offline bundle command and is never
passed to the Gateway or device:

```bash
python "$IRIS" bundle build manifest-template.json \
  --component-root build/system-update \
  --signing-key /private/release-signing-key.pem \
  --signing-key-password-file /private/release-signing-key.password \
  --output release.irisfw
python "$IRIS" bundle inspect release.irisfw \
  --trust-key release-public-key.pem
python "$IRIS" web --system-update-trust-key release-public-key.pem
```

The Gateway archives the exact `.irisfw`, preserves a valid coredump before
recovery entry, verifies the source layout through device inventory, streams
the components in manifest order, and accepts success only after a new
healthy boot reports matching application, bootloader, partition-table and
operation identities. When a trust key is configured, unsigned bundles are
rejected. Without a trust key, signed bundles are rejected and only explicitly
unsigned bundles are accepted.

Encrypted signing keys are supported through the optional password-file flag;
the password is never accepted as a command-line value. Keep the private key
and its password outside the Gateway host in the release-signing environment.

The bundle builder pads the partition-table image to its full 4 KiB sector.
When a bootloader is present it must be accompanied by the target partition
table, and the builder pads it with `0xff` through the byte before that table.
The product inventory provider must hash those same logical protected ranges;
on products with Flash encryption it is responsible for choosing the matching
plaintext/readback semantics. Normal firmware exposes only read-only inventory,
while recovery additionally registers the product write backend.

Use `ctl --json` for stable machine-readable output. A remote Gateway or local
Gateway started with `--require-local-auth` requires a login or named Agent
Token. Pass Agent Tokens through a protected file rather than a command-line
argument:

```bash
export ESP_IRIS_AGENT_TOKEN_FILE=/private/path/agent.token
python "$IRIS" ctl --json devices
```

Profiles store the Gateway URL, browser-style session, optional CA path, and
optional certificate fingerprint. Configure a profile with:

```bash
python "$IRIS" ctl --profile lab-a \
  --url https://192.0.2.10:8443 --ca gateway.crt profile --make-default
```

## Runtime and storage model

- One Gateway supervises many USB/TCP endpoints concurrently and indexes their
  sessions and events by stable `device_id`.
- Each physical device has at most one active session, even if more than one
  configured endpoint resolves to that device.
- Each WebSocket client receives its own live event queue and can resume from a
  retained event cursor. Workbench, CLI, and agents therefore observe without
  consuming one another's events.
- A stable `device_id` joins normal and recovery firmware into one device
  history; `boot_id` identifies a boot and `session_id` identifies a link.
- Device writes are serialized per device. OTA/recovery holds that device's
  write lane without blocking observation of it or other devices.
- `operation_id` makes long-running Gateway operations queryable and
  idempotent; Gateway restart does not replay device writes.
- Observe mode blocks business device requests while protocol housekeeping and
  device-pushed events continue.
- SQLite stores devices, sessions, operations, audit, token metadata, and log
  indexes. Raw logs rotate; explicitly saved artifacts and structured evidence
  remain durable.
- A newly connected Workbench, CLI follower, or agent receives recent stored
  events before live events, preserving continuity across reconnects.

## Troubleshooting

### The Workbench says the frontend is not built

Run `npm ci && npm run build` in `tools/frontend`, then restart the Gateway.

### Opening USB resets or disconnects the board

Confirm that firmware and Gateway use the same transport. For USB Serial/JTAG,
disable the ESP-IDF USB Serial/JTAG console and do not let flashing/monitoring
tools own the endpoint concurrently. For application CDC0, use a separate
programming interface.

### TCP never becomes reachable

ESP-Iris does not provision Wi-Fi or create a product network interface. Verify
that the application connected, obtained an address, and permits inbound TCP
on the configured port. Use the TCP examples as known-good references.

### Pairing fails

Verify that the device and private Gateway token store contain the same
64-character lowercase hexadecimal token. Never print the token in logs. A
device with an existing NVS token ignores a newly supplied example default
until it is explicitly rotated or reprovisioned.

### Diagnose the local installation

```bash
python "$ESP_IRIS_COMPONENT_DIR/tools/esp_iris.py" doctor --json
```

## Development and tests

From the repository root:

```bash
python3 -m pip install -r components/esp_iris/tools/requirements-dev.txt
cd components/esp_iris/tools
python3 -m ruff check iris_gateway tests
python3 -m pytest

cd frontend
npm ci
npm run test:unit
npm run build
```

Firmware examples are under [`../examples`](../examples/README.md). Build them
from the repository root with paths such as:

```bash
idf.py -C components/esp_iris/examples/minimal -B build-ci-tcp build
idf.py -C components/esp_iris/examples/rpc_jobs -B build-ci build
```

See the [component README](../README.md), [Chinese README](../README_zh.md), and
[wire protocol](../protocol/spec.md) for the device-side integration contract.


## Public host integration

Use `iris_gateway.client` (API major 1) for local project coordination, passive
registry snapshots and terminal operation evidence. Its standard-library-only
API owns state layout and schema checks. Consumer products must not import
`link`, `ownership` or `store` internals. See
[component boundaries](../../../../docs/component-boundaries.md) for compatibility,
source fingerprints, shared lifecycle and product Recovery preconditions.

### Receiver-initiated device handoff

`POST /v1/project/takeovers` runs on the receiving project Gateway. Select exactly
one `device_id` or `endpoint`; optionally pass a UUID `takeover_id`, `force` and
`timeout` (default 120 seconds, maximum 3600). It resolves the current live local
owner and asks that owner to use the existing reserved transfer and validated
acceptance protocol. An unowned device uses `/v1/project/acquire` instead.

Without force, a busy device returns its operation, mirror and Job reasons.
With force, the owner closes admission only for this device, cancels queued work,
lets executing writes finish, stops all mirror channels and waits for cooperative
Job cancellation. Browser log streams and client keepalives do not block handoff.
A drain timeout retains ownership and reopens admission; already stopped work is
not restarted, and a writer is never killed. Transfers use per-device control
locks, so unrelated devices can continue and receivers can accept incoming work
while coordinating an outbound request. Peer failures preserve the takeover ID;
query its durable state before retrying with the same ID.

The public record routes are `GET /v1/project/takeovers/{takeover_id}` and
`POST /v1/project/takeovers/{takeover_id}/{resume|abort|reconcile}`. Responses
contain a `takeover` record with `takeover_id`. Resume runs in the original
receiver, abort in the original owner; completed takeovers cannot be rolled back.
Reconcile requires both original sessions to have exited and a participant
project to prove all physical locks are free.

The receiving request retains its Gateway until validation finishes. It calls
`/v1/project/handoff/prepare` on the owner, which validates the receiver and its
pairing setup before releasing any endpoint. The receiver then validates the
reserved identity locally. This peer endpoint is an implementation detail;
there is no owner-initiated public transfer API, callback accept API or legacy
route alias. The durable registry transaction still uses the internal transfer
schema and retains reservations across interrupted HTTP requests.
