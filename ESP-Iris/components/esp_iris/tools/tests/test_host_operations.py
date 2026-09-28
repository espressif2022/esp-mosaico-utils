from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from iris_gateway.demo import DemoHub
from iris_gateway.device_activity import stop_session_work
from iris_gateway.device_state import describe
from iris_gateway.gateway import GatewayService, create_app
from iris_gateway.host_worker import (
    active_workers,
    consume_request,
    publish_request,
    request_directory,
)
from iris_gateway.link import EndpointLock
from iris_gateway.security import Actor
from iris_gateway.store import GatewayStore


class HostHub:
    def __init__(self):
        self.connected = True
        self.endpoint = "usb:location=host-operation-test"
        self.info = {"device_id": "device-a", "boot_id": 1, "firmware_mode": "normal",
                     "app_version": "old", "endpoint": self.endpoint, "capability_names": ["ota"]}
        self._mirror_states = {}
        self.actions = []

    def active_mirrors(self, device_id):
        return []

    def get(self, device_id):
        assert device_id == "device-a"
        return SimpleNamespace(link=SimpleNamespace(endpoint=self.endpoint))

    def list_devices(self):
        return [self.info.copy()] if self.connected else []

    def list_endpoints(self):
        return [{"endpoint": self.endpoint, "state": "ready" if self.connected else "host_operation"}]

    def host_endpoint(self, identifier):
        assert identifier == self.endpoint
        return {"endpoint": self.endpoint, "device_id": "device-a"}

    async def status(self, device_id):
        assert self.connected
        return self.info.copy()

    async def crash_report(self, device_id):
        return {"core_dump_present": False}

    async def detach_for_host(self, endpoint):
        self.actions.append("detach")
        self.connected = False
        return self.host_endpoint(endpoint)

    def yield_host_lock(self, endpoint):
        self.actions.append("yield")

    async def resume_after_host(self, endpoint, *, connect=False):
        self.actions.append("resume")
        self.connected = True


@pytest.mark.parametrize("outcome", ["success", "foreign", "old_boot", "failed", "timeout_read", "pipe_failure"])
def test_host_operation_has_one_record_and_always_finishes_after_writer(tmp_path, outcome):
    async def scenario():
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="host-test")
        hub = HostHub()
        service.attach_hub(hub)
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        writing = asyncio.Event()
        finish = asyncio.Event()

        class Process:
            def __init__(self):
                self.stdin = self
                self.returncode = None

            def write(self, data):
                self.spec = json.loads(data)

            async def drain(self):
                if outcome == "pipe_failure":
                    raise BrokenPipeError("request pipe failed")

            def close(self):
                pass

            async def wait(self):
                writing.set()
                await finish.wait()
                hub.actions.append("writer_exit")
                hub.info.update(boot_id=2, firmware_mode="recovery", app_version="new")
                if outcome == "foreign":
                    hub.info["device_id"] = "device-b"
                if outcome == "old_boot":
                    hub.info["boot_id"] = 1
                if outcome == "timeout_read":
                    original = hub.status
                    first = True

                    async def flaky(device_id):
                        nonlocal first
                        if first:
                            first = False
                            raise OSError("reenumerating")
                        return await original(device_id)
                    hub.status = flaky
                Path(self.spec["result_path"]).write_text(json.dumps(
                    {"returncode": int(outcome == "failed"), "error": "writer failed", "stdout": "written"}))
                self.returncode = 0
                return 0

        try:
            request_id = publish_request({"action": "host.recovery", "device_id": "device-a",
                                          "commands": [{"argv": ["test-writer"]}],
                                          "expected_version": "new", "timeout": 0.15})
            with patch("iris_gateway.host_operations.asyncio.create_subprocess_exec", AsyncMock(return_value=Process())):
                response = await client.post("/v1/host-operations", json={"request_id": request_id})
                assert response.status == 202, await response.text()
                await asyncio.wait_for(writing.wait(), 1)
                assert service.list_devices()[0]["state"] == "busy"
                assert not service.list_devices()[0]["connected"]
                with pytest.raises(RuntimeError, match="exclusive operation"):
                    await service.operations.execute("device-a", Actor("local", "other"),
                                                     "rpc.raw", {}, AsyncMock())
                assert "resume" not in hub.actions
                finish.set()
                while service.operations._pending:
                    await asyncio.sleep(0.01)
            operation = store.operation(request_id)
            assert operation["status"] == ("succeeded" if outcome in {"success", "timeout_read"}
                                            else "failed" if outcome in {"failed", "pipe_failure"} else "outcome_unknown")
            assert hub.actions.index("writer_exit") < hub.actions.index("resume")
            assert not service.operations._exclusive
            assert len(store.operations()) == 1
            # A failed verification retains evidence, never a persistent reservation.
            assert not any("maintenance" in row[0] for row in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"))
            assert service.list_devices()[0]["state"] != "busy"
        finally:
            finish.set()
            await service.operations.close()
            await client.close()
            store.close()

    asyncio.run(scenario())


def test_browser_cannot_submit_executable_commands(tmp_path):
    async def scenario():
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="host-test")
        service.attach_hub(HostHub())
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            response = await client.post("/v1/host-operations", json={"commands": ["anything"]},
                                         headers={"Origin": "http://untrusted.invalid"})
            assert response.status == 403
            for path in ("/v1/maintenance-endpoints/leases", "/v1/maintenance-leases/old/abort"):
                assert (await client.post(path, json={})).status == 404
        finally:
            await client.close()
            store.close()
    asyncio.run(scenario())


