from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any

_SERIAL_PATH_DIRECTORIES = (
    pathlib.Path("/dev/serial/by-path"),
    pathlib.Path("/dev/serial/by-id"),
)


@dataclass(frozen=True)
class IrisUsbDevice:
    path: str
    device: str
    vid: int
    pid: int
    serial_number: str
    product: str = ""
    transport: str = "usb"
    location: str = ""


def _stable_linux_path(device: str) -> str:
    if os.name != "posix":
        return device
    target = os.path.realpath(device)
    # Prefer the physical USB topology path. ESP-Iris recovery and normal
    # firmware intentionally expose different product strings, so Linux gives
    # them different by-id names. A supervisor opened through the old by-id
    # link cannot reconnect after the device re-enumerates. The by-path link
    # remains stable across that transition; by-id is only a fallback for
    # systems that do not expose by-path links.
    for directory in _SERIAL_PATH_DIRECTORIES:
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.iterdir()):
            if os.path.realpath(candidate) == target:
                return str(candidate)
    return device


def discover_iris_usb_devices(
    *, include_usb_serial_jtag: bool = False, include_rom: bool = False
) -> list[IrisUsbDevice]:
    from serial.tools import list_ports

    devices = []
    for port in list_ports.comports():
        is_iris_cdc = iris_usb_allowed(
            {"vid": port.vid, "pid": port.pid, "product": port.product or ""}
        )
        is_usb_serial_jtag = (
            include_usb_serial_jtag
            and port.vid == 0x303A
            and port.pid == 0x1001
            and "USB JTAG/serial debug unit" in (port.product or "")
        )
        is_rom = include_rom and port.vid == 0x303A and port.pid == 0x0020
        if is_iris_cdc or is_usb_serial_jtag or is_rom:
            devices.append(
                IrisUsbDevice(
                    path=_stable_linux_path(port.device),
                    device=port.device,
                    vid=port.vid,
                    pid=port.pid or 0,
                    serial_number=port.serial_number or "",
                    product=port.product or "",
                    transport=(
                        "usb_serial_jtag" if is_usb_serial_jtag else "usb"
                    ),
                    location=getattr(port, "location", None) or "",
                )
            )
    return sorted(devices, key=lambda item: item.path)


def discover_iris_usb_ports(
    *, include_usb_serial_jtag: bool = False
) -> list[str]:
    return sorted(
        {
            device.path
            for device in discover_iris_usb_devices(
                include_usb_serial_jtag=include_usb_serial_jtag
            )
        }
    )


def serial_port_key(path: str) -> str:
    # COM names are absolute device names, not paths relative to a workspace.
    com_name = path.upper()
    if com_name.startswith("\\\\.\\"):
        com_name = com_name[4:]
    if re.fullmatch(r"COM[0-9]+", com_name):
        return com_name
    return os.path.normcase(os.path.realpath(path))


def usb_endpoint(metadata: dict) -> str:
    """Use the same physical interface key for discovery and explicit paths."""
    if metadata.get("location"):
        return f"usb:location={metadata['location']}"
    # A serial number may be shared by multiple interfaces on a composite device.
    return "usb:" + serial_port_key(str(metadata["path"]))


def resolve_usb_port(identifier: str) -> dict[str, Any]:
    """Enumerate descriptors without opening a serial session."""
    from serial.tools import list_ports

    matches: list[dict[str, Any]] = []
    for port in list_ports.comports():
        metadata: dict[str, Any] = {
            "path": _stable_linux_path(port.device),
            "device_path": port.device,
            "vid": port.vid,
            "pid": port.pid,
            "serial_number": port.serial_number or "",
            "product": port.product or "",
            "location": getattr(port, "location", None) or "",
        }
        if identifier == usb_endpoint(metadata) or (
            serial_port_key(identifier) == serial_port_key(port.device)
        ) or (
            identifier.startswith("usb:serial=")
            and identifier == "usb:serial=" + metadata["serial_number"]
        ):
            matches.append(metadata)
    if not matches:
        raise OSError(f"USB endpoint is absent: {identifier}")
    if len(matches) > 1:
        raise OSError(f"USB endpoint is ambiguous: {identifier}")
    return matches[0]


def iris_usb_allowed(
    metadata: dict, *, allow_serial_jtag: bool = False, explicit: bool = False
) -> bool:
    if metadata.get("vid") == 0x303A:
        if metadata.get("pid") == 0x1001:
            return allow_serial_jtag
        if metadata.get("pid") == 0x0020:
            # ESP32-S31 ROM download CDC belongs to the ROM operation executor.
            return False
    if explicit:
        return metadata.get("vid") is not None and metadata.get("pid") is not None
    if metadata.get("vid") != 0x303A:
        return False
    return metadata.get("pid") == 0x4002 or "ESP-Iris" in metadata.get("product", "")
