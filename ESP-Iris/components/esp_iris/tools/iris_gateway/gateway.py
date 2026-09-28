from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import pathlib
import re
import struct
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import BodyPartReader, WSMsgType, web

from .boot_identity import boot_id_text
from .client_lifecycle import CAPABILITY as LIFECYCLE_CAPABILITY
from .compatibility import compatibility_expectation, validate_update_compatibility
from .contracts import GatewayHub
from .crashes import (
    archive_evidence,
    archive_key,
    auto_archive,
    matching_artifact,
)
from .crashes import register_routes as register_crash_routes
from .device_state import describe as describe_device
from .discovery import discover_iris_usb_devices, usb_endpoint
from .file_routes import register_file_routes
from .files import FileServiceError, file_error_http_status
from .firmware import inspect_firmware_image
from .host_operations import HostOperations, active_workers
from .host_operations import register_routes as register_host_routes
from .http_support import (
    ACTOR_CONTEXT,
    PUBLIC_API,
)
from .http_support import (
    error_response as _error,
)
from .http_support import (
    json_body as _json_body,
)
from .http_support import (
    request_actor as _actor,
)
from .http_support import (
    request_is_loopback as _request_is_loopback,
)
from .media import encode_media_image
from .memory_observation import memory_snapshot as sample_memory
from .observability import MetricsRegistry, normalize_event
from .openapi_contract import build_openapi
from .operation_identity import OperationConflict
from .operations import (
    DeviceBusy,
    OperationCancelled,
    OperationManager,
    OperationOutcomeUnknown,
)
from .ota_validation import (
    DEFAULT_OTA_VALIDATION_MODE,
    OTA_VALIDATION_MODES,
    _require_ota_validation_mode,
    _validate_ota_identity,
)
from .reconciliation import health_timeout, reconcile_operation, reconciliation_records
from .recovery_transition import (
    enter_recovery,
    recovery_preconditions,
    validate_recovery,
)
from .security import Actor, AuthManager
from .source_identity import running_source
from .store import GatewayStore
from .system_update import (
    SystemUpdateBundle,
    load_system_update_bundle,
)
from .system_update_workflow import run_system_update
from .update_acceptance import validate_updated_contract

LOG_PATTERN = re.compile(r"^(?P<level>[EWIDV])\s+\((?P<stamp>\d+)\)\s+(?P<tag>[^:]+):\s?(?P<message>.*)$")
CONSOLE_METHOD_NAME = "console.execute"
CONSOLE_LINE_MAX_BYTES = 255
GATEWAY_CLIENT_MAX_SIZE = 1024 * 1024 * 1024
GATEWAY_API = {"major": 1, "minor": 2}
GATEWAY_CAPABILITIES = [
    "local-host-operations/v1",
    "device-states/v1",
    "device-takeover/v1",
    "system-inventory/v1",
    "recovery-preconditions/v1",
    "update-acceptance/v1",
    "recovery-transition/v1",
]