def test_private_request_is_consumed_once_and_rejects_symlinks(tmp_path):
    request_id = publish_request({"action": "host.probe"})
    assert consume_request(request_id) == {"action": "host.probe"}
    with pytest.raises(KeyError, match="already consumed"):
        consume_request(request_id)
    if os.name != "nt":
        path = request_directory() / (request_id + ".json")
        path.symlink_to(tmp_path / "missing")
        try:
            with pytest.raises(PermissionError):
                consume_request(request_id)
        finally:
            path.unlink()


@pytest.mark.parametrize("connected,transport,operation,expected", [
    (True, "normal", False, "idle"),
    (True, "recovery", False, "idle"),
    (False, "absent", False, "offline"),
    (False, "normal", False, "connecting"),
    (False, "rom", False, "needs_recovery"),
    (False, "rom", True, "busy"),
    (False, "absent", True, "busy"),
])
def test_five_states_use_live_evidence_and_busy_survives_reenumeration(tmp_path, connected, transport, operation, expected):
    store = GatewayStore(tmp_path)
    service = GatewayService(store, instance_id="state-test")
    hub = HostHub()
    service.attach_hub(hub)
    workers = [{"operation_id": "write", "resources": ["device-a"]}] if operation else []
    current = {"vid": 0x303A, "pid": 0x0020 if transport == "rom" else 0x4002}
    with patch("iris_gateway.device_state.resolve_usb_port", side_effect=OSError() if transport == "absent" else None,
               return_value=current):
        result = describe(service, {**hub.info, "connected": connected}, workers=workers)
    assert result["state"] == expected
    store.close()


def test_writer_survives_parent_exit_and_holds_physical_lock(tmp_path):
    operation_id = str(uuid.uuid4())
    endpoint = "usb:location=" + operation_id
    release, started = tmp_path / "release", tmp_path / "started"
    writer = (
        "import pathlib,time; "
        f"pathlib.Path({str(started)!r}).touch(); "
        f"release=pathlib.Path({str(release)!r}); "
        "\nwhile not release.exists(): time.sleep(0.02)\n"
    )
    spec = {"operation_id": operation_id, "resources": [endpoint], "endpoints": [],
            "locks": [endpoint], "write_endpoint": endpoint, "commands": [{"argv": [sys.executable, "-c", writer]}],
            "log_path": str(tmp_path / "writer.log"), "result_path": str(tmp_path / "result.json")}
    bootstrap = (
        "from iris_gateway import host_worker as w; "
        f"w.resolve_usb_port=lambda _: {{'device_path':'fake','location':{operation_id!r}}}; "
        "w.main()"
    )
    # The parent behaves like a Gateway which disappears immediately after
    # dispatch. Only the independent worker remains to protect the actual writer.
    parent = (
        "import subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{bootstrap!r}],stdin=subprocess.PIPE,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True); "
        f"p.stdin.write({json.dumps(spec).encode()!r}); p.stdin.close(); print(p.pid)"
    )
    launched = subprocess.run([sys.executable, "-c", parent], capture_output=True, text=True, timeout=5, check=False)
    assert launched.returncode == 0, launched.stderr
    lock = EndpointLock(endpoint)
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists()
        assert any(value["operation_id"] == operation_id for value in active_workers())
        with pytest.raises(RuntimeError):
            lock.acquire()
    finally:
        release.touch()
        deadline = time.monotonic() + 5
        while not Path(spec["result_path"]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        lock.close()
    assert json.loads(Path(spec["result_path"]).read_text())["returncode"] == 0
    with_lock = EndpointLock(endpoint)
    try:
        with_lock.acquire()
    finally:
        with_lock.close()
    assert not any(value["operation_id"] == operation_id for value in active_workers())


def test_logs_do_not_block_but_mirrors_and_jobs_make_device_busy(tmp_path):
    async def scenario():
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="states", demo=True)
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        device_id = "demo-a1b2c3d4"

        def state():
            return next(item for item in service.list_devices() if item["device_id"] == device_id)

        logs = service.subscribe()
        try:
            assert state()["state"] == "idle"
            await hub.mirror_start(device_id, 3)
            assert state()["busy_reasons"] == [{"kind": "mirror", "channel": 3}]
            await hub.mirror_stop(device_id, 3)
            assert state()["state"] == "idle"
            await service.on_device_event({"kind": "job", "device_id": device_id,
                                           "job_id": 7, "job_state": "running"})
            assert state()["state"] == "busy"
            await service.on_device_event({"kind": "job", "device_id": device_id,
                                           "job_id": 7, "job_state": "cancelled"})
            assert state()["state"] == "idle"
        finally:
            service.unsubscribe(logs)
            await hub.close()
            store.close()
    asyncio.run(scenario())


