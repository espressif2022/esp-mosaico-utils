from __future__ import annotations

import asyncio

import pytest

from iris_gateway.observability import MetricsRegistry
from iris_gateway.operations import DeviceBusy, OperationManager
from iris_gateway.security import Actor
from iris_gateway.state_machine import StateTransitionError
from iris_gateway.store import GatewayStore


def test_operation_manager_serializes_transitions_and_records_duration(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        metrics = MetricsRegistry()
        events = []

        async def sink(event):
            events.append(event)

        manager = OperationManager(store, sink, metrics)
        operation, result, created = await manager.execute(
            "device-a",
            Actor("agent", "test"),
            "rpc.raw",
            {},
            lambda: asyncio.sleep(0, result={"ok": True}),
            operation_id="op-1",
        )
        assert created and result == {"ok": True}
        assert operation["status"] == "succeeded"
        assert [item["operation"]["status"] for item in events] == [
            "queued",
            "running",
            "succeeded",
        ]
        snapshot = metrics.snapshot()
        assert snapshot["counters"]["operations.transition.succeeded"] == 1
        assert snapshot["distributions"]["operations.duration_seconds"]["count"] == 1
        with pytest.raises(StateTransitionError):
            manager._transition("op-1", "running")
        store.close()

    asyncio.run(scenario())


def test_observe_transition_cancels_queued_operation(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)

        async def sink(event):
            del event

        manager = OperationManager(store, sink)
        store.create_operation(
            {
                "operation_id": "queued",
                "device_id": "device-a",
                "actor_type": "agent",
                "actor_name": "test",
                "action": "rpc.raw",
                "params": {},
                "status": "queued",
                "created_ns": 1,
            }
        )
        from iris_gateway.operations import _Pending

        manager._pending["queued"] = _Pending("queued", "device-a")
        assert await manager.cancel_queued() == 1
        assert store.operation("queued")["status"] == "cancelled"
        store.close()

    asyncio.run(scenario())


def test_exclusive_operation_drains_one_device_and_rejects_later_work(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)

        async def sink(event):
            del event

        manager = OperationManager(store, sink)
        started = asyncio.Event()
        release = asyncio.Event()

        async def existing() -> dict[str, bool]:
            started.set()
            await release.wait()
            return {"ok": True}

        running = asyncio.create_task(
            manager.execute(
                "device-a", Actor("agent", "test"), "firmware.ota", {}, existing
            )
        )
        await started.wait()
        finished = asyncio.Event()
        host = asyncio.create_task(manager.execute(
            "device-a", Actor("agent", "test"), "host.recovery", {}, finished.wait,
            exclusive_resources=("device-a", "usb:a"),
        ))
        await asyncio.sleep(0)
        with pytest.raises(DeviceBusy):
            await manager.execute(
                "device-a",
                Actor("agent", "test"),
                "rpc.raw",
                {},
                lambda: asyncio.sleep(0),
            )
        other, _, _ = await manager.execute(
            "device-b",
            Actor("agent", "test"),
            "rpc.raw",
            {},
            lambda: asyncio.sleep(0, result={"ok": True}),
        )
        assert other["status"] == "succeeded"
        release.set()
        await running
        await asyncio.sleep(0)
        assert manager.queue_state("device-a")["running"]
        with pytest.raises(DeviceBusy):
            await manager.execute("usb:a", Actor("agent", "test"), "host.probe", {}, lambda: asyncio.sleep(0))
        finished.set()
        await host
        assert not manager.queue_state("device-a")["running"]
        assert not manager._exclusive
        store.close()

    asyncio.run(scenario())


def test_rejected_precondition_retains_structured_failure(tmp_path):
    from iris_gateway.operations import OperationRejected

    async def scenario():
        store = GatewayStore(tmp_path)
        events = []
        async def sink(event):
            events.append(event)
        async def reject():
            raise OperationRejected("layout mismatch", code="partition_layout_mismatch",
                                    current_sha256="ab" * 32, target_sha256="cd" * 32, write_started=False)
        manager = OperationManager(store, sink)
        try:
            with pytest.raises(OperationRejected):
                await manager.execute("device", Actor("agent", "test"), "ota", {}, reject, operation_id="mismatch")
            operation = store.operation("mismatch")
            assert operation["status"] == "failed"
            assert operation["result"]["failure"]["code"] == "partition_layout_mismatch"
            assert operation["result"]["failure"]["write_started"] is False
            assert events[-1]["operation"] == operation
        finally:
            store.close()
    asyncio.run(scenario())
