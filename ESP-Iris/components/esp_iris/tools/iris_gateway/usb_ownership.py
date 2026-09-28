"""Serialize physical USB lock acquisition across Gateway processes."""
from __future__ import annotations

import contextlib

from .link import EndpointLock


class UsbEndpointBusy(RuntimeError):
    """Another process owns the physical USB interface."""


@contextlib.contextmanager
def usb_admission():
    lock = EndpointLock("usb-admission")
    try:
        lock.acquire(blocking=True)
        yield lock.path.parent
    finally:
        lock.close()
