from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_hub import SupervisorLink
from test_usb_ownership import until

from iris_gateway.gateway import GatewayService, create_app
from iris_gateway.hub import IrisHub
from iris_gateway.link import EndpointLock, TcpLink
from iris_gateway.ownership import OwnershipConflict, OwnershipRegistry
from iris_gateway.project_gateway import ProjectGateway
from iris_gateway.store import GatewayStore

D = "00112233445566778899aabbccddeeff"
E = "tcp:127.0.0.1:29772"


def registry(root, name):
    item = OwnershipRegistry(root)
    item.register(name, name, "/projects/" + name, "gateway-" + name)
    return item


def test_transfer_reservation_and_idempotency(tmp_path):
    a, b, c = (registry(tmp_path, name) for name in ("a", "b", "c"))
    try:
        a.acquire(E, {"endpoint": E})
        a.bind(E, D)
        t = str(uuid.uuid4())
        a.prepare(D, "b", t)
        with pytest.raises(OwnershipConflict):
            c.acquire(E, {})
        with pytest.raises(OwnershipConflict):
            a.acquire(E, {})
        a.offer(t)
        a.close()
        assert not b.claim(E)["owner_alive"]
        with pytest.raises(OwnershipConflict):
            c.reconcile_orphan(E)
        b.accept(t)
        b.bind(E, D)
        b.complete(t, D)
        assert b.accept(t)["state"] == "completed"
        assert b.claim(E)["generation"] == 2
        with pytest.raises(OwnershipConflict):
            c.acquire(E, {})
    finally:
        b.close(clean=True)
        c.close()


def test_no_timeout_based_reclaim_and_explicit_orphan_reconciliation(tmp_path):
    a, b = (registry(tmp_path, name) for name in ("a", "b"))
    a.acquire(E, {})
    a.bind(E, D)
    try:
        with pytest.raises(OwnershipConflict):
            b.reconcile_orphan(E)
        a.close()
        with pytest.raises(OwnershipConflict):
            b.acquire(E, {})
        lock = EndpointLock(E)
        try:
            lock.acquire()
            with pytest.raises(RuntimeError):
                b.reconcile_orphan(E)
        finally:
            lock.close()
        b.reconcile_orphan(E)
        b.acquire(E, {})
        b.bind(E, D)
        assert b.claim("device:" + D)["owner"] == "b"
    finally:
        b.close(clean=True)


def test_rollback_requires_target_exit_and_never_rolls_back_commit(tmp_path):
    a, b = (registry(tmp_path, name) for name in ("a", "b"))
    try:
        a.acquire(E, {})
        a.bind(E, D)
        t = str(uuid.uuid4())
        a.prepare(D, "b", t)
        a.offer(t)
        b.accept(t)
        with pytest.raises(OwnershipConflict):
            a.abort(t)
        b.close()
        a.abort(t)
        assert a.allowed(E)
        assert a.abort(t)["state"] == "aborted"
    finally:
        a.close(clean=True)


