"""Single-device admission uses live descriptors and the existing ownership locks."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from iris_gateway.discovery import IrisUsbDevice
from iris_gateway.gateway import GatewayService, create_app
from iris_gateway.hub import IrisHub
from iris_gateway.ownership import OwnershipRegistry
from iris_gateway.project_gateway import ProjectGateway
from iris_gateway.store import GatewayStore

D = "00112233445566778899aabbccddeeff"
U = "usb:location=1-2"


def usb(location="1-2", *, pid=0x4002):
    return IrisUsbDevice("/dev/fake-" + location, "/dev/fake-" + location,
                         0x303A, pid, "serial-" + location, "ESP-Iris", location=location)


@asynccontextmanager
async def gateway(root, name="a", *, mock_handshake=True):
    registry = OwnershipRegistry(root / "ownership")
    registry.register(name, name, "/projects/" + name, name)
    store = GatewayStore(root / name)
    service = GatewayService(store, instance_id=name)
    hub = IrisHub(name, ownership=registry)
    service.attach_hub(hub)
    project = ProjectGateway(registry, service, hub, asyncio.Event())
    service.project = project
    live = []

    async def handshake(endpoint, metadata, **kwargs):
        registry.bind(endpoint, D)
        live.append({"device_id": D, "endpoint": endpoint, "boot_id": 7})

    with ExitStack() as patches:
        connect = None
        if mock_handshake:
            patches.enter_context(patch.object(hub, "list_devices", return_value=live))
            connect = patches.enter_context(patch.object(hub, "connect_owned", side_effect=handshake))
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            yield SimpleNamespace(registry=registry, project=project, hub=hub,
                                  client=client, live=live, connect=connect)
        finally:
            await client.close()
            await hub.close()
            registry.close(clean=True)
            store.close()


def test_unique_usb_handshake_pins_identity_and_prefers_it_over_new_devices(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as g:
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb()]) as discover:
                response = await g.client.post("/v1/project/acquire", json={"auto": True})
                assert response.status == 200
                assert (await response.json())["device"]["device_id"] == D
                assert g.registry.claim(U)["device_id"] == D
                assert g.registry.claim("device:" + D)["owner"] == "a"
                discover.return_value = [usb(), usb("1-3")]
                response = await g.client.post("/v1/project/acquire", json={"auto": True})
                assert response.status == 200
                assert (await response.json())["device"]["device_id"] == D
                discover.assert_called_once()
                g.connect.assert_awaited_once()
    asyncio.run(scenario())


def test_ambiguous_usb_returns_candidates_without_claiming_anything(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as g:
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb(), usb("1-3")]):
                response = await g.client.post("/v1/project/acquire", json={"auto": True, "allow_none": True})
                assert response.status == 400
                result = await response.json()
                assert result["error"]["code"] == "selection_error"
                assert result["error"]["details"]["candidates"] == [U, "usb:location=1-3"]
                assert not g.registry.claims()
                g.connect.assert_not_awaited()
    asyncio.run(scenario())


def test_cache_network_and_rom_are_not_automatic_usb_targets(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as g:
            # Even an old 'present' flag is not proof that USB is still attached.
            g.hub._candidates[U] = {"endpoint": U, "present": True}
            g.hub._candidates["tcp:host:1234"] = {"endpoint": "tcp:host:1234", "present": True}
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb(pid=0x0020), usb("1-3", pid=0x1001)]):
                response = await g.client.post("/v1/project/acquire", json={"auto": True, "allow_none": True})
                assert response.status == 200
                assert (await response.json())["device"] is None
                response = await g.client.post("/v1/project/acquire", json={"auto": True})
                assert response.status == 503
                g.connect.assert_not_awaited()
                assert not g.registry.claims()
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["owned", "offered", "orphan"])
def test_reserved_and_orphaned_devices_are_never_auto_reclaimed(tmp_path, state):
    async def scenario():
        other = OwnershipRegistry(tmp_path / "ownership")
        other.register("b", "b", "/projects/b", "b")
        other.acquire(U, {})
        other.bind(U, D)
        if state == "orphan":
            other.close()
        try:
            async with gateway(tmp_path) as g:
                if state == "offered":
                    transfer_id = str(uuid.uuid4())
                    other.prepare(D, "a", transfer_id)
                    other.offer(transfer_id)
                with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb()]):
                    response = await g.client.post("/v1/project/acquire", json={"auto": True, "allow_none": True})
                    assert response.status == 409
                    assert g.registry.claim(U)["owner"] == "b"
                    g.connect.assert_not_awaited()
        finally:
            if state != "orphan":
                other.close()
    asyncio.run(scenario())


def test_unique_free_usb_can_be_selected_beside_another_projects_device(tmp_path):
    async def scenario():
        async with gateway(tmp_path, "a") as a, gateway(tmp_path, "b") as b:
            b.registry.acquire("usb:location=1-3", {})
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb(), usb("1-3")]):
                response = await a.client.post("/v1/project/acquire", json={"auto": True})
                assert response.status == 200
                assert a.registry.claim(U)["owner"] == "a"
                assert a.registry.claim("usb:location=1-3")["owner"] == "b"
    asyncio.run(scenario())


def test_offline_owned_identity_waits_instead_of_switching_to_another_board(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as g:
            g.registry.acquire(U, {})
            g.registry.bind(U, D)
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices") as discover, \
                    patch.object(g.project, "wait_device", side_effect=TimeoutError("offline")) as wait:
                response = await g.client.post("/v1/project/acquire", json={"auto": True, "allow_none": True})
                assert response.status == 504
                wait.assert_awaited_once_with(D, "", 10.0)
                discover.assert_not_called()
                g.connect.assert_not_awaited()
                assert g.registry.claim(U)["device_id"] == D
    asyncio.run(scenario())


def test_explicit_missing_identity_never_falls_back_to_the_only_usb(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as g:
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb()]) as discover:
                response = await g.client.post("/v1/project/acquire", json={"device_id": "missing", "auto": True})
                assert response.status == 400
                discover.assert_not_called()
                g.connect.assert_not_awaited()
                assert not g.registry.claims()
    asyncio.run(scenario())


def test_one_device_with_two_owned_transports_can_be_released_offline(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as g:
            for endpoint in (U, "tcp:host:1234"):
                g.registry.acquire(endpoint, {})
                g.registry.bind(endpoint, D)
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices") as discover:
                response = await g.client.post("/v1/project/release", json={"auto": True})
                assert response.status == 200
                assert (await response.json())["released"] == "device:" + D
                assert not g.registry.claims()
                discover.assert_not_called()
                g.connect.assert_not_awaited()
    asyncio.run(scenario())


def test_multiple_owned_devices_require_selection_for_release(tmp_path):
    async def scenario():
        async with gateway(tmp_path) as g:
            for endpoint, device in ((U, D), ("usb:location=1-3", "another")):
                g.registry.acquire(endpoint, {})
                g.registry.bind(endpoint, device)
            for action in ("release",):
                response = await g.client.post("/v1/project/" + action, json={"auto": True, "target_session_id": "b"})
                assert response.status == 400
                assert (await response.json())["error"]["code"] == "selection_error"
            assert len(g.registry.claims()) == 4
    asyncio.run(scenario())


def test_competing_projects_cannot_both_claim_the_unique_device(tmp_path):
    async def scenario():
        async with gateway(tmp_path, "a") as a, gateway(tmp_path, "b") as b:
            with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb()]):
                responses = await asyncio.gather(*[
                    g.client.post("/v1/project/acquire", json={"auto": True}) for g in (a, b)
                ])
                assert sorted(r.status for r in responses) == [200, 409]
                assert a.connect.await_count + b.connect.await_count == 1
    asyncio.run(scenario())


def test_auto_usb_completes_real_hello_and_retains_identity_during_reconnect(tmp_path):
    from test_hub import SupervisorLink
    from test_usb_ownership import until

    async def scenario():
        async with gateway(tmp_path, mock_handshake=False) as g:
            endpoint = "usb:location=single-device-test"
            metadata = {"path": "/dev/fake-single", "location": "single-device-test",
                        "vid": 0x303A, "pid": 0x4002, "product": "ESP-Iris"}
            links = []

            async def open_link(selected):
                assert selected == endpoint
                link = SupervisorLink(len(links) + 1, boot_id=len(links) + 7, endpoint=selected)
                links.append(link)
                return link

            with patch("iris_gateway.project_gateway.discover_iris_usb_devices", return_value=[usb("single-device-test")]) as discover, \
                    patch("iris_gateway.hub.resolve_usb_port", return_value=metadata), \
                    patch.object(g.hub, "_open_usb", side_effect=open_link):
                response = await g.client.post("/v1/project/acquire", json={"auto": True})
                assert response.status == 200, await response.text()
                device = (await response.json())["device"]
                assert device["device_id"] == D
                assert device["boot_id"] == 7
                await links[0].incoming.put(b"")
                await until(lambda: len(links) == 2 and bool(g.hub.list_devices()))
                response = await g.client.post("/v1/project/acquire", json={"auto": True})
                assert response.status == 200
                device = (await response.json())["device"]
                assert device["device_id"] == D
                assert device["boot_id"] == 8
                assert g.registry.claim(endpoint)["device_id"] == D
                discover.assert_called_once()
    asyncio.run(scenario())
