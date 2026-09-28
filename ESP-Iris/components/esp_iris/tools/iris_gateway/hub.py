from __future__ import annotations

import asyncio
import collections
import contextlib
import struct
import time
from collections.abc import AsyncIterator
from typing import Any, Awaitable, Callable, Dict

from .compat import to_thread
from .discovery import (
    discover_iris_usb_devices,
    iris_usb_allowed,
    resolve_usb_port,
    usb_endpoint,
)
from .link import EndpointLock, Link, SerialLink, TcpLink
from .mdns_discovery import IrisMdnsDevice, IrisMdnsDiscovery
from .ownership import OwnershipRegistry
from .protocol import ProtocolError, Transport
from .session import DeviceSession
from .system_update import SystemUpdateBundle
from .usb_ownership import UsbEndpointBusy, usb_admission

LinkOpener = Callable[[], Awaitable[Link]]
EventCallback = Callable[[Dict[str, Any]], Awaitable[None]]
SCREEN_CHANNEL = 3
RAW_MEDIA_FORMATS = {1, 2}
ENCODED_MEDIA_FORMATS = {3, 4}


async def _next_complete_screen_frame(
    queue: asyncio.Queue[dict[str, Any]],
    full_description: dict[str, Any],
    *,
    stream_id: int | None,
    timeout: float = 5.0,
) -> tuple[dict[str, int], bytes]:
    full = {key: int(value) for key, value in full_description.items()}
    format_ = int(full.get("format", 0))
    if format_ not in RAW_MEDIA_FORMATS | ENCODED_MEDIA_FORMATS:
        raise ValueError(f"unsupported screen mirror format: {format_}")
    base_y = int(full.get("y", 0))
    width = int(full.get("width", 0))
    height = int(full.get("height", 0))
    stride = int(full.get("stride", 0))
    if (
        width <= 0
        or height <= 0
        or (format_ in RAW_MEDIA_FORMATS and stride <= 0)
    ):
        raise ValueError("screen mirror has an empty frame description")

    async def receive() -> tuple[dict[str, int], bytes]:
        frame_id: int | None = None
        next_y = base_y
        data = bytearray()
        while True:
            event = await queue.get()
            if stream_id is not None and int(event.get("stream_id", -1)) != stream_id:
                continue
            tile = {
                key: int(value)
                for key, value in dict(event.get("description") or {}).items()
            }
            tile_format = int(tile.get("format", 0))
            payload = bytes(event.get("data") or b"")
            if tile_format in ENCODED_MEDIA_FORMATS:
                if not payload:
                    continue
                return tile, payload
            if tile_format != format_:
                continue

            tile_y = int(tile.get("y", -1))
            tile_height = int(tile.get("height", 0))
            current_frame_id = int(event.get("frame_id", 0))
            if tile_y == base_y:
                frame_id = current_frame_id
                next_y = base_y
                data.clear()
            if frame_id != current_frame_id or tile_y != next_y:
                continue
            if (
                int(tile.get("x", full.get("x", 0))) != int(full.get("x", 0))
                or int(tile.get("width", 0)) != width
                or int(tile.get("stride", 0)) != stride
                or tile_height <= 0
                or len(payload) != stride * tile_height
            ):
                raise ValueError(
                    "screen mirror tile does not match the frame description"
                )
            data.extend(payload)
            next_y += tile_height
            if next_y == base_y + height:
                if len(data) != stride * height:
                    raise ValueError("assembled screen frame has an invalid size")
                return full, bytes(data)
            if next_y > base_y + height:
                raise ValueError("screen mirror tile exceeds the frame boundary")

    return await asyncio.wait_for(receive(), timeout=timeout)


