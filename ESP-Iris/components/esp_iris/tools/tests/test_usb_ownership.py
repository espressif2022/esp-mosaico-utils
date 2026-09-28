from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_hub import SupervisorLink

from iris_gateway.cli import _web, build_parser
from iris_gateway.gateway import GatewayService
from iris_gateway.hub import IrisHub
from iris_gateway.link import EndpointLock, SerialLink
from iris_gateway.store import GatewayStore


class SilentLink:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.closed = False
        self.incoming = asyncio.Queue()

    async def read(self, size=4096):
        return await self.incoming.get()

    async def write(self, data):
        pass

    async def close(self):
        self.closed = True


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), 2)


@pytest.fixture
def usb(tmp_path, monkeypatch):
    monkeypatch.setattr("iris_gateway.link.tempfile.gettempdir", lambda: str(tmp_path))
    port = SimpleNamespace(
        device=str(tmp_path / "ttyACM0"),
        location="review:1.0",
        serial_number="board-a",
        vid=0x303A,
        pid=0x4002,
        product="ESP-Iris",
    )
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: [port])
    return port


def test_finished_unmanaged_jtag_does_not_adopt_port(tmp_path, usb):
    async def scenario():
        usb.pid = 0x1001
        usb.product = "USB JTAG/serial debug unit"
        hub = IrisHub("A")
        store = GatewayStore(tmp_path / "store")
        service = GatewayService(store, instance_id="A")
        service.attach_hub(hub)
        try:
            with patch.object(SerialLink, "open") as opened:
                endpoint = await hub.detach_for_host(usb.device)
                hub.yield_host_lock(endpoint["endpoint"])
                await hub.resume_after_host(endpoint["endpoint"])
                await asyncio.sleep(0.01)
                opened.assert_not_called()
                assert not hub._locks
                assert not hub._endpoint_tasks
        finally:
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_reconnect_filters_replacement_jtag_and_can_return_to_iris(usb):
    async def scenario():
        hub = IrisHub("A", reconnect_min_seconds=0.001, reconnect_max_seconds=0.005)
        opened = []

        async def opener(path, **kw):
            link = SilentLink(kw["endpoint"])
            opened.append(link)
            return link

        try:
            with patch.object(SerialLink, "open", side_effect=opener):
                await hub.add_usb(usb.device)
                await until(lambda: len(opened) == 1)
                usb.pid = 0x1001
                usb.product = "USB JTAG/serial debug unit"
                await opened[0].incoming.put(b"")
                await until(lambda: hub.list_endpoints()[0]["attempt"] >= 3)
                assert len(opened) == 1
                usb.pid = 0x4002
                usb.product = "ESP-Iris Recovery"
                await until(lambda: len(opened) == 2)
        finally:
            await hub.close()

    asyncio.run(scenario())


def test_alias_cannot_bypass_other_gateway_host_operation_lock(tmp_path, usb):
    async def scenario():
        first = IrisHub("A")
        second = IrisHub("B")
        alias = tmp_path / "by-id"
        alias.symlink_to(usb.device)
        try:
            with patch.object(SerialLink, "open") as opened:
                await first.add_usb(usb.device)
                await first.detach_for_host(usb.device)
                opened.reset_mock()
                await second.add_usb(str(alias))
                await until(lambda: second.list_endpoints()[0]["state"] == "owned_elsewhere")
                opened.assert_not_called()
                assert second.list_endpoints()[0]["state"] == "owned_elsewhere"
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())


def test_disconnected_device_identity_is_not_new_board_identity(tmp_path, usb):
    async def scenario():
        hub = IrisHub("A", reconnect_min_seconds=0.001)
        store = GatewayStore(tmp_path / "store")
        service = GatewayService(store, instance_id="A")
        service.attach_hub(hub)
        endpoint = "usb:location=review:1.0"
        old = SupervisorLink(1, endpoint=endpoint)
        count = 0

        async def opener():
            nonlocal count
            count += 1
            if count > 1:
                raise OSError("replacement board in ROM")
            return old

        try:
            hub._add_supervisor(endpoint, opener, metadata={"path": usb.device})
            await until(lambda: bool(hub.list_devices()))
            await old.incoming.put(b"")
            await until(lambda: not hub.list_devices())
            endpoint_state = hub.host_endpoint(usb.device)
            assert endpoint_state.get("device_id") is None

        finally:
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_silent_endpoint_is_closed_after_hello_deadline(usb):
    async def scenario():
        hub = IrisHub("A", hello_timeout_seconds=0.02, reconnect_min_seconds=1)
        link = SilentLink("usb:location=review:1.0")
        try:
            with patch.object(SerialLink, "open", return_value=link):
                await hub.add_usb(usb.device)
                # Link closure precedes supervisor cleanup and the retry state update.
                await until(
                    lambda: link.closed and hub.list_endpoints()[0]["state"] == "retrying"
                )
                assert not hub.list_devices()
                assert hub.list_endpoints()[0]["state"] == "retrying"
        finally:
            await hub.close()

    asyncio.run(scenario())


def test_second_gateway_cannot_mutate_active_operations(tmp_path, usb):
    async def scenario():
        state = tmp_path / "State"
        lock = EndpointLock("gateway-state:" + os.path.normcase(str(state.resolve())))
        lock.acquire()
        first = GatewayStore(state)
        try:
            first.create_operation(
                {
                    "operation_id": "ota",
                    "device_id": "A",
                    "actor_type": "local",
                    "actor_name": "test",
                    "action": "firmware.ota",
                    "status": "transferring",
                    "created_ns": 1,
                }
            )
            args = build_parser().parse_args(
                ["web", "--state-dir", str(state), "--demo"]
            )
            with pytest.raises(RuntimeError, match="owned by another"):
                await asyncio.wait_for(_web(args), timeout=2)
            assert first.operation("ota")["status"] == "transferring"
        finally:
            first.close()
            lock.close()

    asyncio.run(scenario())




def test_reused_tty_name_does_not_select_previous_socket(usb):
    hub = IrisHub("A")
    hub._endpoint_states["usb:location=old:1.0"] = {
        "endpoint": "usb:location=old:1.0",
        "path": "/dev/old-by-path",
        "device_path": usb.device,
        "device_id": "old-device",
        "state": "retrying",
    }
    current = hub.host_endpoint(usb.device)
    assert current["endpoint"] == "usb:location=review:1.0"
    assert current["device_id"] is None






def test_com_alias_lock_key_is_independent_of_workspace(tmp_path, monkeypatch):
    from iris_gateway.discovery import usb_endpoint

    original = usb_endpoint({"path": "COM14"})
    monkeypatch.chdir(tmp_path)
    assert original == usb_endpoint({"path": "com14"})
    assert original == usb_endpoint({"path": "\\\\.\\COM14"})