def test_rom_identity_mismatch_never_starts_following_write(tmp_path, monkeypatch):
    from iris_gateway import host_worker

    operation_id = str(uuid.uuid4())
    endpoint = "usb:location=" + operation_id
    monkeypatch.setattr(host_worker, "resolve_usb_port", lambda _: {"device_path": "fake", "location": operation_id})
    written = tmp_path / "written"
    spec = {"operation_id": operation_id, "resources": [endpoint], "endpoints": [],
            "locks": [endpoint], "write_endpoint": endpoint, "log_path": str(tmp_path / "log"),
            "commands": [
                {"argv": [sys.executable, "-c", "print('MAC: wrong')"],
                 "expect": {"pattern": "MAC: (.+)", "value": "correct"}},
                {"argv": [sys.executable, "-c", f"open({str(written)!r},'w').close()"]},
            ]}
    with pytest.raises(RuntimeError, match="identity verification failed"):
        host_worker.run(spec)
    assert not written.exists()
    lock = EndpointLock(endpoint)
    try:
        lock.acquire()
    finally:
        lock.close()


@pytest.mark.parametrize("finishes", [True, False])
def test_rom_work_stops_mirrors_and_requires_job_terminal_confirmation(tmp_path, finishes):
    async def scenario():
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="cleanup")
        hub = HostHub()
        service.attach_hub(hub)
        service.observe_device_activity({"kind": "job", "device_id": "device-a",
                                         "job_id": 7, "job_state": "running"})
        hub.active_mirrors = lambda _: [3, 4, 5]
        hub.mirror_stop = AsyncMock()
        # A non-terminal device may be polled more than once before the
        # deadline, depending on the platform's event-loop timer resolution.
        def job_result(device_id, job_id, *, cancel):
            assert (device_id, job_id) == ("device-a", 7)
            return {"job_id": 7,
                    "job_state": "cancelled" if finishes and not cancel else "running",
                    "cancel_requested": cancel}

        hub.job = AsyncMock(side_effect=job_result)
        try:
            if finishes:
                await stop_session_work(service, "device-a", 0.08)
                assert not service.jobs
            else:
                with pytest.raises(TimeoutError, match="jobs did not finish"):
                    await stop_session_work(service, "device-a", 0.08)
                assert ("device-a", 7) in service.jobs
            assert [call.args for call in hub.mirror_stop.await_args_list] == [
                ("device-a", 3), ("device-a", 4), ("device-a", 5)]
            cancellations = [call.kwargs["cancel"] for call in hub.job.await_args_list]
            assert len(cancellations) >= 2 and cancellations[0] is True
            assert not any(cancellations[1:])
            assert not hub.actions
        finally:
            store.close()
    asyncio.run(scenario())


def test_unmanaged_rom_is_visible_without_opening_or_inventing_identity(tmp_path, monkeypatch):
    port = SimpleNamespace(device="/dev/test-rom", location="test:rom", serial_number="rom",
                           vid=0x303A, pid=0x0020, product="ESP32-S31")
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [port])
    store = GatewayStore(tmp_path)
    service = GatewayService(store, instance_id="discovery")
    hub = HostHub()
    hub.list_endpoints = list
    service.attach_hub(hub)
    try:
        endpoints = service.list_endpoints()
        assert len(endpoints) == 1
        assert endpoints[0]["state"] == "needs_recovery"
        assert endpoints[0]["firmware_mode"] == "rom"
        assert not endpoints[0].get("device_id")
        assert not hub.actions
    finally:
        store.close()


def test_host_worker_uses_reenumerated_device_path(tmp_path, monkeypatch):
    from serial.tools import list_ports

    from iris_gateway import host_worker

    operation_id = str(uuid.uuid4())
    endpoint = "usb:location=" + operation_id
    port = "/dev/ttyACM77"
    monkeypatch.setattr(list_ports, "comports", lambda: [SimpleNamespace(
        device=port, vid=0x303A, pid=0x0020, serial_number="test-rom",
        product="ESP32-S31", location=operation_id,
    )])
    # Exercise the real discovery schema, not a mock with a fictitious `device`
    # key. No serial interface is opened: the foreground command only echoes.
    spec = {
        "operation_id": operation_id, "resources": [endpoint], "endpoints": [],
        "locks": [endpoint], "write_endpoint": endpoint,
        "log_path": str(tmp_path / "probe.log"),
        "commands": [{
            "argv": [sys.executable, "-c",
                     "import os,sys; assert sys.argv[1] == os.environ['RECOVERY_PORT']; print(sys.argv[1])",
                     "{port}"],
            "env": {"RECOVERY_PORT": "{port}"},
        }],
    }
    result = host_worker.run(spec)
    assert result["returncode"] == 0
    assert result["stdout"].strip() == port