class GatewayService:
    def __init__(
        self,
        store: GatewayStore,
        *,
        instance_id: str,
        demo: bool = False,
        require_local_auth: bool = False,
        frontend_dist: pathlib.Path | None = None,
        system_update_trust_key: bytes | None = None,
        ota_health_timeout: float | None = None,
    ) -> None:
        self.store = store
        self.source_identity = running_source()
        self.instance_id = instance_id
        self.demo = demo
        self.require_local_auth = require_local_auth
        self.frontend_dist = frontend_dist
        self.system_update_trust_key = system_update_trust_key
        if ota_health_timeout is not None:
            health_timeout({}, ota_health_timeout)
        self.ota_health_timeout = ota_health_timeout
        self.auth = AuthManager(store)
        self.hub: GatewayHub | None = None
        self.metrics = MetricsRegistry()
        # Runtime mode is intentionally not resumed. Every gateway process
        # starts in develop as the explicit local-development default.
        self.mode = "develop"
        store.set_setting("mode", self.mode)
        self.mode_transition = False
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._memory_requests: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._crash_archives_in_progress: set[str] = set()
        self.operations = OperationManager(store, self.on_device_event, self.metrics)
        self.host_id = str(store.get_setting("host_id") or uuid.uuid4())
        store.set_setting("host_id", self.host_id)
        self.host_operations = HostOperations(self)
        self.rpc_catalog = self._load_rpc_catalog()
        self.project: Any = None
        self.jobs: dict[tuple[str, int], dict[str, Any]] = {}

    def attach_hub(self, hub: GatewayHub) -> None:
        self.hub = hub

    @property
    def device_hub(self) -> GatewayHub:
        if self.hub is None:
            raise RuntimeError("device hub is not attached")
        return self.hub

    def authentication_required(self, request: web.Request) -> bool:
        return self.require_local_auth or not _request_is_loopback(request)

    def _load_rpc_catalog(self) -> dict[str, Any]:
        path = pathlib.Path(__file__).resolve().parent.parent / "rpc_catalog.json"
        if not path.exists():
            return {"schema": "esp-iris-rpc-catalog/v1", "methods": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def observe_device_activity(self, event: dict[str, Any]) -> None:
        device_id = str(event.get("device_id") or "")
        if event.get("kind") == "job":
            key = (device_id, int(event["job_id"]))
            if event.get("job_state") in {"succeeded", "failed", "cancelled"}:
                self.jobs.pop(key, None)
            else:
                self.jobs[key] = dict(event)
        elif event.get("kind") == "connection" and event.get("connection_state") == "rebooted":
            for key in list(self.jobs):
                if key[0] == device_id:
                    del self.jobs[key]

    async def on_device_event(self, event: dict[str, Any]) -> None:
        self.observe_device_activity(event)
        item = dict(event)
        device_id = item.get("device_id")
        kind = str(item.get("kind", "device_event"))
        if kind == "log":
            match = LOG_PATTERN.match(str(item.get("text", "")))
            if match:
                item["parsed"] = match.groupdict()
            category = "log"
        elif kind == "operation":
            category = "operation"
        else:
            category = "device"
        item = normalize_event(item, category=category, component="gateway")
        self.metrics.increment(f"events.{category}")
        if device_id and self.hub is not None:
            for current in self.hub.list_devices():
                if current.get("device_id") == device_id:
                    current = dict(current)
                    current["connected"] = True
                    self.store.remember_device(current)
                    break
        persisted = self.store.append_event(category, item, device_id)
        for queue in tuple(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                gap = {
                    "kind": "history_gap",
                    "category": "system",
                    "host_receive_ns": time.time_ns(),
                    "reason": "live subscriber queue overflow",
                }
                queue.put_nowait(gap)
            queue.put_nowait(persisted)
        self.metrics.gauge("events.subscribers", len(self._subscribers))
        if (
            device_id
            and item.get("event_name") == "core_dump_available"
            and device_id not in self._crash_archives_in_progress
        ):
            self._crash_archives_in_progress.add(str(device_id))
            asyncio.create_task(self._auto_archive_crash(str(device_id)))

    def _matching_crash_artifact(
        self, report: dict[str, Any]
    ) -> dict[str, Any] | None:
        return matching_artifact(self.store, report)

    @staticmethod
    def _crash_archive_key(device_id: str, report: dict[str, Any]) -> str:
        return archive_key(device_id, report)

    async def archive_crash_evidence(self, device_id: str) -> dict[str, Any]:
        return await archive_evidence(self, device_id)

    async def _auto_archive_crash(self, device_id: str) -> None:
        await auto_archive(self, device_id)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._subscribers.add(queue)
        self.metrics.gauge("events.subscribers", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
        self.metrics.gauge("events.subscribers", len(self._subscribers))

    def require_develop(self) -> web.Response | None:
        if self.mode == "develop":
            return None
        return _error(
            423,
            "observe_mode",
            "all business device requests are stopped while gateway mode is observe",
            mode=self.mode,
        )

    async def switch_mode(self, mode: str, actor: Actor) -> dict[str, Any]:
        if mode not in ("develop", "observe"):
            raise ValueError("mode must be develop or observe")
        previous = self.mode
        if previous == mode:
            return self.mode_state()
        self.mode = mode
        self.store.set_setting("mode", mode)
        cancelled = 0
        if mode == "observe":
            self.mode_transition = True
            cancelled = await self.operations.cancel_queued()
            asyncio.create_task(self._finish_mode_transition())
        else:
            self.mode_transition = False
        audit = self.store.add_audit(
            actor.kind,
            actor.name,
            "mode.changed",
            {"from": previous, "to": mode, "cancelled_queued": cancelled},
        )
        await self._broadcast_system(audit)
        return self.mode_state()

    async def _finish_mode_transition(self) -> None:
        while any(
            self.operations.queue_state(device.get("device_id", ""))["running"]
            for device in self.store.cached_devices()
        ):
            await asyncio.sleep(0.05)
        self.mode_transition = False

    def mode_state(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "transitioning": self.mode_transition,
            "default": "develop",
            "observe_behavior": "cached_state_only",
        }

    async def _broadcast_system(self, audit: dict[str, Any]) -> None:
        item = self.store.append_event(
            "system",
            {"kind": "system_audit", "audit": audit, "host_receive_wall_ns": time.time_ns()},
        )
        for queue in tuple(self._subscribers):
            if not queue.full():
                queue.put_nowait(item)

    def list_devices(self) -> list[dict[str, Any]]:
        connected = {
            str(item["device_id"]): {**item, "connected": True, "cached": False}
            for item in (self.hub.list_devices() if self.hub else [])
        }
        cached = {str(item["device_id"]): item for item in self.store.cached_devices()}
        for device_id, item in connected.items():
            old = cached.get(device_id, {})
            if old.get("alias"):
                item["alias"] = old["alias"]
            elif item.get("suggested_alias"):
                item["alias"] = item["suggested_alias"]
            self.store.remember_device(item)
            cached[device_id] = item
        for device_id, item in cached.items():
            if device_id not in connected:
                item["connected"] = False
        result = sorted(
            cached.values(), key=lambda item: str(item.get("alias") or item["device_id"])
        )
        self.metrics.gauge("devices.connected", len(connected))
        self.metrics.gauge("devices.known", len(result))
        workers = active_workers()
        return [boot_id_text({**item, **describe_device(self, item, workers=workers)}) for item in result]

    def list_endpoints(self) -> list[dict[str, Any]]:
        endpoints = {item["endpoint"]: dict(item) for item in self.device_hub.list_endpoints()}
        if not self.demo:
            for port in discover_iris_usb_devices(include_rom=True):
                if port.vid == 0x303A and port.pid == 0x0020:
                    metadata = {"path": port.path, "device": port.device, "vid": port.vid,
                                "pid": port.pid, "serial_number": port.serial_number, "location": port.location}
                    endpoint = usb_endpoint(metadata)
                    endpoints[endpoint] = {**endpoints.get(endpoint, {}), **metadata,
                                           "endpoint": endpoint, "firmware_mode": "rom", "present": True}
        workers = active_workers()
        result = []
        for item in endpoints.values():
            if self.project is not None:
                item["ownership"] = self.project.registry.claim(item["endpoint"])
            result.append({**item, "connection_state": item.get("state"),
                           **describe_device(self, item, workers=workers)})
        return sorted(result, key=lambda item: item["endpoint"])

    def resolve_device(self, value: str) -> str:
        if any(item.get("device_id") == value for item in self.list_devices()):
            return value
        return self.store.resolve_device(value)

    async def current_status(self, device_id: str) -> dict[str, Any]:
        device_id = self.resolve_device(device_id)
        if self.mode == "observe":
            cached = self.store.get_setting(f"status.{device_id}")
            if not cached:
                raise LookupError("no cached device status is available")
            snapshot = next((item for item in self.list_devices() if item["device_id"] == device_id), {})
            return boot_id_text({**cached, **describe_device(self, snapshot, workers=active_workers()),
                                 "stale": True, "mode": "observe"})
        result = boot_id_text(await self.device_hub.status(device_id))
        result.update(stale=False, mode="develop", queue=self.operations.queue_state(device_id))
        self.store.set_setting(f"status.{device_id}", result)
        self.store.remember_device(result)
        return {**result, **describe_device(self, {**result, "connected": True}, workers=active_workers())}

    async def memory_snapshot(self, device_id: str) -> dict[str, Any]:
        return await sample_memory(self, device_id)

    async def preserve_coredump(self, device_id: str) -> dict[str, Any] | None:
        report = await self.device_hub.crash_report(device_id)
        if not report.get("core_dump_present") or not report.get("core_dump_valid"):
            return None
        total = int(report["core_dump_size"])
        data = bytearray()
        while len(data) < total:
            returned_total, chunk = await self.device_hub.read_core_dump_chunk(
                device_id, len(data), min(int(report.get("core_dump_chunk_max", 1024)), total - len(data))
            )
            if returned_total != total or not chunk:
                raise RuntimeError("coredump preservation stopped before completion")
            data.extend(chunk)
        path = self.store.save_artifact(device_id, "coredump", bytes(data), "bin")
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    async def closed_loop_ota(
        self,
        device_id: str,
        image: bytes,
        metadata: dict[str, Any],
        operation_id: str,
        execution_mode: str = "recovery",
        validation_mode: str = DEFAULT_OTA_VALIDATION_MODE,
        compatibility: dict[str, Any] | None = None,
        preconditions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if execution_mode not in {"recovery", "application"}:
            raise ValueError("OTA execution mode must be recovery or application")
        validation_mode = _require_ota_validation_mode(validation_mode)
        before = await self.device_hub.status(device_id)
        required_compatibility = validate_update_compatibility(
            before, metadata.get("chip_id"), compatibility
        )
        timeout = health_timeout(before, getattr(self, "ota_health_timeout", None))
        if (
            before.get("ota_project_name_match_required", False)
            and before.get("project_name")
            and before["project_name"] != metadata["project_name"]
        ):
            raise ValueError(
                f"firmware project {metadata['project_name']} does not match device project {before['project_name']}"
            )
        previous_boot = before.get("boot_id")
        recovery_boot = None
        writer_status = before
        if execution_mode == "recovery":
            async def transition_progress(stage: str, **fields: Any) -> None:
                await self.operations.progress(operation_id, stage=stage, progress_permille={
                    "entering_recovery": 25, "waiting_recovery": 50, "recovery_connected": 75,
                }[stage], **fields)

            writer_status = await enter_recovery(self.device_hub, device_id, before=before,
                                                 progress=transition_progress)
            if before.get("firmware_mode") != "recovery":
                recovery_boot = writer_status["boot_id"]

        validate_update_compatibility(
            writer_status, metadata.get("chip_id"), required_compatibility,
            recovery=execution_mode == "recovery",
        )
        recovery_evidence = await validate_recovery(self.device_hub, device_id, writer_status, preconditions or {})
        last_device_progress = -10

        async def report_progress(progress: dict[str, Any]) -> None:
            nonlocal last_device_progress
            device_progress = int(progress.get("progress_permille", 0))
            stage = str(progress.get("stage", "transferring"))
            if (
                stage == "transferring"
                and device_progress < 900
                and device_progress < last_device_progress + 10
            ):
                return
            last_device_progress = device_progress
            overall = 100 + (device_progress * 750 // 1000) if execution_mode == "recovery" else device_progress * 850 // 1000
            await self.operations.progress(
                operation_id,
                stage=stage,
                progress_permille=overall,
                device_progress_permille=device_progress,
                job_id=progress.get("job_id"),
                bytes_received=progress.get("bytes_received", 0),
                bytes_total=progress.get("bytes_total", len(image)),
                partition=progress.get("partition", ""),
            )

        queue = (
            None
            if getattr(self, "demo", False)
            else self.device_hub.subscribe(device_id)
        )
        writer_boot = recovery_boot if recovery_boot is not None else previous_boot
        try:
            await self.operations.progress(
                operation_id,
                stage="preparing_ota",
                progress_permille=90 if execution_mode == "recovery" else 0,
                writer_boot_id=writer_boot,
                recovery=recovery_evidence,
            )
            result = await self.device_hub.ota_update(
                device_id,
                image,
                expected_sha256=bytes.fromhex(metadata["sha256"]),
                project_name=metadata["project_name"],
                version=metadata["version"],
                progress_callback=report_progress,
            )
            await self.operations.progress(
                operation_id,
                stage="waiting_device",
                progress_permille=900,
                job_id=result.get("job_id"),
            )
            if result.get("healthy"):
                status = await self.device_hub.status(device_id)
                if status.get("boot_id") is None or status.get("boot_id") == writer_boot:
                    raise OperationOutcomeUnknown("OTA HEALTHY result has no confirmed new boot")
                if result.get("boot_id") is not None and result["boot_id"] != status["boot_id"]:
                    raise OperationOutcomeUnknown("OTA boot changed after HEALTHY result")
                validate_updated_contract(status, device_id, metadata.get("chip_id"), required_compatibility)
                validation = _validate_ota_identity(
                    status, metadata, validation_mode
                )
                return {
                    **result,
                    "validated_image": metadata,
                    "execution_mode": execution_mode,
                    "validation": validation,
                    "recovery_boot_id": recovery_boot,
                    "recovery": recovery_evidence,
                }
            assert queue is not None
            if result.get("completion_evidence") == "session_close":
                delay = None
            else:
                try:
                    delay = await self.device_hub.restart(device_id, 250)
                except (ConnectionError, OSError, KeyError):
                    # The writer may restart immediately after END_RESPONSE.
                    # Reconcile that same-device reboot below instead of
                    # turning the expected USB re-enumeration into a failure.
                    delay = None
            await self.operations.progress(
                operation_id,
                stage="reconnecting",
                progress_permille=925,
                writer_boot_id=writer_boot,
            )
            deadline = asyncio.get_running_loop().time() + timeout
            new_boot: Any = None
            reconnected_status: dict[str, Any] | None = None
            while asyncio.get_running_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), min(1.0, max(0.001, deadline - asyncio.get_running_loop().time()))
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                if event.get("boot_id") != writer_boot and event.get("boot_id") is not None:
                    new_boot = event["boot_id"]
                if event.get("event_name") == "healthy" and event.get("boot_id") == new_boot:
                    candidate_status = await self.device_hub.status(device_id)
                    if candidate_status.get("boot_id") == new_boot:
                        reconnected_status = candidate_status
                        break
                    # A second reboot can race the status read after the first
                    # HEALTHY event. Keep observing and require the later boot
                    # to publish its own HEALTHY event before accepting it.
                    if (
                        candidate_status.get("boot_id") != writer_boot
                        and candidate_status.get("boot_id") is not None
                    ):
                        new_boot = candidate_status["boot_id"]
            if new_boot is None or reconnected_status is None:
                raise OperationOutcomeUnknown(
                    f"OTA was written, but the final reconnect/healthy acceptance was not observed within {timeout:g} seconds"
                )
            validate_updated_contract(reconnected_status, device_id, metadata.get("chip_id"), required_compatibility)
            validation = _validate_ota_identity(reconnected_status, metadata, validation_mode)
            return {
                **result,
                "validated_image": metadata,
                "execution_mode": execution_mode,
                "validation": validation,
                "planned_restart_ms": delay,
                "previous_boot_id": previous_boot,
                "recovery_boot_id": recovery_boot,
                "recovery": recovery_evidence,
                "boot_id": new_boot,
                "healthy": True,
            }
        except (ConnectionError, OSError, KeyError, LookupError) as exc:
            raise OperationOutcomeUnknown(
                "OTA write or post-write observation lost contact; reconcile before another write"
            ) from exc
        finally:
            if queue is not None:
                self.device_hub.unsubscribe(device_id, queue)

    async def closed_loop_system_update(
        self,
        device_id: str,
        bundle: SystemUpdateBundle,
        operation_id: str,
        compatibility: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await run_system_update(
            self.device_hub,
            self.operations,
            self.preserve_coredump,
            _validate_ota_identity,
            device_id,
            bundle,
            operation_id,
            validation_mode=DEFAULT_OTA_VALIDATION_MODE,
            health_timeout_override=getattr(self, "ota_health_timeout", None),
            compatibility=compatibility,
        )

    async def closed_loop_restart(
        self, device_id: str, delay_ms: int, operation_id: str
    ) -> dict[str, Any]:
        before = await self.device_hub.status(device_id)
        previous_boot = before.get("boot_id")
        if self.demo:
            accepted_delay = await self.device_hub.restart(device_id, delay_ms)
            await self.operations.stage(operation_id, "reconnecting")
            after = await self.device_hub.status(device_id)
            return {
                "accepted": True,
                "delay_ms": accepted_delay,
                "previous_boot_id": previous_boot,
                "boot_id": after.get("boot_id"),
                "reconnected": after.get("boot_id") != previous_boot,
                "demo": True,
            }
        queue = self.device_hub.subscribe(device_id)
        try:
            accepted_delay = await self.device_hub.restart(device_id, delay_ms)
            await self.operations.stage(operation_id, "reconnecting")
            deadline = asyncio.get_running_loop().time() + 30
            while asyncio.get_running_loop().time() < deadline:
                try:
                    event = await asyncio.wait_for(queue.get(), 1.0)
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                boot_id = event.get("boot_id")
                if (
                    event.get("kind") == "connection"
                    and event.get("connection_state") == "rebooted"
                    and boot_id is not None
                    and boot_id != previous_boot
                ):
                    status = await self.device_hub.status(device_id)
                    return {
                        "accepted": True,
                        "delay_ms": accepted_delay,
                        "previous_boot_id": previous_boot,
                        "boot_id": boot_id,
                        "reconnected": True,
                        "app_version": status.get("app_version"),
                    }
            raise OperationOutcomeUnknown(
                "restart was accepted but same-device reconnect was not observed within 30 seconds"
            )
        finally:
            self.device_hub.unsubscribe(device_id, queue)


def create_app(service: GatewayService) -> web.Application:
    @web.middleware
    async def errors(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except json.JSONDecodeError as exc:
            return _error(400, "invalid_json", str(exc))
        except (TypeError, ValueError) as exc:
            return _error(400, "invalid_request", str(exc))
        except FileServiceError as exc:
            status = file_error_http_status(exc.status)
            return _error(status, f"file_{exc.status.name.lower()}", str(exc))
        except KeyError as exc:
            return _error(404, "not_found", str(exc))
        except LookupError as exc:
            return _error(503, "cached_state_unavailable", str(exc))
        except PermissionError as exc:
            return _error(403, "permission_denied", str(exc))
        except OperationConflict as exc:
            return _error(409, "operation_id_conflict", str(exc))
        except DeviceBusy as exc:
            return _error(409, "device_busy", str(exc))
        except OperationCancelled as exc:
            return _error(409, "operation_cancelled", str(exc))
        except OperationOutcomeUnknown as exc:
            return _error(409, "outcome_unknown", str(exc))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            return _error(504, "device_timeout", str(exc) or "device request timed out")
        except (RuntimeError, ConnectionError) as exc:
            return _error(409, "device_request_failed", str(exc))

    @web.middleware
    async def authentication(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if request.method == "OPTIONS" or request.path in PUBLIC_API or not request.path.startswith("/v1"):
            return await handler(request)
        if not service.authentication_required(request):
            token = ACTOR_CONTEXT.set(Actor("local", "Unauthenticated local client"))
            try:
                return await handler(request)
            finally:
                ACTOR_CONTEXT.reset(token)
        authorization = request.headers.get("Authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
        actor = service.auth.authenticate_bearer(bearer)
        if actor is None:
            actor = service.auth.browser_actor(request.cookies.get(AuthManager.COOKIE_NAME))
        if actor is None:
            return _error(401, "authentication_required", "developer password login or agent token required")
        token = ACTOR_CONTEXT.set(actor)
        try:
            return await handler(request)
        finally:
            ACTOR_CONTEXT.reset(token)

    middlewares = [errors, authentication]
    if service.project is not None:
        middlewares.append(service.project.guard)
    app = web.Application(middlewares=middlewares, client_max_size=GATEWAY_CLIENT_MAX_SIZE)
    async def health(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "instance_id": service.instance_id,
                "host_id": service.host_id,
                "gateway_api": GATEWAY_API,
                "capabilities": GATEWAY_CAPABILITIES + ([service.project.capability] if service.project else [])
                + ([LIFECYCLE_CAPABILITY] if service.project and service.project.shared else []),
                "lifecycle": service.project.clients.snapshot(service.project.keepalive_reasons())
                if service.project and service.project.shared else None,
                "project_session": (
                    service.project.registry.session(service.project.registry.session_id)
                    if service.project is not None else None
                ),
                "esp_iris_version": "0.1.0",
                "source": service.source_identity,
                "esp_iris_revision": os.environ.get(
                    "ESP_IRIS_SOURCE_REVISION", "unknown"
                ),
                "mode": service.mode,
                "demo": service.demo,
                "authenticated_api": service.authentication_required(request),
                "local_authentication_required": service.require_local_auth,
                "database_schema_version": service.store.schema_version,
                "ready": service.hub is not None,
                "system_update_trust_configured": (
                    service.system_update_trust_key is not None
                ),
                "time_ns": time.time_ns(),
            }
        )

    async def metrics(request: web.Request) -> web.Response:
        del request
        return web.json_response(service.metrics.snapshot())

    async def auth_state(request: web.Request) -> web.Response:
        required = service.authentication_required(request)
        if not required:
            return web.json_response(
                {
                    "required": False,
                    "configured": service.auth.configured,
                    "authenticated": True,
                    "actor": Actor(
                        "local", "Unauthenticated local client"
                    ).as_dict(),
                }
            )
        authorization = request.headers.get("Authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
        actor = service.auth.authenticate_bearer(bearer) or service.auth.browser_actor(
            request.cookies.get(AuthManager.COOKIE_NAME)
        )
        return web.json_response(
            {"required": True, "configured": service.auth.configured, "authenticated": actor is not None, "actor": actor.as_dict() if actor else None}
        )

    async def setup(request: web.Request) -> web.Response:
        if service.auth.configured:
            return _error(409, "already_configured", "developer password is already configured")
        body = await _json_body(request)
        service.auth.set_initial_password(str(body.get("password", "")), "web-setup")
        token = service.auth.login(str(body["password"]))
        response = web.json_response({"configured": True, "authenticated": True})
        response.set_cookie(
            AuthManager.COOKIE_NAME,
            token,
            httponly=True,
            secure=request.secure,
            samesite="Strict",
            path="/",
        )
        return response

    async def login(request: web.Request) -> web.Response:
        body = await _json_body(request)
        try:
            token = service.auth.login(str(body.get("password", "")))
        except PermissionError:
            return _error(401, "invalid_password", "developer password is incorrect")
        response = web.json_response({"authenticated": True, "actor": {"type": "developer", "name": "Developer"}})
        response.set_cookie(
            AuthManager.COOKIE_NAME,
            token,
            httponly=True,
            secure=request.secure,
            samesite="Strict",
            path="/",
        )
        return response

    async def logout(request: web.Request) -> web.Response:
        service.auth.logout(request.cookies.get(AuthManager.COOKIE_NAME))
        response = web.json_response({"authenticated": False})
        response.del_cookie(AuthManager.COOKIE_NAME, path="/")
        return response

    async def mode(request: web.Request) -> web.Response:
        if request.method == "GET":
            return web.json_response(service.mode_state())
        body = await _json_body(request)
        return web.json_response(await service.switch_mode(str(body.get("mode", "")), _actor(request)))

    async def devices(request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {"instance_id": service.instance_id, "mode": service.mode, "demo": service.demo, "devices": service.list_devices()}
        )

    async def endpoints(request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {"endpoints": service.list_endpoints(), "demo": service.demo}
        )

    async def status(request: web.Request) -> web.Response:
        return web.json_response(await service.current_status(request.match_info["device_id"]))

    async def memory_snapshot(request: web.Request) -> web.Response:
        try:
            snapshot = await service.memory_snapshot(request.match_info["device_id"])
        except NotImplementedError as exc:
            return _error(501, "not_supported", str(exc))
        return web.json_response(snapshot)

    async def alias(request: web.Request) -> web.Response:
        device_id = service.resolve_device(request.match_info["device_id"])
        body = await _json_body(request)
        value = str(body.get("alias", "")).strip() or None
        service.store.set_alias(device_id, value)
        audit = service.store.add_audit(_actor(request).kind, _actor(request).name, "device.alias_changed", {"device_id": device_id, "alias": value})
        await service._broadcast_system(audit)
        return web.json_response({"device_id": device_id, "alias": value})

    async def remove_device(request: web.Request) -> web.Response:
        device_id = service.resolve_device(request.match_info["device_id"])
        connected_ids = {
            str(item["device_id"])
            for item in (service.hub.list_devices() if service.hub else [])
        }
        current = next((item for item in service.list_devices() if item["device_id"] == device_id), {})
        if device_id in connected_ids or current.get("state") != "offline":
            return _error(
                409,
                "device_connected",
                "connected devices cannot be removed; disconnect the device first",
            )
        if not service.store.remove_device(device_id):
            raise KeyError(f"unknown device: {device_id}")
        audit = service.store.add_audit(
            _actor(request).kind,
            _actor(request).name,
            "device.removed",
            {"device_id": device_id, "history_preserved": True},
        )
        await service._broadcast_system(audit)
        return web.json_response(
            {"device_id": device_id, "removed": True, "history_preserved": True}
        )

    async def event_history(request: web.Request) -> web.Response:
        cursor = int(request.query.get("cursor", "0"))
        categories = [item for item in request.query.get("categories", "").split(",") if item]
        device_id = request.query.get("device_id")
        if cursor:
            items, gap = service.store.events_after(
                cursor, device_id=device_id, categories=categories, limit=3000
            )
        else:
            items = service.store.latest_events(
                device_id=device_id, categories=categories, limit=3000
            )
            gap = False
        return web.json_response({"events": items, "history_gap": gap, "next_cursor": items[-1]["event_id"] if items else cursor})

    async def event_socket(request: web.Request) -> web.StreamResponse:
        cursor = int(request.query.get("cursor", "0"))
        device_id = request.query.get("device_id")
        lease = None
        if service.project and service.project.shared:
            lease = service.project.clients.register({
                "kind": "workbench" if request.query.get("client") == "workbench" else "cli",
                "command": "Web Workbench" if request.query.get("client") == "workbench" else "event stream",
            }, connected=True)
        queue = service.subscribe()
        # Observe PONG frames for accurate last-seen diagnostics. aiohttp still
        # owns heartbeat timeouts; project sockets reply to PING explicitly.
        websocket = web.WebSocketResponse(heartbeat=5 if lease else 20, autoping=not bool(lease))
        last_event_id = cursor

        async def forward() -> None:
            nonlocal last_event_id
            while True:
                item = await queue.get()
                if device_id and item.get("device_id") != device_id:
                    continue
                event_id = int(item.get("event_id") or 0)
                if event_id and event_id <= last_event_id:
                    continue
                await websocket.send_json(item)
                last_event_id = max(last_event_id, event_id)

        task = None
        try:
            await websocket.prepare(request)
            if cursor:
                history, gap = service.store.events_after(cursor, device_id=device_id, limit=3000)
            else:
                history, gap = service.store.latest_events(device_id=device_id, limit=3000), False
            if gap:
                await websocket.send_json({"kind": "history_gap", "reason": "requested cursor is outside retention"})
            for item in history:
                await websocket.send_json(item)
            last_event_id = history[-1]["event_id"] if history else cursor
            if lease:
                await websocket.send_json({"kind": "project_client", "client_id": lease["client_id"],
                                           "session_id": service.project.registry.session_id})
            task = asyncio.create_task(forward())
            async for message in websocket:
                if message.type == WSMsgType.ERROR:
                    break
                if message.type == WSMsgType.PING:
                    await websocket.pong(message.data)
                if lease:
                    service.project.clients.renew(lease["client_id"], lease["lease_token"])
        finally:
            try:
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            finally:
                # Request cancellation and a failed forwarder must both release
                # the connected reference, which has no lease expiry fallback.
                service.unsubscribe(queue)
                if lease:
                    service.project.clients.release(lease["client_id"], lease["lease_token"])
        return websocket

    async def operation_list(request: web.Request) -> web.Response:
        return web.json_response({"operations": service.store.operations(request.query.get("device_id"))})

    async def operation_get(request: web.Request) -> web.Response:
        item = service.store.operation(request.match_info["operation_id"])
        if item is None:
            raise KeyError(request.match_info["operation_id"])
        return web.json_response(item)

    async def operation_reconcile(request: web.Request) -> web.Response:
        item = await reconcile_operation(service.store, service.device_hub,
                                         request.match_info["operation_id"], _actor(request))
        return web.json_response(item, status=201)

    async def operation_reconciliations(request: web.Request) -> web.Response:
        return web.json_response({"reconciliations": reconciliation_records(
            service.store, request.match_info["operation_id"])})

    async def audits(request: web.Request) -> web.Response:
        del request
        return web.json_response({"audits": service.store.audits()})

    async def rpc_catalog(request: web.Request) -> web.Response:
        del request
        return web.json_response(service.rpc_catalog)

    async def raw_rpc(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        device_id = service.resolve_device(request.match_info["device_id"])
        body = await _json_body(request)
        if "payload_base64" in body:
            payload = base64.b64decode(str(body["payload_base64"]), validate=True)
        elif "payload_hex" in body:
            payload = bytes.fromhex(str(body["payload_hex"]))
        else:
            payload = str(body.get("payload_text", "")).encode()
        service_id = int(body.get("service_id", 0))
        method_id = int(body.get("method_id", 0))
        deadline_ms = int(body.get("deadline_ms", 1000))
        operation, result, created = await service.operations.execute(
            device_id,
            _actor(request),
            "rpc.raw",
            {"service_id": service_id, "method_id": method_id, "request_bytes": len(payload),
             "request_sha256": hashlib.sha256(payload).hexdigest(), "deadline_ms": deadline_ms},
            lambda: service.device_hub.rpc(
                device_id,
                service_id,
                method_id,
                payload,
                deadline_ms=deadline_ms,
            ),
            operation_id=request.headers.get("X-Operation-ID"),
        )
        if not created:
            return web.json_response(
                {"operation": operation, "idempotent_reuse": True, "payload_base64": None}
            )
        return web.json_response(
            {"operation": operation, "payload_base64": base64.b64encode(result or b"").decode(), "response_bytes": len(result or b"")}
        )

    async def structured_rpc(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        name = request.match_info["method"]
        method = next((item for item in service.rpc_catalog.get("methods", []) if item.get("name") == name), None)
        if method is None:
            raise KeyError(f"unknown RPC catalog method: {name}")
        if method.get("request_encoding", "json") != "json":
            raise ValueError(f"RPC method {name} requires its dedicated endpoint")
        body = await _json_body(request)
        raw = json.dumps(body.get("params", {}), separators=(",", ":")).encode()
        compatibility = method.get("compatibility", {})
        maximum = int(compatibility.get("max_request_bytes", 65536))
        if len(raw) > maximum:
            raise ValueError(f"RPC request exceeds catalog maximum of {maximum} bytes")
        device_id = service.resolve_device(request.match_info["device_id"])
        deadline_ms = int(body.get("deadline_ms", method.get("timeout_ms", 1000)))
        operation, result, created = await service.operations.execute(
            device_id,
            _actor(request),
            f"rpc.{name}",
            {"method": name, "service_id": int(method["service_id"]), "method_id": int(method["method_id"]),
             "request_bytes": len(raw), "request_sha256": hashlib.sha256(raw).hexdigest(), "deadline_ms": deadline_ms},
            lambda: service.device_hub.rpc(
                device_id,
                int(method["service_id"]),
                int(method["method_id"]),
                raw,
                deadline_ms=deadline_ms,
            ),
            operation_id=request.headers.get("X-Operation-ID"),
        )
        if not created:
            return web.json_response(
                {"operation": operation, "method": name, "idempotent_reuse": True, "payload_base64": None}
            )
        return web.json_response(
            {
                "operation": operation,
                "method": name,
                "payload_base64": base64.b64encode(result or b"").decode(),
                "response_bytes": len(result or b""),
            }
        )

    async def console(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        body = await _json_body(request)
        line = body.get("line")
        if not isinstance(line, str):
            raise TypeError("console line must be a string")
        line = line.strip()
        encoded = line.encode("utf-8")
        if (
            not encoded
            or len(encoded) > CONSOLE_LINE_MAX_BYTES
            or "\x00" in line
            or "\r" in line
            or "\n" in line
        ):
            raise ValueError(
                f"console line must contain 1 to {CONSOLE_LINE_MAX_BYTES} UTF-8 bytes without CR, LF, or NUL"
            )
        method = next(
            (
                item
                for item in service.rpc_catalog.get("methods", [])
                if item.get("name") == CONSOLE_METHOD_NAME
            ),
            None,
        )
        if method is None:
            raise RuntimeError("console RPC is missing from the catalog")
        device_id = service.resolve_device(request.match_info["device_id"])
        deadline_ms = int(method.get("timeout_ms", 1000))
        command_name = line.split(maxsplit=1)[0]
        operation, result, created = await service.operations.execute(
            device_id,
            _actor(request),
            CONSOLE_METHOD_NAME,
            {
                "command": command_name,
                "service_id": int(method["service_id"]),
                "method_id": int(method["method_id"]),
                "request_sha256": hashlib.sha256(encoded).hexdigest(),
                "request_bytes": len(encoded),
                "deadline_ms": deadline_ms,
            },
            lambda: service.device_hub.rpc(
                device_id,
                int(method["service_id"]),
                int(method["method_id"]),
                encoded,
                deadline_ms=deadline_ms,
            ),
            operation_id=request.headers.get("X-Operation-ID"),
        )
        if not created:
            return web.json_response(
                {"operation": operation, "idempotent_reuse": True, "console": None}
            )
        if result is None or len(result) != 4:
            raise RuntimeError("console RPC returned an invalid job response")
        return web.json_response(
            {
                "operation": operation,
                "console": {
                    "job_id": struct.unpack("<I", result)[0],
                    "accepted": True,
                },
            }
        )

    async def job(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        device_id = service.resolve_device(request.match_info["device_id"])
        job_id = int(request.match_info["job_id"], 0)
        cancel = request.method == "DELETE"
        operation, result, _ = await service.operations.execute(
            device_id,
            _actor(request),
            "job.cancel" if cancel else "job.query",
            {"job_id": job_id},
            lambda: service.device_hub.job(device_id, job_id, cancel=cancel),
            operation_id=request.headers.get("X-Operation-ID"),
            serialized=cancel,
        )
        service.observe_device_activity({"kind": "job", "device_id": device_id, **result})
        return web.json_response({"operation": operation, "job": result})

    async def restart(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        body = await _json_body(request)
        device_id = service.resolve_device(request.match_info["device_id"])
        delay = int(body.get("delay_ms", 250))
        operation_id = request.headers.get("X-Operation-ID") or str(uuid.uuid4())
        operation, result, _ = await service.operations.execute(
            device_id,
            _actor(request),
            "device.restart",
            {"delay_ms": delay},
            lambda: service.closed_loop_restart(device_id, delay, operation_id),
            operation_id=operation_id,
        )
        return web.json_response({"operation": operation, "restart": result})

    async def factory_recovery(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        device_id = service.resolve_device(request.match_info["device_id"])
        if not hasattr(service.device_hub, "enter_recovery"):
            raise RuntimeError("factory recovery is not supported by this device adapter")
        body = await _json_body(request)
        wait = body.get("wait", False)
        timeout = body.get("timeout", 30)
        if not isinstance(wait, bool):
            raise TypeError("wait must be a boolean")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.1 <= timeout <= 30:
            raise ValueError("timeout must be between 0.1 and 30 seconds")
        operation, result, _ = await service.operations.execute(
            device_id,
            _actor(request),
            "recovery.enter_factory",
            {"target": "factory_recovery", "wait": wait, "timeout": timeout},
            lambda: enter_recovery(service.device_hub, device_id, timeout=timeout)
            if wait else service.device_hub.enter_recovery(device_id),
            operation_id=request.headers.get("X-Operation-ID"),
        )
        return web.json_response({"operation": operation, "recovery": result})

    async def screenshot(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        body = await _json_body(request)
        device_id = service.resolve_device(request.match_info["device_id"])
        operation, result, created = await service.operations.execute(
            device_id,
            _actor(request),
            "media.screenshot",
            {"description": body, "save": request.query.get("save") == "true"},
            lambda: service.device_hub.screenshot(device_id, body),
            operation_id=request.headers.get("X-Operation-ID"),
            serialized=False,
            result_summary=lambda value: {"description": value[0], "bytes": len(value[1])},
        )
        if not created:
            return web.json_response({"operation": operation, "idempotent_reuse": True,
                                      "screenshot": None})
        description, data = result
        image = encode_media_image(description, data)
        if len(image.data) > 16 * 1024 * 1024:
            raise ValueError("screenshot exceeds the 16 MiB limit")
        saved = None
        if request.query.get("save") == "true":
            saved = str(service.store.save_artifact(
                device_id, "screenshot", image.data, image.extension
            ))
        return web.Response(
            body=image.data,
            content_type=image.content_type,
            headers={"X-Operation-ID": operation["operation_id"], "X-ESP-Iris-Media": json.dumps(image.description), "X-Saved-Artifact": saved or ""},
        )

    async def mirror(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        body = await _json_body(request)
        device_id = service.resolve_device(request.match_info["device_id"])
        names = {"screen": 3, "image": 4, "audio": 5}
        channel_name = str(body.get("channel", "screen"))
        if channel_name not in names:
            raise ValueError("channel must be screen, image, or audio")
        channel = names[channel_name]
        start = request.path.endswith("/start")
        fps = int(body.get("fps", 5))

        async def call() -> Any:
            if start:
                return await service.device_hub.mirror_start(
                    device_id,
                    channel,
                    body.get("description", {}),
                    fps=fps,
                )
            await service.device_hub.mirror_stop(device_id, channel)
            return {"channel": channel, "channel_name": channel_name, "state": "stopped"}

        operation, result, _ = await service.operations.execute(
            device_id,
            _actor(request),
            "media.mirror_start" if start else "media.mirror_stop",
            {"channel": channel_name, "fps": fps, "description": body.get("description", {}) if start else {}},
            call,
            operation_id=request.headers.get("X-Operation-ID"),
        )
        return web.json_response({"operation": operation, "mirror": result})

    async def media_stream(request: web.Request) -> web.StreamResponse:
        names = {"screen": 3, "image": 4, "audio": 5}
        channel = names.get(request.match_info["channel"])
        if channel is None:
            raise KeyError("unknown media channel")
        device_id = service.resolve_device(request.match_info["device_id"])
        queue = service.device_hub.subscribe_media(device_id, channel)
        websocket = web.WebSocketResponse(heartbeat=20)
        await websocket.prepare(request)

        async def forward() -> None:
            while True:
                item = await queue.get()
                data = item["data"]
                metadata = json.dumps({key: value for key, value in item.items() if key != "data"}, separators=(",", ":")).encode()
                await websocket.send_bytes(len(metadata).to_bytes(4, "little") + metadata + data)

        task = asyncio.create_task(forward())
        try:
            async for _ in websocket:
                pass
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            service.device_hub.unsubscribe_media(device_id, channel, queue)
        return websocket

    async def input_event(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        body = await _json_body(request)
        moves = body.get("moves", [])
        if not isinstance(moves, list) or len(moves) > 2048:
            raise ValueError("gesture moves must be a list of at most 2048 points")
        device_id = service.resolve_device(request.match_info["device_id"])
        if not hasattr(service.device_hub, "input_event"):
            raise RuntimeError("device input requires an input RPC catalog adapter")
        operation, result, _ = await service.operations.execute(
            device_id,
            _actor(request),
            "input.gesture",
            {"event": body},
            lambda: service.device_hub.input_event(device_id, body),
            operation_id=request.headers.get("X-Operation-ID"),
        )
        return web.json_response({"operation": operation, "input": result})

    async def audio_upload(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        device_id = service.resolve_device(request.match_info["device_id"])
        data = await request.read()
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("audio upload exceeds the 16 MiB limit")
        content_type = request.content_type
        if content_type not in ("audio/wav", "audio/x-wav", "application/octet-stream"):
            raise ValueError("audio upload must be WAV or PCM")
        audio_upload_adapter = getattr(service.device_hub, "audio_upload", None)
        if audio_upload_adapter is None:
            raise RuntimeError("device audio upload requires an audio RPC catalog adapter")
        operation, result, _ = await service.operations.execute(
            device_id,
            _actor(request),
            "audio.upload",
            {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "content_type": content_type},
            lambda: audio_upload_adapter(device_id, data, content_type),
            operation_id=request.headers.get("X-Operation-ID"),
        )
        return web.json_response({"operation": operation, "audio": result})

    async def firmware_artifacts(request: web.Request) -> web.Response:
        if request.method == "GET":
            return web.json_response({"artifacts": service.store.firmware_artifacts()})
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        reader = await request.multipart()
        parts: dict[str, bytes] = {}
        async for part in reader:
            if not isinstance(part, BodyPartReader):
                continue
            if part.name not in {"bin", "elf", "map"}:
                continue
            data = await part.read(decode=False)
            if not data:
                raise ValueError(f"firmware artifact field {part.name} is empty")
            parts[str(part.name)] = data
        missing = {"bin", "elf", "map"} - parts.keys()
        if missing:
            raise ValueError(
                "firmware artifact requires bin, elf and map fields; missing "
                + ", ".join(sorted(missing))
            )
        metadata = inspect_firmware_image(parts["bin"]).as_dict()
        actual_elf_sha = hashlib.sha256(parts["elf"]).hexdigest()
        embedded_elf_sha = str(metadata.get("elf_sha256", ""))
        if not embedded_elf_sha or embedded_elf_sha == "00" * 32:
            raise ValueError("firmware binary does not contain a usable ELF SHA-256")
        if embedded_elf_sha != actual_elf_sha:
            raise ValueError("firmware binary ELF SHA-256 does not match uploaded ELF")
        artifact = service.store.save_firmware_artifact(
            binary=parts["bin"],
            elf=parts["elf"],
            map_data=parts["map"],
            metadata=metadata,
        )
        return web.json_response({"artifact": artifact}, status=201)

    async def firmware_artifact(request: web.Request) -> web.Response:
        artifact = service.store.firmware_artifact(request.match_info["artifact_id"])
        if artifact is None:
            raise KeyError("unknown firmware artifact")
        return web.json_response({"artifact": artifact})

    async def ota(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        device_id = service.resolve_device(request.match_info["device_id"])
        execution_mode = request.query.get("execution_mode", "recovery")
        if request.content_type != "application/json":
            raise ValueError(
                "OTA requires an archived BIN/ELF/map artifact_id; upload the bundle first"
            )
        body = await _json_body(request)
        artifact_id = str(body.get("artifact_id", ""))
        compatibility = compatibility_expectation(body.get("compatibility"))
        preconditions = recovery_preconditions(body.get("preconditions"))
        execution_mode = str(body.get("execution_mode", execution_mode))
        validation_mode = _require_ota_validation_mode(
            str(body.get("validation_mode", DEFAULT_OTA_VALIDATION_MODE))
        )
        artifact = service.store.firmware_artifact(artifact_id)
        if artifact is None:
            raise KeyError("unknown firmware artifact")
        image = (pathlib.Path(artifact["path"]) / "firmware.bin").read_bytes()
        metadata = inspect_firmware_image(image).as_dict()
        if metadata["sha256"] != artifact["binary_sha256"]:
            raise ValueError("archived firmware binary checksum changed on disk")
        operation_id = request.headers.get("X-Operation-ID") or str(uuid.uuid4())
        operation, created = await service.operations.submit(
            device_id,
            _actor(request),
            "firmware.ota",
            {
                "image": metadata,
                "artifact_id": artifact_id,
                "execution_mode": execution_mode,
                "validation_mode": validation_mode,
                "compatibility": compatibility,
                "preconditions": preconditions,
            },
            lambda: service.closed_loop_ota(
                device_id,
                image,
                metadata,
                operation_id,
                execution_mode=execution_mode,
                validation_mode=validation_mode,
                compatibility=compatibility,
                preconditions=preconditions,
            ),
            operation_id=operation_id,
        )
        return web.json_response(
            {
                "operation": operation,
                "accepted": created,
                "status_url": f"/v1/operations/{operation_id}",
            },
            status=202,
        )

    async def system_inventory(request: web.Request) -> web.Response:
        device_id = service.resolve_device(request.match_info["device_id"])
        inventory = await service.device_hub.system_update_inventory(device_id)
        return web.json_response({"device_id": device_id, "inventory": inventory})

    async def system_update(request: web.Request) -> web.Response:
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        if request.content_type not in {
            "application/zip",
            "application/octet-stream",
            "application/vnd.esp-iris.system-update+zip",
        }:
            raise ValueError("system update requires an .irisfw archive")
        device_id = service.resolve_device(request.match_info["device_id"])
        archive = await request.read()
        compatibility = compatibility_expectation(json.loads(
            request.headers.get("X-Iris-Compatibility", "{}")
        ))
        bundle = load_system_update_bundle(
            archive, trusted_public_key=service.system_update_trust_key
        )
        service.store.save_artifact(device_id, "system-update", archive, "irisfw")
        operation_id = request.headers.get("X-Operation-ID") or str(uuid.uuid4())
        operation, created = await service.operations.submit(
            device_id,
            _actor(request),
            "firmware.system_update",
            {
                "bundle": bundle.as_dict(),
                "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "compatibility": compatibility,
            },
            lambda: service.closed_loop_system_update(
                device_id, bundle, operation_id, compatibility=compatibility
            ),
            operation_id=operation_id,
        )
        return web.json_response(
            {
                "operation": operation,
                "accepted": created,
                "status_url": f"/v1/operations/{operation_id}",
                "bundle": bundle.as_dict(),
            },
            status=202,
        )

    async def tokens(request: web.Request) -> web.Response:
        if request.method == "GET":
            return web.json_response({"tokens": service.auth.list_agent_tokens()})
        body = await _json_body(request)
        created = service.auth.create_agent_token(
            str(body.get("name", "")), _actor(request), body.get("scopes")
        )
        return web.json_response(created, status=201)

    async def revoke_token(request: web.Request) -> web.Response:
        service.auth.revoke_agent_token(request.match_info["token_id"], _actor(request))
        return web.json_response({"revoked": True, "token_id": request.match_info["token_id"]})

    async def change_password(request: web.Request) -> web.Response:
        body = await _json_body(request)
        service.auth.change_password(str(body.get("password", "")), _actor(request))
        return web.json_response({"changed": True, "login_required": True})

    async def export(request: web.Request) -> web.StreamResponse:
        del request
        target = service.store.export_zip()
        return web.FileResponse(target, headers={"Content-Disposition": f'attachment; filename="{target.name}"'})

    async def openapi(request: web.Request) -> web.Response:
        return web.json_response(
            build_openapi(service.authentication_required(request))
        )

    async def spa(request: web.Request) -> web.StreamResponse:
        relative = request.match_info.get("path", "")
        dist = service.frontend_dist
        if dist and relative:
            candidate = (dist / relative).resolve()
            try:
                candidate.relative_to(dist.resolve())
                within_dist = True
            except ValueError:
                within_dist = False
            if within_dist and candidate.is_file():
                cache_control = (
                    "public, max-age=31536000, immutable"
                    if relative.startswith("assets/")
                    else "no-cache, must-revalidate"
                )
                return web.FileResponse(
                    candidate, headers={"Cache-Control": cache_control}
                )
        if dist and (dist / "index.html").exists():
            return web.FileResponse(
                dist / "index.html",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                },
            )
        return web.Response(
            text="<!doctype html><meta charset=utf-8><title>ESP-Iris</title><h1>ESP-Iris frontend is not built</h1><p>Run the frontend build in common_components/esp_iris/tools/frontend.</p>",
            content_type="text/html",
        )

    async def api_not_found(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "error": "not_found",
                "message": f"unknown API route: {request.method} {request.path}",
            },
            status=404,
        )

    app.router.add_get("/v1/health", health)
    app.router.add_get("/v1/metrics", metrics)
    app.router.add_get("/v1/auth/state", auth_state)
    app.router.add_post("/v1/auth/setup", setup)
    app.router.add_post("/v1/auth/login", login)
    app.router.add_post("/v1/auth/logout", logout)
    app.router.add_get("/v1/mode", mode)
    app.router.add_put("/v1/mode", mode)
    app.router.add_get("/v1/devices", devices)
    app.router.add_get("/v1/endpoints", endpoints)
    register_host_routes(app, service)
    app.router.add_get("/v1/devices/{device_id}", status)
    app.router.add_get("/v1/devices/{device_id}/memory", memory_snapshot)
    app.router.add_delete("/v1/devices/{device_id}", remove_device)
    app.router.add_patch("/v1/devices/{device_id}/alias", alias)
    app.router.add_get("/v1/events", event_history)
    app.router.add_get("/v1/events/ws", event_socket)
    app.router.add_get("/v1/operations", operation_list)
    app.router.add_get("/v1/operations/{operation_id}", operation_get)
    app.router.add_post("/v1/operations/{operation_id}/reconcile", operation_reconcile)
    app.router.add_get("/v1/operations/{operation_id}/reconciliations", operation_reconciliations)
    register_file_routes(app, service)
    app.router.add_get("/v1/firmware-artifacts", firmware_artifacts)
    app.router.add_post("/v1/firmware-artifacts", firmware_artifacts)
    app.router.add_get("/v1/firmware-artifacts/{artifact_id}", firmware_artifact)
    app.router.add_get("/v1/system-audit", audits)
    app.router.add_get("/v1/rpc-catalog", rpc_catalog)
    app.router.add_post("/v1/devices/{device_id}/rpc/raw", raw_rpc)
    app.router.add_post("/v1/devices/{device_id}/rpc/{method}", structured_rpc)
    app.router.add_post("/v1/devices/{device_id}/console", console)
    app.router.add_get("/v1/devices/{device_id}/jobs/{job_id}", job)
    app.router.add_delete("/v1/devices/{device_id}/jobs/{job_id}", job)
    app.router.add_post("/v1/devices/{device_id}/restart", restart)
    app.router.add_post(
        "/v1/devices/{device_id}/factory-recovery", factory_recovery
    )
    app.router.add_post("/v1/devices/{device_id}/screenshot", screenshot)
    app.router.add_post("/v1/devices/{device_id}/mirror/start", mirror)
    app.router.add_post("/v1/devices/{device_id}/mirror/stop", mirror)
    app.router.add_get("/v1/devices/{device_id}/streams/{channel}", media_stream)
    app.router.add_post("/v1/devices/{device_id}/input", input_event)
    app.router.add_post("/v1/devices/{device_id}/audio", audio_upload)
    app.router.add_post("/v1/devices/{device_id}/ota", ota)
    app.router.add_get(
        "/v1/devices/{device_id}/system-inventory", system_inventory
    )
    app.router.add_post(
        "/v1/devices/{device_id}/system-update", system_update
    )
    register_crash_routes(app, service)
    app.router.add_get("/v1/auth/tokens", tokens)
    app.router.add_post("/v1/auth/tokens", tokens)
    app.router.add_delete("/v1/auth/tokens/{token_id}", revoke_token)
    app.router.add_put("/v1/auth/password", change_password)
    app.router.add_post("/v1/export", export)
    app.router.add_get("/v1/openapi.json", openapi)
    if service.project is not None:
        service.project.register_routes(app)
    app.router.add_route("*", "/v1/{path:.*}", api_not_found)
    app.router.add_get("/{path:.*}", spa)
    return app


__all__ = [
    "DEFAULT_OTA_VALIDATION_MODE",
    "OTA_VALIDATION_MODES",
    "GatewayService",
    "create_app",
]
