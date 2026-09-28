"""Explicit independent USB Serial/JTAG routing, without inferred board identity."""

from __future__ import annotations

import os
from typing import Any

from .errors import DeviceError, SelectionError


def same_port(left: str, right: str) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def serial_jtag_candidate(requested: str) -> dict[str, Any]:
    from serial.tools import list_ports

    available = [port for port in list_ports.comports()
                 if port.vid == 0x303A and port.pid == 0x1001]
    candidates = [port for port in available if same_port(requested, str(port.device))]
    if available and not candidates:
        raise DeviceError("--recovery-port does not identify a connected USB Serial/JTAG interface.")
    if len(candidates) != 1:
        raise SelectionError("--recovery-port must select exactly one connected USB Serial/JTAG 303A:1001 interface.")
    port = candidates[0]
    if not same_port(requested, str(port.device)):
        raise DeviceError("--recovery-port does not identify the unique USB Serial/JTAG 303A:1001 interface.")
    return {"path": str(port.device), "vid": port.vid, "pid": port.pid,
            "serial_number": str(getattr(port, "serial_number", None) or ""),
            "location": str(getattr(port, "location", None) or "")}
