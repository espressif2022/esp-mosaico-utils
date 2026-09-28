from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from .observability import MetricsRegistry
from .operation_identity import request_fingerprint, require_same_request
from .security import Actor
from .state_machine import TERMINAL_OPERATION_STATES, operation_transition
from .store import GatewayStore


class OperationRejected(ValueError):
    """A pre-write failure carrying machine-readable remediation evidence."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


class OperationCancelled(RuntimeError):
    pass


class OperationOutcomeUnknown(RuntimeError):
    pass


class DeviceBusy(RuntimeError):
    """An exclusive operation currently controls this device or endpoint."""


@dataclasses.dataclass
class _Pending:
    operation_id: str
    device_id: str
    cancelled: bool = False
    running: bool = False


def _safe_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, dict):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class OperationManager:
    def __init__(
        self,
        store: GatewayStore,
        event_sink: Callable[[dict[str, Any]], Awaitable[None]],
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.store = store
        self.event_sink = event_sink
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._pending: dict[str, _Pending] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._submission_ids: set[str] = set()
        self._exclusive: dict[str, str] = {}
        self.metrics = metrics or MetricsRegistry()

    @staticmethod
    def _fingerprint(device_id: str, actor: Actor, action: str, params: dict[str, Any]) -> str:
        return request_fingerprint({
            "device_id": device_id, "actor_type": actor.kind, "actor_name": actor.name,
            "actor_scopes": sorted(actor.scopes), "action": action, "params": params,
        })

    def _transition(
        self, operation_id: str, status: str, **changes: Any
    ) -> dict[str, Any]:
        current = self.store.operation(operation_id)
        if current is None:
            raise KeyError(operation_id)
        target = operation_transition(str(current["status"]), status)
        operation = self.store.update_operation(
            operation_id, status=target.value, **changes
        )
        self.metrics.increment(f"operations.transition.{target.value}")
        if target in TERMINAL_OPERATION_STATES:
            started = operation.get("started_ns") or operation.get("created_ns")
            finished = operation.get("finished_ns")
            if started and finished and finished >= started:
                self.metrics.observe(
                    "operations.duration_seconds", (finished - started) / 1_000_000_000
                )
        return operation

    def queue_state(self, device_id: str) -> dict[str, Any]:
        items = [
            item
            for item in self._pending.values()
            if item.device_id == device_id or self._exclusive.get(device_id) == item.operation_id
        ]
        return {
            "device_id": device_id,
            "running": [item.operation_id for item in items if item.running],
            "queued": [
                item.operation_id
                for item in items
                if not item.running and not item.cancelled
            ],
        }

    async def stage(self, operation_id: str, status: str) -> dict[str, Any]:
        if status not in {
            "running",
            "preserving_evidence",
            "entering_recovery",
            "waiting_recovery",
            "recovery_connected",
            "preparing_ota",
            "erasing",
            "validating_plan",
            "transferring",
            "verifying",
            "committing",
            "waiting_device",
            "reconnecting",
        }:
            raise ValueError(f"invalid active operation stage: {status}")
        operation = self._transition(operation_id, status)
        await self._emit(operation)
        return operation

    async def progress(
        self,
        operation_id: str,
        *,
        stage: str,
        progress_permille: int,
        **details: Any,
    ) -> dict[str, Any]:
        progress_permille = max(0, min(int(progress_permille), 1000))
        current = self.store.operation(operation_id)
        previous = (current or {}).get("progress") or {}
        operation = self._transition(
            operation_id,
            stage,
            progress_json={
                **previous,
                "stage": stage,
                "progress_permille": progress_permille,
                "updated_ns": time.time_ns(),
                **_safe_value(details),
            },
        )
        await self._emit(operation)
        return operation

    async def submit(
        self,
        device_id: str,
        actor: Actor,
        action: str,
        params: dict[str, Any],
        call: Callable[[], Awaitable[Any]],
        *,
        operation_id: str | None = None,
        serialized: bool = True,
        result_summary: Callable[[Any], Any] | None = None,
        exclusive_resources: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], bool]:
        """Queue an operation and return immediately while it runs in background."""

        if device_id in self._exclusive and self._exclusive[device_id] != operation_id:
            raise DeviceBusy(f"device {device_id} has exclusive operation {self._exclusive[device_id]}")
        operation_id = operation_id or str(uuid.uuid4())
        fingerprint = self._fingerprint(device_id, actor, action, params)
        existing = self.store.operation(operation_id)
        if existing is not None:
            require_same_request(existing, fingerprint)
            return existing, False
        if operation_id in self._submission_ids:
            for _ in range(10):
                await asyncio.sleep(0)
                existing = self.store.operation(operation_id)
                if existing is not None:
                    require_same_request(existing, fingerprint)
                    return existing, False
            raise RuntimeError("concurrent operation submission was not registered")
        self._submission_ids.add(operation_id)
        task = asyncio.create_task(
            self.execute(
                device_id,
                actor,
                action,
                params,
                call,
                operation_id=operation_id,
                serialized=serialized,
                result_summary=result_summary,
                exclusive_resources=exclusive_resources,
            ),
            name=f"esp-iris-operation-{operation_id}",
        )
        self._tasks.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            self._submission_ids.discard(operation_id)
            # Retrieve the exception so a failed background request does not
            # become an unhandled asyncio task. Its durable operation row is
            # the public result channel.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        await asyncio.sleep(0)
        operation = self.store.operation(operation_id)
        if operation is None:
            raise RuntimeError("background operation was not registered")
        require_same_request(operation, fingerprint)
        if task.done():
            _, _, created = task.result()
            return operation, created
        return operation, True

    async def cancel_queued(self, device_id: str | None = None, *,
                            reason: str = "cancelled when gateway entered observe mode") -> int:
        count = 0
        for pending in tuple(self._pending.values()):
            if device_id is not None and pending.device_id != device_id and self._exclusive.get(device_id) != pending.operation_id:
                continue
            if not pending.running and not pending.cancelled:
                pending.cancelled = True
                count += 1
                operation = self._transition(
                    pending.operation_id,
                    "cancelled",
                    error=reason,
                    finished_ns=time.time_ns(),
                )
                await self._emit(operation)
        return count

    async def close(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def execute(
        self,
        device_id: str,
        actor: Actor,
        action: str,
        params: dict[str, Any],
        call: Callable[[], Awaitable[Any]],
        *,
        operation_id: str | None = None,
        serialized: bool = True,
        result_summary: Callable[[Any], Any] | None = None,
        exclusive_resources: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], Any, bool]:
        if device_id in self._exclusive and self._exclusive[device_id] != operation_id:
            raise DeviceBusy(f"device {device_id} has exclusive operation {self._exclusive[device_id]}")
        operation_id = operation_id or str(uuid.uuid4())
        queue_position = sum(
            1
            for item in self._pending.values()
            if (item.device_id == device_id or self._exclusive.get(device_id) == item.operation_id) and not item.cancelled
        )
        resources = tuple(sorted({device_id, *exclusive_resources}))
        if exclusive_resources and any(key in self._exclusive and self._exclusive[key] != operation_id
                                       for key in resources):
            raise DeviceBusy("another exclusive operation owns a requested resource")
        operation, created = self.store.create_operation(
            {
                "operation_id": operation_id,
                "device_id": device_id,
                "actor_type": actor.kind,
                "actor_name": actor.name,
                "action": action,
                "params": _safe_value(params),
                "request_fingerprint": self._fingerprint(device_id, actor, action, params),
                "status": "queued" if serialized else "running",
                "created_ns": time.time_ns(),
                "queue_position": queue_position,
            }
        )
        if not created:
            return operation, operation.get("result"), False
        pending = _Pending(operation_id, device_id)
        if exclusive_resources:
            self._exclusive.update({key: operation_id for key in resources})
        self._pending[operation_id] = pending
        try:
            await self._emit(operation)
            if serialized:
                async with AsyncExitStack() as locks:
                    for key in resources:
                        await locks.enter_async_context(self._locks[key])
                    if pending.cancelled:
                        raise OperationCancelled(
                            "operation cancelled before it reached the device"
                        )
                    completed, result = await self._run(
                        pending, call, result_summary=result_summary
                    )
                    return completed, result, True
            completed, result = await self._run(
                pending, call, result_summary=result_summary
            )
            return completed, result, True
        finally:
            self._pending.pop(operation_id, None)
            for key in resources:
                if self._exclusive.get(key) == operation_id:
                    del self._exclusive[key]

    async def _run(
        self,
        pending: _Pending,
        call: Callable[[], Awaitable[Any]],
        *,
        result_summary: Callable[[Any], Any] | None,
    ) -> tuple[dict[str, Any], Any]:
        pending.running = True
        operation = self._transition(
            pending.operation_id,
            "running",
            started_ns=time.time_ns(),
            queue_position=0,
        )
        await self._emit(operation)
        try:
            result = await call()
        except asyncio.CancelledError:
            interrupted = self.store.operation(pending.operation_id) or {}
            operation = self._transition(
                pending.operation_id,
                "outcome_unknown" if interrupted.get("action") in {
                    "firmware.ota", "firmware.system_update", "device.restart", "rpc.raw", "host.recovery"
                } else "interrupted",
                error="gateway task was interrupted; device writes were not replayed",
                finished_ns=time.time_ns(),
            )
            await self._emit(operation)
            raise
        except OperationCancelled:
            raise
        except (OperationOutcomeUnknown, asyncio.TimeoutError, TimeoutError) as exc:
            operation = self._transition(
                pending.operation_id,
                "outcome_unknown",
                error=str(exc) or "device outcome could not be established",
                finished_ns=time.time_ns(),
            )
            await self._emit(operation)
            raise
        except Exception as exc:
            operation = self._transition(
                pending.operation_id,
                "failed",
                result_json={"failure": exc.details} if isinstance(exc, OperationRejected) else None,
                error=str(exc),
                finished_ns=time.time_ns(),
            )
            await self._emit(operation)
            raise
        summary = result_summary(result) if result_summary else result
        current = self.store.operation(pending.operation_id)
        progress = current.get("progress") if current else None
        if isinstance(progress, dict):
            progress = {
                **progress,
                "stage": "succeeded",
                "progress_permille": 1000,
                "updated_ns": time.time_ns(),
            }
        operation = self._transition(
            pending.operation_id,
            "succeeded",
            result_json=_safe_value(summary),
            progress_json=progress,
            finished_ns=time.time_ns(),
        )
        await self._emit(operation)
        return operation, result

    async def _emit(self, operation: dict[str, Any]) -> None:
        await self.event_sink(
            {
                "kind": "operation",
                "device_id": operation["device_id"],
                "host_receive_wall_ns": time.time_ns(),
                "operation": operation,
            }
        )


__all__ = ["OperationCancelled", "OperationManager", "OperationOutcomeUnknown"]
