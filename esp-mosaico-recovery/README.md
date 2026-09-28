# ESP-Mosaico Recovery

This component owns retained Recovery firmware, its reviewed bundle and the
public product ABI in `include/mosaico_recovery_contract.h`. The header is used
by independently built normal applications and Recovery. Its existing 64-byte
sysmeta record, NVS namespace, magic and version remain unchanged.

The product CLI and build runner now live in [mosaico-tools](../mosaico-tools/README.md).
The old `mosaico.py`, Python import path and build-runner entrypoint forward to
that implementation for compatibility. New consumers use `mosaico-tools`.
Integration and firmware tests remain under `tests/`.

Firmware and reviewed images stay under `firmware/recovery`; moving host tools
does not rebuild or replace the reviewed bundle. Perform device operations
through the consuming workspace's `mosaico.py` launcher.

## Recovery through an independent USB Serial/JTAG connection

When the same board has a separate USB Serial/JTAG cable connected, select its
port explicitly while the primary ESP-Iris connection is still live:

```sh
python mosaico.py recover --device-id DEVICE_ID --recovery-port COM14 --source current
```

Omit `--source current` to use the reviewed bundle. `--dry-run` resolves the live
identity and port without building, resetting or writing. This option
requires exactly one connected Espressif `303A:1001` interface, and the named
port must identify it. Selecting it asserts that this independently connected
interface belongs to the chosen board. For a device already in ROM download
mode, prefer `--hardware-mac`; its eFuse identity is read directly instead of
inferring an association from USB topology.

The command prepares the complete reviewed/current Recovery bundle before
submitting one local Gateway operation. The operation preserves crash evidence
and detaches both the managed interface and the explicit programming interface.
A separate foreground executor holds physical locks on both endpoints while
writing. The serial interface is enumerated again before the write; an identity
change, ambiguous endpoint or conflicting owner prevents flashing. Other
applications must release the serial port before this operation can use it.

Only the existing `mosaico-recover-flash` target writes firmware. Its complete
bundle writes bootloader, partition table, OTA selection data and factory
Recovery; this option does **not** introduce whole-flash erase or a
Recovery-partition-only mode. Preserve the normal bundle/layout contract.
Recovery acceptance uses the original managed connection: the same Device ID,
a new Boot ID, the prepared Recovery version and OTA capability must be verified.
The operation record contains before/after evidence and links to the raw writer
log. Its operation ID is included in the command result and `host-operation.json`.
There are no separate endpoint leases, tokens or manual abort/renewal steps.
Failure remains recorded after the temporary process-owned exclusion ends.

Recovery remote downloads now use [HTTPS Bridge](firmware/recovery/README.md).
Configure the build Origin and board ID, then use `mosaico.py iris test bridge-code` or
the device download page to pair once for a partitions, layout or factory update.
