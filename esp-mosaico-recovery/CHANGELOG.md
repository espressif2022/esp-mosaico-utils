# Changelog

## Unreleased

- Raise Recovery to `0.1.2` and regenerate the complete prebuilt bundle with
  omitted-data layout support. Keep the Recovery ABI, partition table and initial
  OTA data unchanged. Build and host validation pass; device acceptance was
  confirmed by the operator on 2026-09-22.
- Allow remote layout updates to omit mutable data images, including filesystem
  partitions. Only explicitly supplied components are erased/written; omitted
  data is not initialized and old bytes may be invalid under the new layout.
  Mutable application images remain mandatory. Requires Recovery `0.1.2` and
  the matching Bridge change; devices on older Recovery must be updated.
- Separate Bridge protocol-2 PRECHECK/COMMITTING authorization from coalesced,
  retryable progress telemetry, with an execution lease and legacy-server
  compatibility. Reuse private HTTPS connections for control and component
  downloads; retain explicit cancellation and the critical commit fence.
- Persist the local update result before a final HTTPS report with a two-second
  reboot wait budget. Keep the result available through later inventory even
  when the cloud acknowledgement is lost. Erase only image-sized, sector-aligned
  application ranges; retain full data-partition erasure.

- Refresh the complete reviewed Recovery bundle from `151a631`, including
  ESP-61 QR/OTA status UI, PSRAM and HTTPS throughput improvements, and the
  device-accepted INFO splash bootloader. Keep the base partition table and
  initial OTA data byte-identical; record the intermittent cloud progress-gate
  failure and remaining hardware-validation limits in the historical bundle
  README retained in Git history.
- Raise the retained Recovery compatibility version to `0.1.1`; keep the
  Recovery ABI and partition layout unchanged.
- Replace local HTTP triggers and URL downloads with the Recovery-owned HTTPS
  Bridge client, supporting partitions, layout and factory updates.
- Add USB `bridge-code` with bounded asynchronous pairing; remove
  `http-update-code` and `system-update --manifest-url` without aliases.
- Preserve complete RFC 3339 pairing expiry timestamps and report the overall
  pairing deadline when the final USB poll times out.
- Configure source-built ESP-Mosaico Recovery images to use the production
  `https://iris-bridge.esp-claw.com` Origin and `esp-mosaico` board ID.
- Refresh the reviewed `0.1` Recovery bootloader and application after complete
  ROM provisioning and v1.2 display acceptance; retain the byte-identical
  partition table and initial OTA data.

- Set newly built Recovery firmware to version `0.1` for ESP-30, independently
  of the `0.1.0` host CLI version. Keep the Recovery ABI and partition layout.
- Accept two-part Recovery versions in System Update checks, and reject old
  2.x update bundles and Recovery image rollback across release lines.
- Regenerate the prebuilt manifest from the validated Bridge-enabled build;
  keep the Recovery partition layout and ABI unchanged.

## 0.1.0 - 2026-09-10

- Establish ESP-Mosaico Tools as a `0.1.x` product in the
  `esp-mosaico-utils` monorepo.
- Move the host CLI and retained Recovery firmware into
  `esp-mosaico-recovery` without importing the former repository history.
- Consume the sibling ESP-Iris checkout from the same monorepo revision.
- Retain the reviewed device image until its replacement completes hardware
  acceptance.
