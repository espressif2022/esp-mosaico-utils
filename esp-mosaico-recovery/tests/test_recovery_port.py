from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_cli import REPOSITORY, WORKSPACE

# isort: split
# The shared fixture module adds the repository-local tools package to sys.path.
from mosaico_cli import commands
from mosaico_cli.cli import build_parser
from mosaico_cli.errors import DeviceError, OperationError, SelectionError
from mosaico_cli.recovery_port import serial_jtag_candidate


def _check_explicit_port_rejects_missing_ambiguous_or_wrong_interface(ports, requested, error):
    with mock.patch("serial.tools.list_ports.comports", return_value=ports), unittest.TestCase().assertRaises(error):
        serial_jtag_candidate(requested)


def _check_explicit_port_records_usb_identity():
    port = SimpleNamespace(device="COM14", vid=0x303A, pid=0x1001, serial_number="usb-serial", location="1-2")
    with mock.patch("serial.tools.list_ports.comports", return_value=[port]):
        assert serial_jtag_candidate("COM14") == {
            "path": "COM14", "vid": 0x303A, "pid": 0x1001,
            "serial_number": "usb-serial", "location": "1-2"}
    assert build_parser().parse_args(["recover", "--recovery-port", "COM14"]).recovery_port == "COM14"


def _check_independent_recovery_holds_both_endpoints_and_verifies_original_device(tmp_path, monkeypatch, failure):
    arguments = SimpleNamespace(model=None, source="reviewed", device_id="device-a", gateway_profile=None,
                                timeout=180, dry_run=failure == "dry_run", recovery_port="COM14")
    context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY, directory=tmp_path,
                        log_path=tmp_path / "run.log")
    session = SimpleNamespace(started_local=False)
    manifest = {"version": "0.1", "images": {"recovery": {}}}
    status = {"device_id": "other" if failure == "wrong_device" else "device-a",
              "boot_id": "old" if failure == "old_boot" else "new", "firmware_mode": "recovery",
              "app_version": "0.1", "capability_names": ["ota"]}
    identity = {"path": "COM14", "vid": 0x303A, "pid": 0x1001, "serial_number": "serial", "location": "1-2"}
    order = []

    def target(*args, **kwargs):
        order.append(kwargs["target"])

    def execute(context, session, spec, **kwargs):
        order.append("operation")
        assert spec["device_id"] == "device-a"
        assert spec["write_endpoint"] == "COM14"
        assert spec["expected_version"] == "0.1"
        assert spec["commands"] == [{"argv": ["test-flash"], "env": {"ESPPORT": "{port}"}}]
        if failure in {"busy", "flash", "wrong_owner"}:
            raise OperationError(failure)
        return {"operation_id": "recovery-1", "evidence": {"verification": status}}

    patches = {
        "load_bundle": mock.Mock(return_value=manifest),
        "ensure_gateway": mock.Mock(return_value=session),
        "connected_devices": mock.Mock(return_value=[] if failure == "no_live" else [{"device_id": "device-a", "boot_id": "old"}]),
        "gateway_json": mock.Mock(return_value={"device_id": "device-a", "boot_id": "old"}),
        "serial_jtag_candidate": mock.Mock(side_effect=[identity, {**identity, "serial_number": "changed"} if failure == "changed_port" else identity]),
        "resolve_idf_path": mock.Mock(return_value=Path("/idf")),
        "run_idf_target": mock.Mock(side_effect=target),
        "idf_target_command": mock.Mock(return_value={"argv": ["test-flash"], "env": {"ESPPORT": "{port}"}}),
        "run_host_operation": mock.Mock(side_effect=execute),
        "record_recovery_verification": mock.Mock(),
    }
    for name, replacement in patches.items():
        monkeypatch.setattr(commands, name, replacement)
    if failure == "dry_run":
        result = commands.recover(arguments, context)
        assert result["status"] == "dry_run"
        assert order == []
        patches["run_host_operation"].assert_not_called()
    elif failure:
        with unittest.TestCase().assertRaises((DeviceError, OperationError)):
            commands.recover(arguments, context)
        patches["record_recovery_verification"].assert_not_called()
        if failure in {"changed_port", "no_live"}:
            assert "operation" not in order
    else:
        result = commands.recover(arguments, context)
        assert result["status"] == "succeeded"
        assert result["device_id"] == "device-a"
        assert result["operation_id"] == "recovery-1"
        assert order == ["mosaico-recover-prepare", "operation"]
        patches["record_recovery_verification"].assert_called_once_with("device-a", "0.1", "new")


class RecoveryPortTests(unittest.TestCase):
    def test_explicit_port_with_multiple_boards(self):
        ports = [SimpleNamespace(device=name, vid=0x303A, pid=0x1001)
                 for name in ("COM14", "COM15")]
        with mock.patch("serial.tools.list_ports.comports", return_value=ports):
            assert serial_jtag_candidate("COM15")["path"] == "COM15"

    def test_unavailable_device_id_never_selects_or_writes_other_rom(self):
        arguments = SimpleNamespace(model=None, source="reviewed", device_id="missing-a",
                                    gateway_profile=None, timeout=180, dry_run=False)
        context = mock.Mock(workspace=WORKSPACE, repository=REPOSITORY)
        with mock.patch.object(commands, "load_bundle", return_value={"version": "0.1"}), \
                mock.patch.object(commands, "resolve_idf_path", return_value=Path("/idf")), \
                mock.patch.object(commands, "ensure_gateway"), \
                mock.patch.object(commands, "connected_devices", return_value=[]), \
                mock.patch.object(commands, "provisioning_candidate") as candidate, \
                mock.patch.object(commands, "run_idf_target") as write:
            with self.assertRaisesRegex(DeviceError, "refusing to select another"):
                commands.recover(arguments, context)
            candidate.assert_not_called()
            write.assert_not_called()

    def test_identity_and_parser(self):
        _check_explicit_port_records_usb_identity()


def _port_test(ports, requested, error):
    def check(self):
        _check_explicit_port_rejects_missing_ambiguous_or_wrong_interface(ports, requested, error)
    return check


for _name, _ports, _requested, _error in [
    ("missing", [], "COM14", SelectionError),
    ("ambiguous", [SimpleNamespace(device="COM14", vid=0x303A, pid=0x1001),
                   SimpleNamespace(device="COM14", vid=0x303A, pid=0x1001)], "COM14", SelectionError),
    ("wrong_path", [SimpleNamespace(device="COM14", vid=0x303A, pid=0x1001)], "COM15", DeviceError),
    ("wrong_pid", [SimpleNamespace(device="COM14", vid=0x303A, pid=0x1002)], "COM14", SelectionError),
]:
    setattr(RecoveryPortTests, "test_port_" + _name, _port_test(_ports, _requested, _error))


def _recovery_test(failure):
    def check(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            patcher = SimpleNamespace(setattr=lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value)))
            _check_independent_recovery_holds_both_endpoints_and_verifies_original_device(Path(folder), patcher, failure)
    return check


for _failure in [None, "dry_run", "no_live", "changed_port", "wrong_owner", "flash", "wrong_device", "old_boot", "busy"]:
    setattr(RecoveryPortTests, "test_recovery_" + str(_failure or "success"), _recovery_test(_failure))
