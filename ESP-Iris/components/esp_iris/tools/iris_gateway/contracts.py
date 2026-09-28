"""Typed boundaries between the Gateway application and device adapters.

The real :class:`IrisHub` and the deterministic :class:`DemoHub` both satisfy
this protocol.  Keeping the application layer dependent on this interface
prevents HTTP handlers from reaching into transport/session implementation
details.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Dict, Protocol, runtime_checkable

from .system_update import SystemUpdateBundle

ProgressCallback = Callable[[Dict[str, Any]], Awaitable[None]]


@runtime_checkable
class GatewayHub(Protocol):
    def list_devices(self) -> list[dict[str, Any]]: ...

    def list_endpoints(self) -> list[dict[str, Any]]: ...

    async def status(self, device_id: str) -> dict[str, Any]: ...

    async def task_memory(self, device_id: str) -> dict[str, Any]: ...

    async def file_volumes(self, device_id: str) -> dict[str, Any]: ...

    async def file_stat(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]: ...

    async def file_list(
        self,
        device_id: str,
        volume: str,
        path: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]: ...

    def file_download(
        self,
        device_id: str,
        volume: str,
        path: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> AsyncIterator[bytes]: ...

    async def file_upload(
        self,
        device_id: str,
        volume: str,
        path: str,
        chunks: AsyncIterator[bytes],
        *,
        total_size: int,
        overwrite: bool = False,
        if_match: str | None = None,
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]: ...

    async def file_mkdir(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]: ...

    async def file_delete(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]: ...

    async def file_rename(
        self,
        device_id: str,
        volume: str,
        source: str,
        destination: str,
    ) -> dict[str, Any]: ...

    async def crash_report(self, device_id: str) -> dict[str, Any]: ...

    async def read_core_dump_chunk(
        self, device_id: str, offset: int, maximum: int
    ) -> tuple[int, bytes]: ...

    async def rpc(
        self,
        device_id: str,
        service_id: int,
        method_id: int,
        payload: bytes,
        *,
        deadline_ms: int = 1000,
    ) -> bytes: ...

    async def job(
        self, device_id: str, job_id: int, *, cancel: bool = False
    ) -> dict[str, Any]: ...

    async def screenshot(
        self, device_id: str, description: dict[str, int] | None = None
    ) -> tuple[dict[str, int], bytes]: ...

    def active_mirrors(self, device_id: str) -> list[int]: ...

    async def mirror_start(
        self,
        device_id: str,
        channel: int,
        description: dict[str, int] | None = None,
        *,
        fps: int = 5,
    ) -> dict[str, Any]: ...

    async def mirror_stop(self, device_id: str, channel: int) -> None: ...

    async def ota_update(
        self,
        device_id: str,
        image: bytes,
        *,
        expected_sha256: bytes | None,
        project_name: str,
        version: str,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]: ...

    async def ota_status(self, device_id: str) -> dict[str, Any]: ...

    async def system_update(
        self,
        device_id: str,
        bundle: SystemUpdateBundle,
        *,
        operation_id: bytes,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]: ...

    async def system_update_inventory(self, device_id: str) -> dict[str, Any]: ...

    async def restart(self, device_id: str, delay_ms: int = 250) -> int: ...

    async def input_event(
        self, device_id: str, gesture: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def enter_recovery(self, device_id: str) -> dict[str, Any]: ...

    def subscribe(self, device_id: str) -> asyncio.Queue[dict[str, Any]]: ...

    def unsubscribe(
        self, device_id: str, queue: asyncio.Queue[dict[str, Any]]
    ) -> None: ...

    def subscribe_media(
        self, device_id: str, channel: int
    ) -> asyncio.Queue[dict[str, Any]]: ...

    def unsubscribe_media(
        self,
        device_id: str,
        channel: int,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None: ...

    async def close(self) -> None: ...


class LocalUsbHub(GatewayHub, Protocol):
    def host_endpoint(self, identifier: str) -> dict[str, Any]: ...

    async def detach_for_host(self, identifier: str) -> dict[str, Any]: ...

    def yield_host_lock(self, endpoint: str) -> None: ...

    async def resume_after_host(self, endpoint: str, *, connect: bool = False) -> None: ...



__all__ = ["GatewayHub", "ProgressCallback"]
