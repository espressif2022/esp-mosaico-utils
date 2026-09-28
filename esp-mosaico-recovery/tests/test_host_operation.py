from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from mosaico_cli import commands, gateway
from mosaico_cli.errors import DeviceError, OperationError
from test_cli import REPOSITORY, WORKSPACE


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "waiting"])
def test_cli_records_operation_and_never_cancels_a_writer_on_timeout(tmp_path, monkeypatch, outcome):
    session = gateway.GatewaySession(Path("python"), Path("iris"),
                                     ("--url", "http://127.0.0.1:8443"), None, False)
    context = SimpleNamespace(directory=tmp_path, note=Mock(), status=Mock())
    submitted = []

    def request(context, session, command, *args, **kwargs):
        if command == "health":
            return {"capabilities": ["local-host-operations/v1"]}
        if command == "host-operation":
            path = Path(args[0])
            if os.name != "nt":
                assert path.stat().st_mode & 0o077 == 0
            submitted.append(path)
            assert json.loads(path.read_text())["commands"][0]["env"]["PRIVATE"] == "secret"
            return {"operation": {"operation_id": "rom-1", "status": "running"}}
        assert command == "operation-status"
        return {"operation_id": "rom-1", "status": outcome,
                "error": "write failed", "result": {"stdout": "finished"}}

    monkeypatch.setattr(gateway, "gateway_json", request)
    spec = {"commands": [{"argv": ["test-writer"], "env": {"PRIVATE": "secret"}}]}
    if outcome == "succeeded":
        assert gateway.run_host_operation(context, session, spec, timeout=2)["operation_id"] == "rom-1"
    else:
        with pytest.raises(DeviceError if outcome == "waiting" else OperationError):
            gateway.run_host_operation(context, session, spec, timeout=0 if outcome == "waiting" else 2)
    assert json.loads((tmp_path / "host-operation.json").read_text()) == {"operation_id": "rom-1"}
    assert not submitted[0].exists()
    assert "secret" not in str(context.note.call_args_list)


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_recover_prepares_then_submits_one_operation(tmp_path, monkeypatch, managed, failed):
    arguments = SimpleNamespace(model=None, source="reviewed", device_id="a" if managed else None,
                                gateway_profile=None, timeout=180, dry_run=False)
    context = Mock(workspace=WORKSPACE, repository=REPOSITORY, directory=tmp_path, log_path=tmp_path / "run.log")
    session = SimpleNamespace(started_local=False)
    before = {"device_id": "a", "boot_id": "before"}
    after = {"device_id": "a", "boot_id": "after", "app_version": "0.1",
             "firmware_mode": "recovery", "capability_names": ["ota"]}
    prepare = Mock()
    execute = Mock(side_effect=OperationError("write failed") if failed else None,
                   return_value={"operation_id": "rom-1", "evidence": {"verification": after}})
    record = Mock()
    for name, value in {
        "resolve_idf_path": Mock(return_value=Path("/idf")),
        "load_bundle": Mock(return_value={"version": "0.1", "images": {"recovery": {}}}),
        "ensure_gateway": Mock(return_value=session),
        "connected_devices": Mock(return_value=[before] if managed else []),
        "provisioning_candidate": Mock(return_value="/dev/test-rom"),
        "run_idf_target": prepare,
        "idf_target_command": Mock(return_value={"argv": ["test-flash"], "env": {"ESPPORT": "{port}"}}),
        "run_host_operation": execute,
        "record_recovery_verification": record,
    }.items():
        monkeypatch.setattr(commands, name, value)
    if failed:
        with pytest.raises(OperationError, match="write failed"):
            commands.recover(arguments, context)
        record.assert_not_called()
    else:
        result = commands.recover(arguments, context)
        assert result["operation_id"] == "rom-1"
        record.assert_called_once_with("a", "0.1", "after")
    assert [call.kwargs["target"] for call in prepare.call_args_list] == ["mosaico-recover-prepare"]
    execute.assert_called_once()
    spec = execute.call_args.args[2]
    assert spec["device_id"] == ("a" if managed else None)
    assert spec["endpoint"] == (None if managed else "/dev/test-rom")
    assert spec["action"] == "host.recovery"
    assert spec["commands"][0]["env"]["ESPPORT"] == "{port}"
