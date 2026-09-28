# ESP-Mosaico Utils agent rules

## Repository boundaries

- `ESP-Iris/` owns the reusable ESP-IDF component, protocol, Developer
  Gateway, Workbench, examples, and component-level tests.
- `mosaico-tools/` owns the ESP-Mosaico host CLI and build runner. It consumes
  the public Iris host API and product Recovery contracts.
- `esp-mosaico-recovery/` owns retained Recovery firmware, its public product
  ABI header, reviewed bundle, and integration tests. Legacy tool paths only forward.
- Do not recreate a nested ESP-Iris submodule. Both products resolve from the
  same `esp-mosaico-utils` revision.
- Keep consuming applications and workspace-specific `.mosaico.json` files in
  their application repositories. Reusable templates live in mosaico-tools/templates;
  optional GSP application components in mosaico-tools/components; the normal
  Recovery adapter and its CMake configuration gate live in esp-mosaico-recovery.
- Run both mosaico-tools/tests and esp-mosaico-recovery/tests for public application
  integration changes. These tests must not read consuming workspace internals.

## Versions

- ESP-Iris and ESP-Mosaico Tools are pre-1.0 products on the `0.1.x` release
  line. Their release versions are independent even when released together.
- ESP-Iris has its version source in
  `ESP-Iris/components/esp_iris/idf_component.yml`.
- ESP-Mosaico Tools has its version source in
  `mosaico-tools/tools/mosaico_cli/__init__.py`.
- The retained Recovery firmware version is a separate on-device compatibility
  identifier. Do not reset or downgrade it merely to match the tools release.
  ESP-30 explicitly sets source builds and the prebuilt bundle to `0.1`.
  Do not accept update bundles or Recovery image rollback from the old 2.x line.
- Use product-specific tags: `esp-iris-vX.Y.Z` and
  `esp-mosaico-tools-vX.Y.Z`.

## Validation

- Run Recovery host tests from `esp-mosaico-recovery/tests`.
- Run ESP-Iris Python tests from `ESP-Iris/components/esp_iris/tools` and keep
  Python 3.8 compatibility.
- Run Workbench unit/build/E2E checks for frontend changes.
- Build affected ESP-IDF examples and fixtures with a compatible, verified
  ESP-IDF checkout. Recovery currently requires ESP-IDF 6.2 or newer and the
  ESP32-S31 target.
- Preserve reviewed Recovery binaries until their replacement passes manifest,
  layout, hash, and device validation.

## Device operations

Perform device operations only through the consuming workspace's `mosaico.py`
launcher. Do not directly invoke ESP-IDF or ESP-Iris device-write commands, and
do not erase credentials, identity, Recovery data, or partitions without
explicit authorization.