def test_cross_process_crash_preserves_reservation(tmp_path):
    code = '''
import pathlib, sys
from iris_gateway.ownership import OwnershipRegistry
r = OwnershipRegistry(pathlib.Path(sys.argv[1]))
r.register("child", "p", "/p", "i")
r.acquire("usb:location=test", {})
print("ready", flush=True)
sys.stdin.read()
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        other = registry(tmp_path, "other")
        try:
            assert other.claim("usb:location=test")["owner_alive"]
            process.kill()
            process.wait(timeout=5)
            assert not other.claim("usb:location=test")["owner_alive"]
            with pytest.raises(OwnershipConflict):
                other.acquire("usb:location=test", {})
            other.reconcile_orphan("usb:location=test")
        finally:
            other.close()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("by_endpoint", [False, True])
def test_discovery_reconnect_and_transfer_with_third_contender(tmp_path, by_endpoint):
    async def scenario():
        registries = [registry(tmp_path / "registry", name) for name in ("a", "b", "c")]
        stores = [GatewayStore(tmp_path / name) for name in ("a", "b", "c")]
        hubs, projects = [], []
        opened = []

        async def open_link(host, port):
            link = SupervisorLink(len(opened) + 1, endpoint=f"tcp:{host}:{port}")
            opened.append(link)
            return link

        for reg, store in zip(registries, stores):
            service = GatewayService(store, instance_id=reg.session_id)
            hub = IrisHub(reg.session_id, ownership=reg, event_sink=service.on_device_event,
                          reconnect_min_seconds=0.005, reconnect_max_seconds=0.01)
            service.attach_hub(hub)
            project = ProjectGateway(reg, service, hub, asyncio.Event())
            service.project = project
            hubs.append(hub)
            projects.append(project)
        try:
            with patch.object(TcpLink, "open", side_effect=open_link):
                for hub in hubs:
                    await hub.add_tcp("127.0.0.1", 29772)
                await asyncio.sleep(0.03)
                assert not opened  # discovery must never open an unknown device
                await projects[0].acquire({"endpoint": E})
                assert len(opened) == 1
                await opened[0].incoming.put(b"")
                await until(lambda: len(opened) == 2 and bool(hubs[0].list_devices()))
                assert registries[1].claim("device:" + D)["owner"] == "a"
                with pytest.raises(OwnershipConflict):
                    await projects[1].acquire({"endpoint": E})
                t = str(uuid.uuid4())
                await projects[0].prepare({"device_id": D, "target_session_id": "b", "transfer_id": t})
                assert opened[-1].closed
                with pytest.raises(OwnershipConflict):
                    await projects[2].acquire({"endpoint": E})
                result = await projects[1].accept(t)
                assert result["state"] == "completed"
                assert not hubs[0].list_devices()
                await hubs[0].add_tcp("127.0.0.1", 29772)
                await asyncio.sleep(0.03)
                assert len(opened) == 3
                assert registries[0].claim("device:" + D)["owner"] == "b"
                # Exercise the complete HTTP coordinator in the reverse
                # direction, including an identical retry after commit.
                clients = [TestClient(TestServer(create_app(project.service))) for project in projects[:2]]
                try:
                    for client, reg in zip(clients, registries):
                        await client.start_server()
                        reg.set_url(str(client.make_url("")).rstrip("/"))
                    body = {"endpoint": E} if by_endpoint else {"device_id": D}
                    body["takeover_id"] = str(uuid.uuid4())
                    response = await clients[0].post("/v1/project/takeovers", json=body)
                    assert response.status == 200, await response.text()
                    result = await response.json()
                    assert result["takeover"]["state"] == "completed"
                    response = await clients[0].post("/v1/project/takeovers", json=body)
                    assert await response.json() == result
                    assert len(opened) == 4
                finally:
                    for client in clients:
                        await client.close()
        finally:
            for hub in hubs:
                await hub.close()
            for reg, store in zip(registries, stores):
                reg.close(clean=True)
                store.close()
    asyncio.run(scenario())


def test_http_blocks_non_owner_and_transfer_when_busy(tmp_path):
    async def scenario():
        reg = registry(tmp_path / "registry", "a")
        store = GatewayStore(tmp_path / "store")
        service = GatewayService(store, instance_id="a")
        hub = IrisHub("a", ownership=reg)
        service.attach_hub(hub)
        service.project = ProjectGateway(reg, service, hub, asyncio.Event())
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            store.remember_device({"device_id": D})
            response = await client.get("/v1/devices/" + D)
            assert response.status == 409
            reg.acquire(E, {})
            reg.bind(E, D)
            gate = asyncio.Event()
            await service.operations.submit(D, __import__("iris_gateway.security", fromlist=["Actor"]).Actor("local", "test"),
                                            "host.recovery", {}, gate.wait, exclusive_resources=(D,))
            with pytest.raises(web.HTTPConflict):
                await service.project.prepare({"device_id": D, "target_session_id": "b", "transfer_id": str(uuid.uuid4())})
            assert reg.claim(E)["state"] == "owned"
            gate.set()
            while service.operations._pending:
                await asyncio.sleep(0)
            service.project.observe({"kind": "job", "device_id": D, "job_id": 7, "job_state": "running"})
            assert service.project.busy(D)

            async def finished_job(*args, **kwargs):
                return {"job_id": 7, "job_state": "succeeded"}

            with patch.object(hub, "job", side_effect=finished_job):
                response = await client.get(f"/v1/devices/{D}/jobs/7")
                assert response.status == 200
            assert not service.project.busy(D)
            response = await client.post("/v1/project/stop", json={})
            assert response.status == 404
            assert not service.project.closing
            response = await client.post("/v1/project/acquire", json={
                "endpoint": "tcp:127.0.0.1:29999", "pairing_token": "invalid",
            })
            assert response.status == 400
            assert reg.claim("tcp:127.0.0.1:29999") is None
            reg.acquire("tcp:127.0.0.1:29999", {})
            response = await client.post("/v1/project/release", json={"endpoint": "tcp:127.0.0.1:29999"})
            assert response.status == 200
            assert reg.claim("tcp:127.0.0.1:29999") is None
        finally:
            await client.close()
            await hub.close()
            reg.close(clean=True)
            store.close()
    asyncio.run(scenario())


def test_preparing_crash_and_lost_commit_ack(tmp_path):
    a, b = (registry(tmp_path, name) for name in ("a", "b"))
    a.acquire(E, {})
    a.bind(E, D)
    transfer = str(uuid.uuid4())
    a.prepare(D, "b", transfer)
    a.close()  # crash before offer: target needs proof that physical I/O ended
    lock = EndpointLock(E)
    try:
        lock.acquire()
        with pytest.raises(RuntimeError):
            b.accept(transfer)
        assert b.transfer(transfer)["state"] == "preparing"
    finally:
        lock.close()
    try:
        b.accept(transfer)
        with pytest.raises(OwnershipConflict):
            b.bind(E, "ffffffffffffffffffffffffffffffff")
        assert b.claim(E)["state"] == "accepting"
        b.bind(E, D)
        b.complete(transfer, D)
        # An acknowledgement lost after commit must not change generations.
        assert b.accept(transfer)["state"] == "completed"
        assert b.complete(transfer, D)["state"] == "completed"
        assert b.claim(E)["generation"] == 2
    finally:
        b.close(clean=True)


def test_both_participants_crash_requires_explicit_project_reconciliation(tmp_path):
    a, b, c = (registry(tmp_path, name) for name in ("a", "b", "c"))
    a.acquire(E, {})
    a.bind(E, D)
    transfer = str(uuid.uuid4())
    a.prepare(D, "b", transfer)
    a.offer(transfer)
    b.accept(transfer)
    a.close()
    b.close()
    restarted = OwnershipRegistry(tmp_path)
    restarted.register("b-new", "b", "/projects/b", "gateway-b-new")
    try:
        with pytest.raises(OwnershipConflict):
            c.reconcile_transfer(transfer)
        with pytest.raises(OwnershipConflict):
            restarted.acquire(E, {})
        result = restarted.reconcile_transfer(transfer)
        assert result["metadata"]["reconciled_by"] == "b-new"
        assert restarted.allowed(E)
        assert restarted.reconcile_transfer(transfer) == result
    finally:
        restarted.close(clean=True)
        c.close()




def test_device_generation_increases_when_transport_changes(tmp_path):
    a = registry(tmp_path, "a")
    try:
        for endpoint in (E, "tcp:127.0.0.1:29773", "usb:location=test"):
            a.acquire(endpoint, {})
            a.bind(endpoint, D)
            generation = a.claim("device:" + D)["generation"]
            a.release(endpoint)
        assert generation == 3
    finally:
        a.close(clean=True)


def test_drain_rejects_new_work_and_stops_after_active_work_finishes(tmp_path):
    async def scenario():
        reg = registry(tmp_path / "registry", "a")
        store = GatewayStore(tmp_path / "store")
        service = GatewayService(store, instance_id="a")
        hub = IrisHub("a", ownership=reg)
        service.attach_hub(hub)
        stop = asyncio.Event()
        project = ProjectGateway(reg, service, hub, stop)
        try:
            project.active[D] = 1
            task = asyncio.create_task(project.drain(timeout=2))
            project.request_stop()
            with pytest.raises(OwnershipConflict):
                await project.acquire({"endpoint": E})
            await asyncio.sleep(0.03)
            assert not stop.is_set()
            project.active[D] = 0
            await asyncio.wait_for(task, 1)
            assert stop.is_set() and not project.drain_timed_out
        finally:
            await hub.close()
            reg.close(clean=True)
            store.close()
    asyncio.run(scenario())