class IrisHub:
    def __init__(
        self,
        instance_id: str = "default",
        *,
        reconnect_min_seconds: float = 0.25,
        reconnect_max_seconds: float = 5.0,
        event_sink: EventCallback | None = None,
        hello_timeout_seconds: float = 5.0,
        ownership: OwnershipRegistry | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.ownership = ownership
        self._candidates: dict[str, dict[str, Any]] = {}
        self.hello_timeout_seconds = hello_timeout_seconds
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self._event_sink = event_sink
        self._devices: dict[str, DeviceSession] = {}
        self._last_boot_id: dict[str, int] = {}
        self._endpoint_tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, EndpointLock] = {}
        self._endpoint_states: dict[str, dict[str, Any]] = {}
        self._endpoint_configs: dict[str, tuple[LinkOpener, str | None]] = {}
        self._host_endpoints: set[str] = set()
        self._pending_usb: dict[str, dict[str, Any]] = {}
        self._usb_retry_task: asyncio.Task[None] | None = None
        self._discovery_task: asyncio.Task[None] | None = None
        self._mdns_discovery: IrisMdnsDiscovery | None = None
        self._mdns_services: dict[str, str] = {}
        self._mdns_devices: dict[str, str] = {}
        self._closing = False
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = (
            collections.defaultdict(set)
        )
        self._history: dict[str, collections.deque[dict[str, Any]]] = (
            collections.defaultdict(lambda: collections.deque(maxlen=100))
        )
        self._media_subscribers: dict[
            tuple[str, int], set[asyncio.Queue[dict[str, Any]]]
        ] = collections.defaultdict(set)
        self._media_locks: dict[tuple[str, int], asyncio.Lock] = (
            collections.defaultdict(asyncio.Lock)
        )
        self._mirror_states: dict[tuple[str, int], dict[str, Any]] = {}

    async def add_tcp(
        self,
        host: str,
        port: int = 19772,
        pairing_token: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        endpoint = f"tcp:{host}:{port}"

        async def opener() -> Link:
            return await TcpLink.open(host, port)

        self._add_supervisor(
            endpoint, opener, pairing_token=pairing_token, metadata=metadata
        )

    async def add_usb(
        self,
        port: str,
        firmware_mode: str | None = None,
        *,
        usb_serial_jtag: bool = False,
        metadata: dict[str, Any] | None = None,
        discovered: bool = False,
    ) -> None:
        if self._closing:
            raise RuntimeError("ESP-Iris Hub is closing")
        # Keep the selector stable even when a previously dangling symlink
        # starts resolving to a different tty path.
        pending = "usb:pending=" + port
        try:
            resolved: dict[str, Any] = await to_thread(resolve_usb_port, port)
        except OSError as exc:
            prior = self._pending_usb.get(pending, {})
            self._pending_usb[pending] = {
                "port": port, "firmware_mode": firmware_mode,
                "usb_serial_jtag": usb_serial_jtag or prior.get("usb_serial_jtag", False),
                "discovered": discovered and prior.get("discovered", True),
            }
            self._endpoint_states[pending] = {
                "endpoint": pending, "path": port, "state": "waiting",
                "device_id": None, "error": str(exc),
                "attempt": int(self._endpoint_states.get(pending, {}).get("attempt", 0)) + 1,
                "updated_monotonic_ns": time.monotonic_ns(),
            }
            self._ensure_usb_retry()
            return
        prior = self._pending_usb.pop(pending, {})
        discovered = discovered and prior.get("discovered", True)
        usb_serial_jtag = usb_serial_jtag or prior.get("usb_serial_jtag", False)
        self._endpoint_states.pop(pending, None)
        endpoint = usb_endpoint(resolved)
        # All aliases share the same physical lock and supervisor.
        endpoint = next((key for key, state in self._endpoint_states.items()
                         if state.get("lock_endpoint") == endpoint), endpoint)
        previous = self._endpoint_states.get(endpoint, {})
        if discovered and endpoint in self._host_endpoints:
            return
        resolved["usb_selection"] = (
            "explicit" if not discovered else previous.get("usb_selection", "discovery")
        )
        resolved["allow_serial_jtag"] = (
            usb_serial_jtag or bool(previous.get("allow_serial_jtag"))
        )

        async def opener() -> Link:
            return await self._open_usb(endpoint)

        self._add_supervisor(endpoint, opener, metadata=resolved, defer_lock=True)
        if endpoint not in self._endpoint_states:
            return
        if firmware_mode is None:
            lowered = port.lower()
            if "recovery" in lowered:
                firmware_mode = "recovery"
            elif "normal" in lowered:
                firmware_mode = "normal"
        # The supervisor can complete HELLO before add_usb() resumes. Preserve
        # the identity-derived mode in that race; otherwise seed the state from
        # discovery until HELLO provides the authoritative value.
        self._endpoint_states[endpoint].setdefault(
            "firmware_mode", firmware_mode or "unknown"
        )
        self._endpoint_states[endpoint]["transport_name"] = (
            "USB Serial/JTAG" if resolved["pid"] == 0x1001 else "USB Highspeed"
        )

    def _ensure_usb_retry(self) -> None:
        if self._usb_retry_task is None or self._usb_retry_task.done():
            self._usb_retry_task = asyncio.create_task(self._retry_usb())

    async def _retry_usb(self) -> None:
        delay = max(0.01, self.reconnect_min_seconds)
        while self._pending_usb:
            for options in list(self._pending_usb.values()):
                await self.add_usb(**options)
            await asyncio.sleep(delay)
            delay = min(self.reconnect_max_seconds, max(delay * 2, 0.01))

    def _claim_usb(self, endpoint: str, metadata: dict[str, Any]) -> None:
        with usb_admission():
            if endpoint not in self._locks:
                lock = EndpointLock(usb_endpoint(metadata))
                try:
                    lock.acquire()
                except RuntimeError as exc:
                    lock.close()
                    raise UsbEndpointBusy(str(exc)) from exc
                except BaseException:
                    lock.close()
                    raise
                self._locks[endpoint] = lock

    async def _open_usb(self, endpoint: str) -> Link:
        if self.ownership is not None:
            self.ownership._require(endpoint)
        state = self._endpoint_states[endpoint]
        current = await to_thread(
            resolve_usb_port, str(state.get("lock_endpoint") or endpoint)
        )
        allow_jtag = bool(state.get("allow_serial_jtag"))
        if not iris_usb_allowed(
            current, allow_serial_jtag=allow_jtag,
            explicit=state.get("usb_selection") == "explicit",
        ):
            raise OSError("USB interface is not eligible for ESP-Iris; left unopened")
        self._claim_usb(endpoint, current)
        state.update(current)
        state["transport_name"] = (
            "USB Serial/JTAG" if current["pid"] == 0x1001 else "USB Highspeed"
        )
        return await SerialLink.open(
            current["path"], endpoint=endpoint,
            hupcl=False if current.get("pid") == 0x1001 else None,
        )

    def _add_supervisor(
        self,
        endpoint: str,
        opener: LinkOpener,
        *,
        pairing_token: str | None = None,
        metadata: dict[str, Any] | None = None,
        defer_lock: bool = False,
    ) -> None:
        if self._closing:
            raise RuntimeError("ESP-Iris Hub is closing")
        if self.ownership is not None:
            candidate = {"endpoint": endpoint, **(metadata or {}),
                         "last_seen_ns": time.time_ns(), "present": True}
            self._candidates[endpoint] = candidate
            if not self.ownership.allowed(endpoint):
                device_id = candidate.get("advertised_device_id")
                # Descriptor serials are hints; HELLO still verifies identity.
                if not device_id and candidate.get("serial_number"):
                    matches = {item["device_id"] for item in self.ownership.claims()
                               if item["owner"] == self.ownership.session_id
                               and item["state"] == "owned" and item["device_id"]
                               and item["metadata"].get("serial_number") == candidate["serial_number"]}
                    if len(matches) == 1:
                        device_id = matches.pop()
                device_claim = self.ownership.claim("device:" + str(device_id)) if device_id else None
                if (device_claim and self.ownership.allowed("device:" + str(device_id))
                        and not device_claim["metadata"].get("identity_probe")):
                    self.ownership.acquire(endpoint, candidate)
                    self.ownership.bind(endpoint, str(device_id), verified=False)
                else:
                    return
        self._endpoint_configs[endpoint] = (opener, pairing_token)
        state = self._endpoint_states.setdefault(
            endpoint,
            {
                "endpoint": endpoint,
                "state": "waiting",
                "attempt": 0,
                "error": None,
                "device_id": None,
                "updated_monotonic_ns": time.monotonic_ns(),
            },
        )
        state.update(metadata or {})
        if endpoint in self._endpoint_tasks:
            return
        if endpoint in self._host_endpoints:
            state["state"] = "host_operation"
            state["updated_monotonic_ns"] = time.monotonic_ns()
            return
        if endpoint not in self._locks and not defer_lock:
            lock = EndpointLock(endpoint)
            try:
                lock.acquire()
            except BaseException as exc:
                lock.close()
                state.update(state="owned_elsewhere", error=str(exc))
                raise
            self._locks[endpoint] = lock
        state.update(
            state="waiting",
            attempt=0,
            error=None,
            updated_monotonic_ns=time.monotonic_ns(),
        )
        self._endpoint_tasks[endpoint] = asyncio.create_task(
            self._supervise(endpoint, opener, pairing_token),
            name=f"iris-supervisor-{endpoint}",
        )

    async def start_usb_discovery(
        self,
        interval_seconds: float = 1.0,
        *,
        include_usb_serial_jtag: bool = False,
    ) -> None:
        if self._discovery_task is not None:
            return

        async def discover_loop() -> None:
            while True:
                try:
                    devices = await to_thread(
                        discover_iris_usb_devices,
                        include_usb_serial_jtag=include_usb_serial_jtag,
                    )
                except (OSError, ImportError):
                    devices = []
                if self.ownership is not None:
                    for candidate in self._candidates.values():
                        if candidate["endpoint"].startswith("usb:"):
                            candidate["present"] = False
                for device in devices:
                    product = device.product.lower()
                    firmware_mode = (
                        "recovery" if "recovery" in product else "normal"
                        if "normal" in product
                        else "unknown"
                    )
                    with contextlib.suppress(RuntimeError, OSError):
                        await self.add_usb(
                            device.path,
                            firmware_mode=firmware_mode,
                            usb_serial_jtag=(
                                device.transport == "usb_serial_jtag"
                            ),
                            discovered=True,
                            metadata={
                                "path": device.path,
                                "device_path": device.device,
                                "vid": device.vid,
                                "pid": device.pid,
                                "serial_number": device.serial_number,
                                "product": device.product,
                                "location": device.location,
                            },
                        )
                await asyncio.sleep(interval_seconds)

        self._discovery_task = asyncio.create_task(
            discover_loop(), name="iris-usb-discovery"
        )

    async def start_mdns_discovery(
        self, pairing_token: str | None = None
    ) -> None:
        if self._mdns_discovery is not None:
            return

        async def on_service(device: IrisMdnsDevice) -> None:
            await self._add_mdns_device(device, pairing_token)

        discovery = IrisMdnsDiscovery(on_service, self._remove_mdns_service)
        await discovery.start()
        self._mdns_discovery = discovery

    async def _add_mdns_device(
        self, device: IrisMdnsDevice, pairing_token: str | None
    ) -> None:
        endpoint = f"tcp:{device.host}:{device.port}"
        old_service = self._mdns_devices.get(device.device_id)
        if old_service is not None and old_service != device.service_name:
            await self._remove_mdns_service(old_service)
        old_endpoint = self._mdns_services.get(device.service_name)
        if old_endpoint is not None and old_endpoint != endpoint:
            await self._remove_endpoint(old_endpoint, discovery="mdns")
        if endpoint not in self._endpoint_tasks:
            await self.add_tcp(
                device.host,
                device.port,
                pairing_token=pairing_token,
                metadata={
                    "discovery": "mdns",
                    "service_name": device.service_name,
                    "advertised_device_id": device.device_id,
                    "firmware_mode": device.mode,
                    "pairing": device.pairing,
                },
            )
        elif self._endpoint_states[endpoint].get("discovery") != "mdns":
            return
        else:
            self._endpoint_states[endpoint].update(
                {
                    "service_name": device.service_name,
                    "advertised_device_id": device.device_id,
                    "firmware_mode": device.mode,
                    "pairing": device.pairing,
                }
            )
        self._mdns_services[device.service_name] = endpoint
        self._mdns_devices[device.device_id] = device.service_name

    async def _remove_mdns_service(self, service_name: str) -> None:
        endpoint = self._mdns_services.pop(service_name, None)
        if endpoint is None:
            return
        self._candidates.pop(endpoint, None)
        state = self._endpoint_states.get(endpoint, {})
        device_id = state.get("advertised_device_id")
        if device_id is not None and self._mdns_devices.get(device_id) == service_name:
            self._mdns_devices.pop(device_id, None)
        await self._remove_endpoint(endpoint, discovery="mdns")

    async def disconnect_owned_endpoint(self, endpoint: str) -> None:
        """Close a failed admission attempt without disturbing other endpoints."""
        if self.ownership is not None:
            self.ownership._require(endpoint)
        await self._remove_endpoint(endpoint)

    async def _remove_endpoint(
        self, endpoint: str, *, discovery: str | None = None
    ) -> None:
        state = self._endpoint_states.get(endpoint)
        if state is None or (
            discovery is not None and state.get("discovery") != discovery
        ):
            return
        task = self._endpoint_tasks.pop(endpoint, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        lock = self._locks.pop(endpoint, None)
        if lock is not None:
            lock.close()
        self._endpoint_states.pop(endpoint, None)
        self._endpoint_configs.pop(endpoint, None)
        self._host_endpoints.discard(endpoint)
        self._pending_usb.pop(endpoint, None)

    def host_endpoint(self, identifier: str) -> dict[str, Any]:
        """Resolve or register one physical endpoint without opening a session."""

        if identifier in self._pending_usb:
            raise LookupError("USB selector is not yet bound to a physical endpoint")
        if identifier in self._endpoint_states:
            return self._endpoint_states[identifier].copy()
        # A tty number can be reused at a different USB socket. Never match it
        # against a historical device_path before resolving the live descriptor.
        metadata = resolve_usb_port(identifier)
        endpoint = usb_endpoint(metadata)
        endpoint = next((key for key, state in self._endpoint_states.items()
                         if state.get("lock_endpoint") == endpoint), endpoint)
        if endpoint in self._endpoint_states:
            self._endpoint_states[endpoint].update(metadata)
            return self._endpoint_states[endpoint].copy()
        state = self._endpoint_states.setdefault(
            endpoint,
            {
                "endpoint": endpoint,
                "state": "unmanaged",
                "attempt": 0,
                "error": None,
                "device_id": None,
                "updated_monotonic_ns": time.monotonic_ns(),
            },
        )
        state.update(metadata)
        state.update(firmware_mode="unknown", allow_serial_jtag=False,
                     transport_name="USB Serial/JTAG" if metadata["pid"] == 0x1001 else "USB Highspeed")

        async def opener() -> Link:
            return await self._open_usb(endpoint)

        self._endpoint_configs[endpoint] = (opener, None)
        return state.copy()

    async def detach_for_host(self, identifier: str) -> dict[str, Any]:
        """Close one local USB endpoint even when HELLO identity is unavailable."""

        resolved = self.host_endpoint(identifier)
        endpoint = str(resolved["endpoint"])
        if not endpoint.startswith("usb:"):
            raise RuntimeError("host operations require a local USB endpoint")
        if self.ownership is not None:
            self.ownership.acquire(endpoint, resolved)
        state = self._endpoint_states.get(endpoint)
        if state is None:
            raise RuntimeError("device endpoint state is unavailable")
        if endpoint in self._host_endpoints:
            raise RuntimeError("device endpoint has an active host operation")
        device_id = str(state.get("device_id") or "")
        session = self._devices.get(device_id) if device_id else None
        self._claim_usb(endpoint, state)
        state["resume_after_host"] = endpoint in self._endpoint_tasks
        self._host_endpoints.add(endpoint)
        task = self._endpoint_tasks.pop(endpoint, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if device_id:
            self._devices.pop(device_id, None)
        state.update(
            state="host_operation",
            device_id=device_id or None,
            error=None,
            updated_monotonic_ns=time.monotonic_ns(),
        )
        if device_id:
            await self._on_event(
                {
                    "kind": "connection",
                    "connection_state": "host_operation",
                    "device_id": device_id,
                    "boot_id": session.info.boot_id if session and session.info else None,
                    "session_id": (
                        session.info.session_id if session and session.info else None
                    ),
                    "endpoint": endpoint,
                    "host_receive_monotonic_ns": time.monotonic_ns(),
                    "host_receive_wall_ns": time.time_ns(),
                }
            )
        return state.copy()

    def yield_host_lock(self, endpoint: str) -> None:
        """Let the supervised writer acquire the physical lock; retain ownership."""
        if endpoint not in self._host_endpoints:
            raise RuntimeError("endpoint was not detached for a host operation")
        lock = self._locks.pop(endpoint, None)
        if lock is not None:
            lock.close()

    async def resume_after_host(self, endpoint: str, *, connect: bool = False) -> None:
        if endpoint not in self._host_endpoints:
            return
        if not connect and not self._endpoint_states[endpoint].get("resume_after_host", False):
            await self._remove_endpoint(endpoint)
            if self.ownership is not None:
                self.ownership.release(endpoint)
            return
        opener, pairing_token = self._endpoint_configs[endpoint]
        self._host_endpoints.remove(endpoint)
        self._add_supervisor(endpoint, opener, pairing_token=pairing_token, defer_lock=True)

    def _set_endpoint_state(
        self,
        endpoint: str,
        state: str,
        *,
        attempt: int,
        error: str | None = None,
        device_id: str | None = None,
    ) -> None:
        current = self._endpoint_states[endpoint]
        current.update(
            {
                "state": state,
                "attempt": attempt,
                "error": error,
                "updated_monotonic_ns": time.monotonic_ns(),
            }
        )
        if device_id is not None:
            current["device_id"] = device_id

    async def _supervise(
        self,
        endpoint: str,
        opener: LinkOpener,
        pairing_token: str | None,
    ) -> None:
        attempt = 0
        delay = self.reconnect_min_seconds
        active_session: DeviceSession | None = None
        try:
            while True:
                attempt += 1
                retry_state = "retrying"
                self._set_endpoint_state(
                    endpoint, "connecting", attempt=attempt, error=None
                )
                try:
                    if self.ownership is not None:
                        self.ownership._require(endpoint)
                    link = await opener()
                    self._set_endpoint_state(
                        endpoint, "handshaking", attempt=attempt, error=None
                    )
                    active_session = DeviceSession(
                        link,
                        self._on_ready,
                        self._on_event,
                        on_media=self._on_media,
                        pairing_token=pairing_token,
                    )
                    run_task = asyncio.create_task(active_session.run())
                    ready_task = asyncio.create_task(active_session.wait_ready(self.hello_timeout_seconds))
                    try:
                        done, _ = await asyncio.wait(
                            (run_task, ready_task), return_when=asyncio.FIRST_COMPLETED
                        )
                        if ready_task in done:
                            await ready_task
                        await run_task
                    finally:
                        for task in (ready_task, run_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(ready_task, run_task, return_exceptions=True)
                    failure = "link closed"
                except asyncio.CancelledError:
                    raise
                except (ConnectionError, OSError, ProtocolError, RuntimeError, asyncio.TimeoutError) as exc:
                    failure = str(exc)
                    if isinstance(exc, UsbEndpointBusy):
                        retry_state = "owned_elsewhere"

                device_id = self._endpoint_states[endpoint].get("device_id")
                if device_id:
                    current = self._devices.get(device_id)
                    if current is not None and current.link.endpoint == endpoint:
                        await self._on_event(
                            {
                                "kind": "connection",
                                "connection_state": "disconnected",
                                "device_id": device_id,
                                "boot_id": current.info.boot_id if current.info else None,
                                "session_id": (
                                    current.info.session_id if current.info else None
                                ),
                                "endpoint": endpoint,
                                "host_receive_monotonic_ns": time.monotonic_ns(),
                                "host_receive_wall_ns": time.time_ns(),
                            }
                        )
                        del self._devices[device_id]
                    for key in [
                        key for key in self._mirror_states if key[0] == device_id
                    ]:
                        self._mirror_states.pop(key, None)
                active_session = None
                self._endpoint_states[endpoint]["device_id"] = None
                self._set_endpoint_state(
                    endpoint, retry_state, attempt=attempt, error=failure
                )
                await asyncio.sleep(delay)
                delay = min(self.reconnect_max_seconds, max(delay * 2, 0.001))
        finally:
            if active_session is not None and active_session.info is not None:
                current = self._devices.get(active_session.info.device_id)
                if current is active_session:
                    del self._devices[active_session.info.device_id]
            self._endpoint_states[endpoint]["device_id"] = None
            self._set_endpoint_state(
                endpoint, "stopped", attempt=attempt, error=None
            )

    async def _on_ready(self, session: DeviceSession) -> None:
        assert session.info is not None
        info = session.info
        endpoint_state = self._endpoint_states[session.link.endpoint]
        advertised_device_id = endpoint_state.get("advertised_device_id")
        if (
            advertised_device_id is not None
            and advertised_device_id != info.device_id
        ):
            await session.close()
            raise RuntimeError(
                "mDNS device_id does not match the ESP-Iris HELLO identity"
            )
        self._endpoint_states[session.link.endpoint]["firmware_mode"] = info.firmware_mode
        existing = self._devices.get(info.device_id)
        if existing is not None and existing is not session:
            await session.close()
            raise RuntimeError(
                f"device {info.device_id} already has a physical session"
            )
        if info.hardware_mac:
            for other_id, other in self._devices.items():
                if (other is not session and other.info is not None
                        and other.info.hardware_mac == info.hardware_mac
                        and other_id != info.device_id):
                    await session.close()
                    raise RuntimeError(
                        f"hardware MAC {info.hardware_mac} is already associated "
                        f"with device {other_id}"
                    )
        if self.ownership is not None:
            try:
                self.ownership.bind(session.link.endpoint, info.device_id)
            except RuntimeError:
                await session.close()
                raise
        self._devices[info.device_id] = session
        self._set_endpoint_state(
            session.link.endpoint,
            "ready",
            attempt=self._endpoint_states[session.link.endpoint]["attempt"],
            device_id=info.device_id,
        )
        previous_boot = self._last_boot_id.get(info.device_id)
        if previous_boot is None:
            connection_state = "connected"
        elif previous_boot == info.boot_id:
            connection_state = "reconnected"
        else:
            connection_state = "rebooted"
        self._last_boot_id[info.device_id] = info.boot_id
        await self._on_event(
            {
                "kind": "connection",
                "connection_state": connection_state,
                "device_id": info.device_id,
                "boot_id": info.boot_id,
                "session_id": info.session_id,
                "endpoint": info.endpoint,
                "host_receive_monotonic_ns": time.monotonic_ns(),
                "host_receive_wall_ns": time.time_ns(),
            }
        )

    async def _on_event(self, event: dict[str, Any]) -> None:
        device_id = event.get("device_id")
        if not device_id:
            return
        self._history[device_id].append(event)
        if self._event_sink is not None:
            await self._event_sink(event)
        for queue in tuple(self._subscribers.get(device_id, ())):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)

    async def _on_media(self, event: dict[str, Any]) -> None:
        device_id = event.get("device_id")
        channel = event.get("channel")
        if not device_id or channel is None:
            return
        for queue in tuple(
            self._media_subscribers.get((device_id, int(channel)), ())
        ):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)

    def list_devices(self) -> list[dict[str, Any]]:
        result = []
        for session in self._devices.values():
            if session.info is None:
                continue
            item = session.info.as_dict()
            state = self._endpoint_states.get(session.link.endpoint, {})
            item["firmware_mode"] = state.get("firmware_mode", "unknown")
            item["transport_name"] = {
                Transport.USB: "USB Highspeed",
                Transport.TCP: "TCP",
                Transport.USB_SERIAL_JTAG: "USB Serial/JTAG",
            }.get(Transport(session.info.transport), "Unknown")
            result.append(item)
        return result

    def list_endpoints(self) -> list[dict[str, Any]]:
        remembered = {} if self.ownership is None else {
            item["resource"]: {**item["metadata"], "endpoint": item["resource"], "present": False}
            for item in self.ownership.claims() if not item["resource"].startswith("device:")
        }
        endpoints = {**remembered, **self._candidates, **self._endpoint_states}
        result = [dict(endpoints[key]) for key in sorted(endpoints)]
        if self.ownership is not None:
            for item in result:
                item["ownership"] = self.ownership.claim(item["endpoint"])
                item.setdefault("state", "discovered")
        return result

    async def connect_owned(self, endpoint: str, metadata: dict[str, Any],
                            *, pairing_token: str | None = None) -> None:
        """Open an acquired endpoint, never a discovery wildcard."""
        if self.ownership is None:
            raise RuntimeError("project ownership is unavailable")
        self.ownership._require(endpoint)
        if endpoint.startswith("usb:"):
            selector = endpoint if endpoint.startswith("usb:location=") else metadata.get("path", endpoint)
            await self.add_usb(str(selector))
        elif endpoint.startswith("tcp:"):
            host, port = endpoint[4:].rsplit(":", 1)
            await self.add_tcp(host, int(port), pairing_token=pairing_token, metadata=metadata)
        else:
            raise ValueError("unsupported project endpoint")

    async def detach_owned(self, device_id: str) -> None:
        """Cancel supervisors before releasing locks; preserve discovery hints."""
        if self.ownership is None:
            raise RuntimeError("project ownership is unavailable")
        for claim in self.ownership.claims():
            if claim["device_id"] == device_id and not claim["resource"].startswith("device:"):
                await self._remove_endpoint(claim["resource"])

    def get(self, device_id: str) -> DeviceSession:
        try:
            return self._devices[device_id]
        except KeyError as exc:
            raise KeyError(f"unknown ESP-Iris device: {device_id}") from exc

    async def status(self, device_id: str) -> dict[str, Any]:
        session = self.get(device_id)
        result = await session.status()
        endpoint = self._endpoint_states.get(session.link.endpoint, {})
        result["firmware_mode"] = endpoint.get("firmware_mode", "unknown")
        result["endpoint"] = session.link.endpoint
        return result

    async def task_memory(self, device_id: str) -> dict[str, Any]:
        return await self.get(device_id).task_memory()

    async def file_volumes(self, device_id: str) -> dict[str, Any]:
        return await self.get(device_id).files.volumes()

    async def file_stat(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]:
        return await self.get(device_id).files.stat(volume, path)

    async def file_list(
        self,
        device_id: str,
        volume: str,
        path: str,
        *,
        cursor: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return await self.get(device_id).files.list_directory(
            volume, path, cursor=cursor, limit=limit
        )

    async def file_download(
        self,
        device_id: str,
        volume: str,
        path: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> AsyncIterator[bytes]:
        async for chunk in self.get(device_id).files.read_chunks(
            volume, path, offset=offset, length=length
        ):
            yield chunk

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
        return await self.get(device_id).files.upload(
            volume,
            path,
            chunks,
            total_size=total_size,
            overwrite=overwrite,
            if_match=if_match,
            progress=progress,
        )

    async def file_mkdir(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]:
        return await self.get(device_id).files.mkdir(volume, path)

    async def file_delete(
        self, device_id: str, volume: str, path: str
    ) -> dict[str, Any]:
        return await self.get(device_id).files.delete(volume, path)

    async def file_rename(
        self,
        device_id: str,
        volume: str,
        source: str,
        destination: str,
    ) -> dict[str, Any]:
        return await self.get(device_id).files.rename(
            volume, source, destination
        )

    async def crash_report(self, device_id: str) -> dict[str, Any]:
        return await self.get(device_id).crash_report()

    async def read_core_dump_chunk(
        self, device_id: str, offset: int, maximum: int = 1024
    ) -> tuple[int, bytes]:
        return await self.get(device_id).read_core_dump_chunk(offset, maximum)

    async def rpc(
        self,
        device_id: str,
        service_id: int,
        method_id: int,
        payload: bytes = b"",
        *,
        deadline_ms: int = 1000,
    ) -> bytes:
        return await self.get(device_id).rpc(
            service_id,
            method_id,
            payload,
            deadline_ms=deadline_ms,
        )

    async def job(
        self, device_id: str, job_id: int, *, cancel: bool = False
    ) -> dict[str, Any]:
        return await self.get(device_id).job(job_id, cancel=cancel)

    async def screenshot(
        self, device_id: str, description: dict[str, int] | None = None
    ) -> tuple[dict[str, int], bytes]:
        key = (device_id, SCREEN_CHANNEL)
        async with self._media_locks[key]:
            session = self.get(device_id)
            existing = self._mirror_states.get(key)
            reuse_existing = existing is not None
            started_temporary = False
            queue = self.subscribe_media(device_id, SCREEN_CHANNEL, maxsize=256)
            try:
                state = existing
                if state is None:
                    state = await session.mirror_start(
                        SCREEN_CHANNEL, description, fps=5
                    )
                    self._mirror_states[key] = state
                    started_temporary = True
                actual, data = await _next_complete_screen_frame(
                    queue,
                    dict(state.get("description") or {}),
                    stream_id=(
                        int(state["stream_id"])
                        if state.get("stream_id") is not None
                        else None
                    ),
                )
                actual["mirror_reused"] = int(reuse_existing)
                return actual, data
            finally:
                self.unsubscribe_media(device_id, SCREEN_CHANNEL, queue)
                if started_temporary:
                    await session.mirror_stop(SCREEN_CHANNEL)
                    self._mirror_states.pop(key, None)

    def active_mirrors(self, device_id: str) -> list[int]:
        return sorted(key[1] for key in self._mirror_states if key[0] == device_id)

    async def mirror_start(
        self,
        device_id: str,
        channel: int,
        description: dict[str, int] | None = None,
        *,
        fps: int = 5,
    ) -> dict[str, Any]:
        key = (device_id, channel)
        async with self._media_locks[key]:
            existing = self._mirror_states.get(key)
            if existing is not None:
                return {**existing, "reused": True}
            result = await self.get(device_id).mirror_start(
                channel, description, fps=fps
            )
            self._mirror_states[key] = result
            return {**result, "reused": False}

    async def mirror_stop(self, device_id: str, channel: int) -> None:
        key = (device_id, channel)
        async with self._media_locks[key]:
            await self.get(device_id).mirror_stop(channel)
            self._mirror_states.pop(key, None)

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
        return await self.get(device_id).ota_update(
            image,
            expected_sha256=expected_sha256,
            project_name=project_name,
            version=version,
            progress_callback=progress_callback,
        )

    async def ota_status(self, device_id: str) -> dict[str, Any]:
        return await self.get(device_id).ota_status()

    async def system_update(
        self,
        device_id: str,
        bundle: SystemUpdateBundle,
        *,
        operation_id: bytes,
        progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        return await self.get(device_id).system_update(
            bundle,
            operation_id=operation_id,
            progress_callback=progress_callback,
        )

    async def system_update_inventory(self, device_id: str) -> dict[str, Any]:
        return await self.get(device_id).system_update_inventory()

    async def restart(self, device_id: str, delay_ms: int = 250) -> int:
        return await self.get(device_id).restart(delay_ms)

    async def input_event(
        self, device_id: str, gesture: dict[str, Any]
    ) -> dict[str, Any]:
        """Send one normalized begin/moves/end gesture through pointer RPC.

        The device's full-screen description selects the geometry. The optional
        pointer/v1 profile owns the RPC IDs and fixed-size payload format.
        """

        begin = gesture.get("begin")
        end = gesture.get("end")
        moves = gesture.get("moves", [])
        if not isinstance(begin, dict) or not isinstance(end, dict):
            raise TypeError("gesture requires begin and end points")
        if not isinstance(moves, list) or len(moves) > 2048:
            raise ValueError("gesture moves must contain at most 2048 points")
        sequence = int(time.monotonic_ns() & 0xFFFFFFFF)
        from .service_profiles import POINTER_METHOD_ID, POINTER_SERVICE_ID

        screen = await self.get(device_id).screen_description()
        width, height = int(screen["width"]), int(screen["height"])
        if not (1 <= width <= 32768 and 1 <= height <= 32768):
            raise ValueError("device screen geometry exceeds the pointer/v1 coordinate range")

        def request(phase: int, point: dict[str, Any], value: int) -> bytes:
            x = min(width - 1, max(0, round(int(point.get("x", 0)) * (width - 1) / 10000)))
            y = min(height - 1, max(0, round(int(point.get("y", 0)) * (height - 1) / 10000)))
            return struct.pack("<BBhhHI", phase, 0, x, y, 0, value)

        points = [(0, begin), *((1, point) for point in moves), (2, end)]
        for index, (phase, point) in enumerate(points):
            response = await self.rpc(
                device_id,
                POINTER_SERVICE_ID,
                POINTER_METHOD_ID,
                request(phase, point, (sequence + index) & 0xFFFFFFFF),
                deadline_ms=1000,
            )
            if len(response) != 12:
                raise RuntimeError("pointer RPC returned an invalid response")
        return {
            "accepted": True,
            "points": len(points),
            "sequence_begin": sequence,
            "sequence_end": (sequence + len(points) - 1) & 0xFFFFFFFF,
        }

    async def enter_recovery(self, device_id: str) -> dict[str, Any]:
        from .service_profiles import ENTER_RECOVERY_METHOD_ID, RECOVERY_SERVICE_ID

        await self.rpc(device_id, RECOVERY_SERVICE_ID, ENTER_RECOVERY_METHOD_ID, b"", deadline_ms=2000)
        return {
            "accepted": True,
            "restart_planned": True,
            "target": "factory_recovery",
        }

    def subscribe(self, device_id: str) -> asyncio.Queue[dict[str, Any]]:
        self.get(device_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers[device_id].add(queue)
        for event in self._history.get(device_id, ()):
            queue.put_nowait(event)
        return queue

    def unsubscribe(self, device_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers[device_id].discard(queue)

    def subscribe_media(
        self, device_id: str, channel: int, *, maxsize: int = 8
    ) -> asyncio.Queue[dict[str, Any]]:
        self.get(device_id)
        if channel not in (3, 4, 5):
            raise ValueError("media channel must be 3, 4, or 5")
        if maxsize <= 0:
            raise ValueError("media subscriber queue size must be positive")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._media_subscribers[(device_id, channel)].add(queue)
        return queue

    def unsubscribe_media(
        self,
        device_id: str,
        channel: int,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        self._media_subscribers[(device_id, channel)].discard(queue)

    async def close(self) -> None:
        self._closing = True
        if self._usb_retry_task is not None:
            self._usb_retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._usb_retry_task
            self._usb_retry_task = None
        self._pending_usb.clear()
        if self._mdns_discovery is not None:
            await self._mdns_discovery.close()
            self._mdns_discovery = None
        self._mdns_services.clear()
        self._mdns_devices.clear()
        if self._discovery_task is not None:
            self._discovery_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._discovery_task
            self._discovery_task = None
        tasks = list(self._endpoint_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._endpoint_tasks.clear()
        self._endpoint_configs.clear()
        self._host_endpoints.clear()
        self._mirror_states.clear()
        for lock in self._locks.values():
            lock.close()
        self._locks.clear()
