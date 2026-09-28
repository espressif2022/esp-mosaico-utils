from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_hub import SupervisorLink
from test_usb_ownership import until

from iris_gateway.hub import IrisHub
from iris_gateway.link import SerialLink


@pytest.fixture
def ports(tmp_path, monkeypatch):
    monkeypatch.setattr("iris_gateway.link.tempfile.gettempdir", lambda: str(tmp_path))
    current = []
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: list(current))
    return current


def board(path="/dev/review-usb", **changes):
    values = {"device": path, "location": "review:1.0", "serial_number": "board-a",
              "vid": 0x303A, "pid": 0x4002, "product": "ESP-Iris"}
    values.update(changes)
    return SimpleNamespace(**values)


async def open_board(path, **kwargs):
    return SupervisorLink(1, endpoint=kwargs["endpoint"])


def test_explicit_offline_port_connects_after_arrival(ports):
    async def scenario():
        hub = IrisHub("A", reconnect_min_seconds=0.005)
        try:
            with patch.object(SerialLink, "open", side_effect=open_board) as opened:
                await hub.add_usb("/dev/review-usb")
                assert not hub.list_devices()
                opened.assert_not_called()
                with pytest.raises(LookupError, match="not yet bound"):
                    await hub.detach_for_host(hub.list_endpoints()[0]["endpoint"])
                assert not hub._locks
                ports.append(board())
                await until(lambda: bool(hub.list_devices()))
                assert opened.call_count == 1
                assert len(hub.list_endpoints()) == 1
        finally:
            await hub.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("vid,pid,product", [
    (0x1234, 0x5678, "Custom Product"),
    (0x303A, 0x5678, "Custom Product"),
])
def test_explicit_custom_descriptors_can_handshake(ports, vid, pid, product):
    async def scenario():
        ports.append(board(vid=vid, pid=pid, product=product))
        hub = IrisHub("A", reconnect_min_seconds=0.005)
        try:
            with patch.object(SerialLink, "open", side_effect=open_board):
                await hub.add_usb(ports[0].device)
                await until(lambda: bool(hub.list_devices()))
        finally:
            await hub.close()
    asyncio.run(scenario())






def test_pending_alias_and_discovery_share_one_supervisor(tmp_path, ports):
    async def scenario():
        hub = IrisHub("A", reconnect_min_seconds=0.005)
        alias = tmp_path / "by-id"
        try:
            with patch.object(SerialLink, "open", side_effect=open_board) as opened:
                await hub.add_usb(str(alias))
                current = board(path=str(tmp_path / "ttyACM0"))
                alias.symlink_to(current.device)
                ports.append(current)
                await hub.start_usb_discovery(0.005)
                await until(lambda: bool(hub.list_devices()) and not hub._pending_usb)
                assert opened.call_count == 1
                assert len(hub.list_endpoints()) == 1
                assert hub.list_endpoints()[0]["usb_selection"] == "explicit"
        finally:
            await hub.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("jtag", [False, True])
def test_discovery_cannot_replace_pending_explicit_policy(ports, jtag):
    async def scenario():
        hub = IrisHub("A", reconnect_min_seconds=0.005)
        try:
            with patch.object(SerialLink, "open", side_effect=open_board):
                await hub.add_usb("/dev/review-usb", usb_serial_jtag=jtag)
                await hub.add_usb("/dev/review-usb", discovered=True)
                ports.append(board(
                    vid=0x303A if jtag else 0x1234,
                    pid=0x1001 if jtag else 0x5678,
                    product="Custom",
                ))
                await until(lambda: bool(hub.list_devices()))
                assert hub.list_endpoints()[0]["usb_selection"] == "explicit"
        finally:
            await hub.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("pid,allow_jtag", [(0x1001, False), (0x0020, False), (0x0020, True)])
def test_explicit_selection_still_rejects_unapproved_download_interfaces(ports, pid, allow_jtag):
    async def scenario():
        ports.append(board(pid=pid, product="ROM or JTAG"))
        hub = IrisHub("A", reconnect_min_seconds=0.005)
        try:
            with patch.object(SerialLink, "open") as opened:
                await hub.add_usb(ports[0].device, usb_serial_jtag=allow_jtag)
                await until(lambda: hub.list_endpoints()[0]["attempt"] >= 2)
                opened.assert_not_called()
        finally:
            await hub.close()
    asyncio.run(scenario())




def test_serial_jtag_opt_in_survives_discovery_refresh(ports):
    async def scenario():
        current = board()
        ports.append(current)
        hub = IrisHub("A", reconnect_min_seconds=0.005)
        links = []

        async def open_link(path, **kwargs):
            link = await open_board(path, **kwargs)
            links.append(link)
            return link

        try:
            with patch.object(SerialLink, "open", side_effect=open_link) as opened:
                await hub.add_usb(current.device, usb_serial_jtag=True)
                await until(lambda: bool(hub.list_devices()))
                await hub.add_usb(current.device, discovered=True)
                current.pid, current.product = 0x1001, "USB JTAG/serial debug unit"
                await links[0].incoming.put(b"")
                await until(lambda: len(links) == 2)
                assert opened.call_args.kwargs["hupcl"] is False
        finally:
            await hub.close()
    asyncio.run(scenario())
