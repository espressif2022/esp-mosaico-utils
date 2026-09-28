from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import math
import random
import struct
import time
import zlib
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Dict

from .boot_identity import boot_id_text
from .compat import remove_prefix
from .firmware import inspect_firmware_image
from .system_update import SystemUpdateBundle, SystemUpdateComponentKind

EventSink = Callable[[Dict[str, Any]], Awaitable[None]]


def _png(width: int = 640, height: int = 360, phase: int = 0) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + name
            + data
            + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            grid = x % 80 < 2 or y % 60 < 2
            pulse = abs(x - ((phase * 13) % width)) < 3
            if pulse:
                color = (39, 197, 209)
            elif grid:
                color = (41, 54, 62)
            else:
                noise = (x * 7 + y * 3 + phase) % 11
                color = (16 + noise, 22 + noise, 26 + noise)
            rows.extend(color)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
        b"IDAT", zlib.compress(bytes(rows), 6)
    ) + chunk(b"IEND", b"")


class DemoHub:
    """In-process deterministic device fleet used for offline UI/API tests."""

    instance_id = "demo"

    def __init__(self, event_sink: EventSink | None = None) -> None:
        self._event_sink = event_sink
        self._started_ns = time.monotonic_ns()
        self._phase = 0
        self._task: asyncio.Task[None] | None = None
        self._media_tasks: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._media_subscribers: dict[
            tuple[str, int], set[asyncio.Queue[dict[str, Any]]]
        ] = collections.defaultdict(set)
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = (
            collections.defaultdict(set)
        )
        self._jobs: dict[str, dict[int, dict[str, Any]]] = collections.defaultdict(dict)
        self._devices: dict[str, dict[str, Any]] = {
            "demo-a1b2c3d4": self._device(
                "demo-a1b2c3d4", "Mosaico Alpha", "normal", "3.5.0", 0xA10A
            ),
            "demo-e5f6a7b8": self._device(
                "demo-e5f6a7b8", "Camera Bench", "normal", "3.5.0", 0xB20B
            ),
            "demo-c9d0e1f2": self._device(
                "demo-c9d0e1f2", "Recovery Fixture", "recovery", "3.5.0", 0xC30C
            ),
        }
        self._files = {
            "app.json": b'{"device":"esp-iris","mode":"demo"}\n',
            "README.txt": b"ESP-Iris streamed file service demo.\n",
            "certs/device.pem": b"-----BEGIN CERTIFICATE-----\nDEMO\n-----END CERTIFICATE-----\n",
        }
        self._directories = {"", "certs"}

    @staticmethod
    def _device(
        device_id: str, alias: str, firmware_mode: str, version: str, boot_id: int
    ) -> dict[str, Any]:
        return {
            "device_id": device_id,
            "suggested_alias": alias,
            "boot_id": boot_id,
            "session_id": boot_id * 3,
            "endpoint": f"demo:{device_id}",
            "transport": 1,
            "transport_name": "USB Highspeed",
            "firmware_mode": firmware_mode,
            "chip_target": "esp32s31",
            "project_name": "esp-iris-template",
            "app_version": version,
            "idf_version": "v6.0-demo",
            "firmware_sha256": hashlib.sha256(
                f"{device_id}-{version}".encode()
            ).hexdigest(),
            "reset_reason": 1,
            "capabilities": 0x1FFFF,
            "capability_names": [
                "rpc",
                "jobs",
                "ota",
                "restart",
                "screen",
                "image",
                "audio",
                "input",
                "crash",
                "files",
                "system_update",
                "system_inventory",
            ],
            "auth_mode": 0,
            "max_payload": 262144,
            "connected": True,
            "demo": True,
        }

    async def start(self) -> None:
        if self._task is not None:
            return
        for device in self._devices.values():
            await self._emit(
                {
                    "kind": "connection",
                    "connection_state": "connected",
                    "device_id": device["device_id"],
                    "boot_id": device["boot_id"],
                    "session_id": device["session_id"],
                    "endpoint": device["endpoint"],
                    "host_receive_monotonic_ns": time.monotonic_ns(),
                    "host_receive_wall_ns": time.time_ns(),
                }
            )
        self._task = asyncio.create_task(self._run(), name="esp-iris-demo")

    async def start_usb_discovery(self, interval_seconds: float = 1.0) -> None:
        del interval_seconds
        await self.start()

    async def add_usb(self, port: str) -> None:
        del port
        raise RuntimeError("USB is disabled in demo mode")

    async def add_tcp(self, host: str, port: int = 19772, pairing_token: str | None = None) -> None:
        del host, port, pairing_token
        raise RuntimeError("TCP device links are disabled in demo mode")

    def list_devices(self) -> list[dict[str, Any]]:
        return [boot_id_text(device) for device in self._devices.values() if device["connected"]]

    def list_endpoints(self) -> list[dict[str, Any]]:
        return [
            {
                "endpoint": device["endpoint"],
                "state": "ready" if device["connected"] else "retrying",
                "attempt": 1,
                "error": None,
                "device_id": device["device_id"],
                "updated_monotonic_ns": time.monotonic_ns(),
                "demo": True,
            }
            for device in self._devices.values()
        ]

    def get(self, device_id: str) -> dict[str, Any]:
        try:
            device = self._devices[device_id]
        except KeyError as exc:
            raise KeyError(f"unknown ESP-Iris demo device: {device_id}") from exc
        if not device["connected"]:
            raise KeyError(f"ESP-Iris demo device is disconnected: {device_id}")
        return device

    async def status(self, device_id: str) -> dict[str, Any]:
        device = self.get(device_id)
        uptime_us = (time.monotonic_ns() - self._started_ns) // 1000
        wobble = int(1400 * math.sin(self._phase / 8))
        return {
            **boot_id_text(device),
            "uptime_us": uptime_us,
            "free_internal": 186_240 + wobble,
            "min_free_internal": 174_112,
            "total_internal": 512_000,
            "total_spiram": 8_388_608,
            "free_spiram": 6_291_456 + wobble,
            "min_free_spiram": 6_160_384,
            "log_dropped_bytes": 0,
            "rx_frames": 19_400 + self._phase,
            "tx_frames": 8_300 + self._phase,
            "invalid_frames": 0,
            "link_count": 1,
            "stack_min_free": 5024,
            "worker_max_used": 4096,
            "lifecycle_state": "running" if device["firmware_mode"] == "normal" else "recovery",
            "heap_used": 74_752 - wobble,
            "heap_static": 64_000,
            "heap_total": 260_992,
            "clock_offset_us": 818.0,
            "clock_uncertainty_us": 410.0,
            "demo": True,
        }

    async def task_memory(self, device_id: str) -> dict[str, Any]:
        device = self.get(device_id)
        return {
            **boot_id_text(device),
            "uptime_us": (time.monotonic_ns() - self._started_ns) // 1000,
            "tasks": [
                {"task_number": 1, "stack_free_min_bytes": 2416},
                {"task_number": 2, "stack_free_min_bytes": 4096},
            ],
        }

    async def file_volumes(self, device_id: str) -> dict[str, Any]:
        self.get(device_id)
        return {
            "volumes": [
                {
                    "id": "cfg",
                    "capabilities": 511,
                    "capability_names": [
                        "read",
                        "list",
                        "mtime",
                        "write",
                        "delete",
                        "mkdir",
                        "rename",
                        "atomic_replace",
                        "hash",
                    ],
                }
            ],
            "chunk_max": 1024,
            "path_max": 255,
        }

    def _file_metadata(self, path: str) -> dict[str, Any]:
        if path in self._directories:
            digest = hashlib.sha256(f"directory:{path}".encode()).hexdigest()[:16]
            return {"kind": "directory", "size": 0, "mtime_s": 1_777_000_000, "etag": digest}
        try:
            data = self._files[path]
        except KeyError as exc:
            raise KeyError(f"unknown demo file: {path}") from exc
        return {
            "kind": "file",
            "size": len(data),
            "mtime_s": 1_777_000_000,
            "etag": hashlib.sha256(data).hexdigest()[:16],
        }

    async def file_stat(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]:
        self.get(device_id)
        if volume != "cfg":
            raise KeyError(volume)
        return {"volume": volume, "path": path, **self._file_metadata(path)}

    async def file_list(
        self,
        device_id: str,
        volume: str,
        path: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        metadata = await self.file_stat(device_id, volume, path)
        if metadata["kind"] != "directory":
            raise ValueError("file list target is not a directory")
        prefix = f"{path}/" if path else ""
        names: set[str] = set()
        for candidate in {*self._files, *self._directories}:
            if not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            if not remainder:
                continue
            names.add(remainder.split("/", 1)[0])
        ordered = sorted(names)
        selected = ordered[cursor : cursor + limit]
        entries = []
        for name in selected:
            child = f"{prefix}{name}"
            entries.append({"name": name, **self._file_metadata(child)})
        next_cursor = cursor + len(selected) if cursor + len(selected) < len(ordered) else None
        return {
            "volume": volume,
            "path": path,
            "entries": entries,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "snapshot": False,
        }

    async def file_download(
        self,
        device_id: str,
        volume: str,
        path: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> AsyncIterator[bytes]:
        metadata = await self.file_stat(device_id, volume, path)
        if metadata["kind"] != "file":
            raise ValueError("file download target is not a regular file")
        data = self._files[path]
        end = len(data) if length is None else min(len(data), offset + length)
        for position in range(offset, end, 1024):
            await asyncio.sleep(0)
            yield data[position : min(position + 1024, end)]

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
    ) -> dict[str, Any]:
        self.get(device_id)
        if volume != "cfg" or not path or path in self._directories:
            raise ValueError("invalid demo upload target")
        parent = path.rpartition("/")[0]
        if parent not in self._directories:
            raise KeyError(parent)
        existing = self._files.get(path)
        if existing is not None and not overwrite:
            raise ValueError("demo file already exists")
        if if_match is not None and (
            existing is None
            or hashlib.sha256(existing).hexdigest()[:16]
            != remove_prefix(if_match, "W/").strip('"')
        ):
            raise ValueError("demo file etag conflict")
        data = bytearray()
        async for chunk in chunks:
            data.extend(chunk)
            if len(data) > total_size:
                raise ValueError("upload exceeds declared size")
            if progress is not None:
                await progress(len(data), total_size)
        if len(data) != total_size:
            raise ValueError("upload ended before declared size")
        self._files[path] = bytes(data)
        return {
            "volume": volume,
            "path": path,
            **self._file_metadata(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "replaced": existing is not None,
        }

    async def file_mkdir(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]:
        self.get(device_id)
        if volume != "cfg" or not path or path in self._directories or path in self._files:
            raise ValueError("invalid or existing demo directory")
        parent = path.rpartition("/")[0]
        if parent not in self._directories:
            raise KeyError(parent)
        self._directories.add(path)
        return {"volume": volume, "path": path, **self._file_metadata(path)}

    async def file_delete(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]:
        self.get(device_id)
        if volume != "cfg" or not path:
            raise ValueError("invalid demo delete target")
        if path in self._files:
            del self._files[path]
        elif path in self._directories:
            prefix = f"{path}/"
            if any(item.startswith(prefix) for item in {*self._files, *self._directories}):
                raise ValueError("demo directory is not empty")
            self._directories.remove(path)
        else:
            raise KeyError(path)
        return {"volume": volume, "path": path, "deleted": True}

    async def file_rename(
        self,
        device_id: str,
        volume: str,
        source: str,
        destination: str,
    ) -> dict[str, Any]:
        self.get(device_id)
        if volume != "cfg" or not source or not destination:
            raise ValueError("invalid demo rename target")
        if destination in self._files or destination in self._directories:
            raise ValueError("demo rename destination exists")
        if source in self._directories and destination.startswith(f"{source}/"):
            raise ValueError("a demo directory cannot be moved below itself")
        parent = destination.rpartition("/")[0]
        if parent not in self._directories:
            raise KeyError(parent)
        if source in self._files:
            self._files[destination] = self._files.pop(source)
        elif source in self._directories:
            prefix = f"{source}/"
            moved_files = {
                destination + item[len(source) :]: data
                for item, data in self._files.items()
                if item.startswith(prefix)
            }
            moved_directories = {
                destination + item[len(source) :]
                for item in self._directories
                if item.startswith(prefix)
            }
            self._files = {
                item: data
                for item, data in self._files.items()
                if not item.startswith(prefix)
            }
            self._files.update(moved_files)
            self._directories = {
                item
                for item in self._directories
                if item != source and not item.startswith(prefix)
            }
            self._directories.add(destination)
            self._directories.update(moved_directories)
        else:
            raise KeyError(source)
        return {
            "volume": volume,
            "source": source,
            "path": destination,
            **self._file_metadata(destination),
        }

    async def crash_report(self, device_id: str) -> dict[str, Any]:
        self.get(device_id)
        present = device_id.endswith("a7b8")
        return {
            "device_id": device_id,
            "core_dump_present": present,
            "core_dump_valid": present,
            "core_dump_size": 2048 if present else 0,
            "core_dump_chunk_max": 1024,
            "core_dump_elf_sha256": "ab" * 32 if present else "",
            "decode_eligible": present,
            "previous_boot_crash": present,
            "crash_count": 1 if present else 0,
            "crash_limit": 3,
            "crash_loop_triggered": False,
            "crash_recovery_pending": False,
            "crash_origin_reset_reason": 9 if present else 0,
            "crash_failed_app_address": 0x120000 if present else 0,
            "crash_failed_firmware_sha256": "ab" * 32 if present else "",
            "crash_state_error": 0,
            "panic_count": 1 if present else 0,
            "demo": True,
        }

    async def read_core_dump_chunk(
        self, device_id: str, offset: int, maximum: int = 1024
    ) -> tuple[int, bytes]:
        report = await self.crash_report(device_id)
        if not report["core_dump_present"]:
            raise RuntimeError("no retained coredump")
        data = (b"ESP-IRIS-DEMO-COREDUMP\0" * 100)[:2048]
        return len(data), data[offset : offset + maximum]

    async def rpc(
        self,
        device_id: str,
        service_id: int,
        method_id: int,
        payload: bytes = b"",
        *,
        deadline_ms: int = 1000,
    ) -> bytes:
        self.get(device_id)
        await asyncio.sleep(min(deadline_ms / 1000, 0.08))
        if service_id == 0x1002 and method_id == 1:
            job_id = max(self._jobs[device_id], default=0) + 1
            self._jobs[device_id][job_id] = {
                "job_id": job_id,
                "kind": 0x102,
                "state": "succeeded",
                "progress": 1.0,
            }
            line = payload.decode("utf-8", errors="replace")
            await self._emit(
                {
                    "kind": "log",
                    "device_id": device_id,
                    "text": f"[console:{job_id}]$ {line}\n",
                    "host_receive_wall_ns": time.time_ns(),
                    "demo": True,
                }
            )
            await self._emit(
                {
                    "kind": "log",
                    "device_id": device_id,
                    "text": f"[console:{job_id}] result=0\n",
                    "host_receive_wall_ns": time.time_ns(),
                    "demo": True,
                }
            )
            return struct.pack("<I", job_id)
        return (
            f'{{"ok":true,"service_id":{service_id},"method_id":{method_id},'
            f'"request_bytes":{len(payload)},"demo":true}}'
        ).encode()

    async def job(self, device_id: str, job_id: int, *, cancel: bool = False) -> dict[str, Any]:
        self.get(device_id)
        job = self._jobs[device_id].setdefault(
            job_id, {"job_id": job_id, "state": "running", "progress": 0.58}
        )
        if cancel:
            job.update(state="cancelled", cancel_requested=True)
        return {**job, "device_id": device_id, "demo": True}

    async def screenshot(
        self, device_id: str, description: dict[str, int] | None = None
    ) -> tuple[dict[str, int], bytes]:
        self.get(device_id)
        description = description or {}
        width = min(max(int(description.get("width", 640)), 64), 1280)
        height = min(max(int(description.get("height", 360)), 64), 720)
        return {
            "width": width,
            "height": height,
            "format": 4,
            "mirror_reused": int((device_id, 3) in self._media_tasks),
        }, _png(width, height, self._phase)

    def active_mirrors(self, device_id: str) -> list[int]:
        return sorted(key[1] for key in self._media_tasks if key[0] == device_id)

    async def mirror_start(
        self,
        device_id: str,
        channel: int,
        description: dict[str, int] | None = None,
        *,
        fps: int = 5,
    ) -> dict[str, Any]:
        self.get(device_id)
        if not 1 <= fps <= 30:
            raise ValueError("mirror fps must be between 1 and 30")
        key = (device_id, channel)
        reused = key in self._media_tasks
        if not reused:
            self._media_tasks[key] = asyncio.create_task(
                self._run_media(device_id, channel, fps),
                name=f"demo-media-{device_id}-{channel}",
            )
        return {
            "device_id": device_id,
            "channel": channel,
            "description": description or {},
            "fps": fps,
            "state": "running",
            "reused": reused,
            "demo": True,
        }

    async def mirror_stop(self, device_id: str, channel: int) -> None:
        self.get(device_id)
        task = self._media_tasks.pop((device_id, channel), None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def subscribe_media(self, device_id: str, channel: int) -> asyncio.Queue[dict[str, Any]]:
        self.get(device_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)
        self._media_subscribers[(device_id, channel)].add(queue)
        return queue

    def unsubscribe_media(
        self, device_id: str, channel: int, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        self._media_subscribers[(device_id, channel)].discard(queue)

    def subscribe(self, device_id: str) -> asyncio.Queue[dict[str, Any]]:
        self.get(device_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subscribers[device_id].add(queue)
        return queue

    def unsubscribe(
        self, device_id: str, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        self._subscribers[device_id].discard(queue)

    async def ota_update(
        self,
        device_id: str,
        image: bytes,
        *,
        expected_sha256: bytes | None,
        project_name: str,
        version: str,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        device = self.get(device_id)
        binary_sha256 = hashlib.sha256(image)
        if expected_sha256 and binary_sha256.digest() != expected_sha256:
            raise ValueError("OTA SHA-256 mismatch")
        firmware = inspect_firmware_image(image)
        if progress_callback is not None:
            await progress_callback(
                {
                    "stage": "transferring",
                    "job_id": 1,
                    "bytes_received": len(image) // 2,
                    "bytes_total": len(image),
                    "progress_permille": 450,
                    "partition": "ota_0",
                }
            )
        await asyncio.sleep(0.2)
        old_boot = device["boot_id"]
        device.update(
            app_version=version or device["app_version"],
            project_name=project_name or device["project_name"],
            firmware_sha256=firmware.elf_sha256,
            boot_id=old_boot + 1,
            firmware_mode="normal",
        )
        await self._emit(
            {
                "kind": "device_event",
                "event_name": "healthy",
                "device_id": device_id,
                "boot_id": device["boot_id"],
                "host_receive_wall_ns": time.time_ns(),
                "demo": True,
            }
        )
        return {
            "accepted": True,
            "bytes": len(image),
            "sha256": binary_sha256.hexdigest(),
            "expected_version": device["app_version"],
            "previous_boot_id": old_boot,
            "boot_id": device["boot_id"],
            "healthy": True,
            "demo": True,
        }

    async def ota_status(self, device_id: str) -> dict[str, Any]:
        self.get(device_id)
        return {
            "stage": "idle",
            "job_id": 1,
            "bytes_total": 0,
            "bytes_received": 0,
            "progress_permille": 1000,
            "active": False,
            "result": 0,
            "partition": "ota_0",
            "demo": True,
        }

    async def system_update(
        self,
        device_id: str,
        bundle: SystemUpdateBundle,
        *,
        operation_id: bytes,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        device = self.get(device_id)
        total = sum(component.size for component in bundle.components)
        received = 0
        for index, component in enumerate(bundle.components):
            received += component.size
            if progress_callback is not None:
                await progress_callback(
                    {
                        "stage": "transferring",
                        "job_id": 2,
                        "operation_id": operation_id.hex(),
                        "component_id": component.id,
                        "component_index": index,
                        "component_count": len(bundle.components),
                        "bytes_received": received,
                        "bytes_total": total,
                        "progress_permille": 50 + 850 * received // total,
                    }
                )
            if component.kind is SystemUpdateComponentKind.APPLICATION:
                firmware = inspect_firmware_image(component.data)
                device["project_name"] = firmware.project_name
                device["app_version"] = firmware.version
                device["firmware_sha256"] = firmware.elf_sha256
            if component.kind is SystemUpdateComponentKind.RECOVERY:
                firmware = inspect_firmware_image(component.data)
                device["project_name"] = firmware.project_name
                device["app_version"] = firmware.version
                device["firmware_sha256"] = firmware.elf_sha256
            if component.kind is SystemUpdateComponentKind.BOOTLOADER:
                device["bootloader_sha256"] = component.sha256.hex()
        if any(
            component.kind is SystemUpdateComponentKind.PARTITION_TABLE
            for component in bundle.components
        ):
            device["partition_table_sha256"] = bundle.target_layout_sha256
        device["last_system_update_operation_id"] = operation_id.hex()
        device["boot_id"] += 1
        device["session_id"] += 1
        device["firmware_mode"] = (
            "recovery"
            if any(
                component.kind is SystemUpdateComponentKind.RECOVERY
                for component in bundle.components
            )
            else "normal"
        )
        await self._emit(
            {
                "kind": "connection",
                "connection_state": "rebooted",
                "device_id": device_id,
                "boot_id": device["boot_id"],
                "session_id": device["session_id"],
                "endpoint": device["endpoint"],
                "host_receive_wall_ns": time.time_ns(),
                "demo": True,
            }
        )
        await self._emit(
            {
                "kind": "device_event",
                "event_name": "healthy",
                "device_id": device_id,
                "boot_id": device["boot_id"],
                "host_receive_wall_ns": time.time_ns(),
                "demo": True,
            }
        )
        return {
            "operation_id": operation_id.hex(),
            "job_id": 2,
            "manifest_sha256": bundle.manifest_sha256.hex(),
            "target_layout_sha256": bundle.target_layout_sha256,
            "components": [item.as_dict() for item in bundle.components],
            "bytes": total,
            "restart_required": True,
            "completion_evidence": "session_close",
            "demo": True,
        }

    async def system_update_inventory(self, device_id: str) -> dict[str, Any]:
        device = self.get(device_id)
        return {
            "flags": 0x7,
            "layout_version": 1,
            "bootloader_sha256": device.get("bootloader_sha256", "00" * 32),
            "partition_table_sha256": device.get(
                "partition_table_sha256", "00" * 32
            ),
            "last_operation_id": device.get(
                "last_system_update_operation_id", "00" * 16
            ),
            "last_result": 0,
            "demo": True,
        }

    async def restart(self, device_id: str, delay_ms: int = 250) -> int:
        device = self.get(device_id)
        await asyncio.sleep(min(delay_ms / 1000, 0.25))
        device["boot_id"] += 1
        device["session_id"] += 1
        await self._emit(
            {
                "kind": "connection",
                "connection_state": "rebooted",
                "device_id": device_id,
                "boot_id": device["boot_id"],
                "session_id": device["session_id"],
                "endpoint": device["endpoint"],
                "host_receive_wall_ns": time.time_ns(),
                "demo": True,
            }
        )
        return delay_ms

    async def input_event(self, device_id: str, gesture: dict[str, Any]) -> dict[str, Any]:
        self.get(device_id)
        return {"accepted": True, "points": len(gesture.get("moves", [])) + 2, "demo": True}

    async def enter_recovery(self, device_id: str) -> dict[str, Any]:
        device = self.get(device_id)
        await asyncio.sleep(0.1)
        device["firmware_mode"] = "recovery"
        await self.restart(device_id, 250)
        return {
            "accepted": True,
            "restart_planned": True,
            "target": "factory_recovery",
            "boot_id": device["boot_id"],
            "demo": True,
        }

    async def audio_upload(self, device_id: str, data: bytes, content_type: str) -> dict[str, Any]:
        self.get(device_id)
        return {"accepted": True, "bytes": len(data), "content_type": content_type, "demo": True}

    async def close(self) -> None:
        tasks = [task for task in [self._task, *self._media_tasks.values()] if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._media_tasks.clear()

    async def _run(self) -> None:
        messages = [
            ("I", "app", "application healthy; acceptance gate passed"),
            ("D", "esp_iris", "LOG credit replenished"),
            ("I", "sensor", "frame sampled and published"),
            ("W", "wifi", "demo link RSSI below preferred threshold"),
            ("I", "mosaico", "worker queue idle"),
        ]
        while True:
            await asyncio.sleep(0.45)
            self._phase += 1
            device = list(self._devices.values())[self._phase % len(self._devices)]
            if not device["connected"]:
                continue
            level, tag, message = messages[self._phase % len(messages)]
            monotonic_us = (time.monotonic_ns() - self._started_ns) // 1000
            await self._emit(
                {
                    "kind": "log",
                    "device_id": device["device_id"],
                    "boot_id": device["boot_id"],
                    "monotonic_us": monotonic_us,
                    "estimated_wall_ns": time.time_ns(),
                    "clock_uncertainty_us": 410,
                    "host_receive_monotonic_ns": time.monotonic_ns(),
                    "host_receive_wall_ns": time.time_ns(),
                    "dropped_bytes": 0,
                    "source": 0,
                    "flags": 0,
                    "text": f"{level} ({monotonic_us // 1000}) {tag}: {message}",
                    "demo": True,
                }
            )
            if self._phase % 31 == 0:
                target = list(self._devices.values())[1]
                target["connected"] = False
                await self._emit(
                    {
                        "kind": "connection",
                        "connection_state": "disconnected",
                        "device_id": target["device_id"],
                        "boot_id": target["boot_id"],
                        "host_receive_wall_ns": time.time_ns(),
                        "demo": True,
                    }
                )
            elif self._phase % 31 == 4:
                target = list(self._devices.values())[1]
                target["connected"] = True
                await self._emit(
                    {
                        "kind": "connection",
                        "connection_state": "reconnected",
                        "device_id": target["device_id"],
                        "boot_id": target["boot_id"],
                        "host_receive_wall_ns": time.time_ns(),
                        "demo": True,
                    }
                )

    async def _run_media(self, device_id: str, channel: int, fps: int) -> None:
        while True:
            await asyncio.sleep(1 / fps)
            if channel in (3, 4):
                data = _png(640, 360, self._phase)
                description = {"width": 640, "height": 360, "format": 4}
            else:
                data = bytes(random.randrange(0, 255) for _ in range(320))
                description = {"sample_rate": 16000, "channels": 1, "format": 1}
            event = {
                "device_id": device_id,
                "channel": channel,
                "description": description,
                "frame_id": self._phase,
                "host_receive_wall_ns": time.time_ns(),
                "data": data,
                "demo": True,
            }
            for queue in tuple(self._media_subscribers[(device_id, channel)]):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(event)

    async def _emit(self, event: dict[str, Any]) -> None:
        device_id = event.get("device_id")
        if device_id:
            for queue in tuple(self._subscribers[str(device_id)]):
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(event)
        if self._event_sink:
            await self._event_sink(event)


__all__ = ["DemoHub"]
