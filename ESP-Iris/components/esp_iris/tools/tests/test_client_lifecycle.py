from __future__ import annotations

import asyncio
import sqlite3
import uuid
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_usb_ownership import until

from iris_gateway.client_lifecycle import CAPABILITY, LEASE_SECONDS, ClientLifecycle
from iris_gateway.gateway import GatewayService, create_app
from iris_gateway.hub import IrisHub
from iris_gateway.ownership import OwnershipRegistry
from iris_gateway.project_gateway import ProjectGateway
from iris_gateway.store import GatewayStore


def test_references_idle_deadline_and_passive_queries():
    now = [0.0]
    lifetime = ClientLifecycle(clock=lambda: now[0])
    a = lifetime.register({"kind": "cli", "command": "iris app-update"})
    b = lifetime.register({"kind": "run", "command": "iris run"})
    lifetime.release(a["client_id"], a["lease_token"])
    assert lifetime.snapshot({})["idle_remaining_seconds"] is None
    lifetime.release(b["client_id"], b["lease_token"])
    for second in range(10):
        now[0] = float(second)
        assert not lifetime.should_stop({})
        assert lifetime.snapshot({})["idle_remaining_seconds"] == 10 - second
    now[0] = 10
    assert lifetime.should_stop({})


def test_crashed_client_expiration_starts_a_separate_idle_period():
    now = [0.0]
    lifetime = ClientLifecycle(clock=lambda: now[0])
    lease = lifetime.register({"kind": "run"})
    now[0] = LEASE_SECONDS - 1
    assert lifetime.snapshot({})["clients"]
    now[0] = LEASE_SECONDS
    assert lifetime.snapshot({})["idle_remaining_seconds"] == 10
    with pytest.raises(KeyError):
        lifetime.renew(lease["client_id"], lease["lease_token"])
    now[0] += 10
    assert lifetime.should_stop({})


def test_idle_join_renews_once_and_private_tokens_are_not_exposed():
    now = [0.0]
    lifetime = ClientLifecycle(clock=lambda: now[0])
    now[0] = 9.9
    body = {"client_id": str(uuid.uuid4()), "lease_token": "a" * 64, "pid": 123, "command": "iris run"}
    first = lifetime.register(body)
    assert lifetime.register(body) == first
    assert len(lifetime.snapshot({})["clients"]) == 1
    assert "lease_token" not in lifetime.snapshot({})["clients"][0]
    with pytest.raises(PermissionError):
        lifetime.release(first["client_id"], "wrong")
    lifetime.release(first["client_id"], first["lease_token"])
    now[0] = 15
    lifetime.release(first["client_id"], first["lease_token"])
    assert lifetime.snapshot({})["idle_remaining_seconds"] == pytest.approx(4.9)
    lifetime.closing = True
    with pytest.raises(RuntimeError):
        lifetime.register({})


@pytest.mark.parametrize("work", ["operations", "host_workers", "jobs", "requests", "mirrors", "streams"])
def test_work_without_clients_defers_idle_shutdown(work):
    now = [0.0]
    lifetime = ClientLifecycle(clock=lambda: now[0])
    now[0] = 100
    assert not lifetime.should_stop({work: 1})
    assert lifetime.snapshot({work: 0})["idle_remaining_seconds"] == 10
    now[0] = 110
    assert lifetime.should_stop({work: 0})


def test_legacy_sessions_table_remains_writable(tmp_path):
    registry = OwnershipRegistry(tmp_path)
    try:
        registry.register("new", "project", "/project", "instance")
        registry.set_metadata("/workspace", CAPABILITY, "revision")
        with sqlite3.connect(tmp_path / "ownership.sqlite3") as old:
            old.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", ("old", "old-project", "/old", "old-instance", "", 1, 1))
        assert registry.session("new")["workspace_path"] == "/workspace"
        assert "lifecycle_capability" not in registry.session("old")
    finally:
        registry.close()


def test_http_client_leases_and_workbench_reference_cleanup(tmp_path):
    async def scenario():
        registry = OwnershipRegistry(tmp_path / "registry")
        registry.register("session", "project", "/project", "gateway")
        store = GatewayStore(tmp_path / "store")
        service = GatewayService(store, instance_id="gateway")
        hub = IrisHub("gateway", ownership=registry)
        service.attach_hub(hub)
        project = ProjectGateway(registry, service, hub, asyncio.Event())
        service.project = project
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            response = await client.post("/v1/project/clients", json={"session_id": "wrong"})
            assert response.status == 409
            response = await client.post("/v1/project/clients", json={"session_id": "session", "kind": "run", "pid": 123})
            assert response.status == 200
            lease = await response.json()
            lease["session_id"] = "session"
            first = await client.ws_connect("/v1/events/ws?client=workbench")
            notification = await first.receive_json()
            first_id = notification["client_id"]
            last_seen = project.clients.clients[first_id]["last_seen_ns"]
            with patch("iris_gateway.client_lifecycle.time.time_ns", return_value=last_seen + 1):
                await first.pong(b"alive")
                await until(lambda: project.clients.clients[first_id]["last_seen_ns"] == last_seen + 1)
            second = await client.ws_connect("/v1/events/ws?client=workbench")
            assert {item["kind"] for item in project.clients.snapshot({})["clients"]} == {"run", "workbench"}
            assert len(project.clients.clients) == 3
            await first.close()
            await until(lambda: len(project.clients.clients) == 2)
            path = "/v1/project/clients/" + lease["client_id"]
            assert (await client.post(path + "/renew", json=lease)).status == 200
            assert (await client.post(path + "/release", json=lease)).status == 200
            assert (await client.post(path + "/release", json=lease)).status == 200
            assert len(project.clients.clients) == 1
            await second.close()
            await until(lambda: not project.clients.clients)
            state = await (await client.get("/v1/project")).json()
            assert state["lifecycle"]["clients"] == []
            assert 9 < state["lifecycle"]["idle_remaining_seconds"] <= 10
            project.request_stop()
            response = await client.post("/v1/project/clients", json={"session_id": "session"})
            assert response.status == 409
            assert (await client.get("/v1/project")).status == 200
        finally:
            await client.close()
            await hub.close()
            registry.close(clean=True)
            store.close()
    asyncio.run(scenario())
