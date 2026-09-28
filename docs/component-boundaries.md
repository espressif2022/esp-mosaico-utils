# Component boundaries

| Owner | Responsibilities | Public contracts |
| --- | --- | --- |
| ESP-Iris | Transport, device sessions, Gateway lifetime and ownership, operations, reconnect, Workbench | Wire protocol; HTTP API/capabilities; `iris_gateway.client` host API v1 |
| mosaico-tools | Workspace selection, templates, GSP integration, build/artifact orchestration, product preconditions, evidence and user CLI | `mosaico.py`; `.mosaico.json` |
| esp-mosaico-recovery | Retained Recovery firmware, normal-application Recovery adapter, reviewed images and persistent product format | `include/mosaico_recovery_contract.h`; reviewed manifest |
| Application workspace | User-created application, dependency selection and UI policy | Product configuration and normal firmware |

Dependencies point from product tools and firmware adapters toward these public
contracts. Iris does not import Mosaico modules or encode Mosaico versions,
partition layouts or a fixed screen size.

## Host integration

`iris_gateway.client` is the only Python import surface consumed by Mosaico.
It requires only Python's standard library, so passive status works before host
dependencies are installed. `LocalProject` owns canonical project keys, startup
locks, connection records, launch arguments and terminal evidence recovery.
`registry_snapshot` returns same-user sessions and claims with OS-lock liveness.
Clients must not read Iris SQLite tables, lock names or private store converters.
Registry reads accept ownership schema 1 with or without the additive metadata
table, reject unknown versions, and never migrate or recreate existing state.

Live operations use Gateway HTTP/CLI interfaces and their declared capabilities.
The `recovery-transition/v1` capability supports a bounded same-device/new-boot
transition. `recovery-preconditions/v1` accepts reviewed Recovery version and
partition-table SHA-256 as operation data. The Gateway checks these after
reconnect and before OTA BEGIN. Transition, checks, write and healthy boot are
one operation; failures are not replayed automatically. The product CLI consumes
the completed operation's Recovery evidence instead of maintaining a second
transition state machine. Standalone transition and system update use the same
Iris transition helper.

## Compatibility and source provenance

The default `gateway.source_policy` is `compatible`: reuse requires a supported
Gateway API major and the capabilities needed by the command. Another monorepo
commit alone does not make a running Gateway incompatible.

Set this optional workspace field to require identical local runtime sources:

```json
{"gateway": {"source_policy": "exact"}}
```

`/v1/health` exposes `source.algorithm`, `source.fingerprint` and optional
`source.git_revision`. The `sha256-runtime-v1` fingerprint includes host Python,
RPC catalog, host dependency lock, component manifest and served Workbench
distribution. It captures uncommitted changes; unrelated Recovery changes do
not affect it. The running process captures this identity at startup. Git is
provenance only and is optional for source archives. Exact-policy mismatch
reports an error and leaves the current users' Gateway running. Remote profiles
retain their external lifecycle and use API compatibility.

## Firmware contracts

`mosaico_recovery_contract.h` owns the existing sysmeta ABI, including 64-byte
record size, result offset, namespace, magic and format version. It has no Iris
or ESP-IDF dependency. Both the normal-app adapter and Recovery use it; changing
the header does not replace reviewed firmware images.

Iris reserves optional `pointer/v1` and `enter-recovery/v1` RPC profiles in
`protocol/spec.md` and `include/esp_iris_service_profiles.h`. Generic RPC support
alone does not guarantee either profile. Pointer coordinates use the device's
negotiated full-screen geometry. The profiles preserve existing wire IDs and
payloads, so the refactor does not require a firmware update.

## Directory migration

New launchers point to `mosaico-tools/mosaico.py`; its implementation and build
runner live under `mosaico-tools/tools` and `mosaico-tools/skills`. Recovery source,
reviewed bundles, contracts and integration tests retain their paths.
The old Recovery launcher, Python package path and build-runner script are
forwarding shims with no duplicate implementation or independent version.

BSP examples own complete Mosaico games and their behavior tests; the engine owns
rendering/runtime and Host implementation tests. Shared application integration
is documented in [mosaico-tools](../mosaico-tools/docs/application-integration.md).
