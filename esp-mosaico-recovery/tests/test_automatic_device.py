"""The CLI asks for auto admission only for operations that need a device."""
from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mosaico_cli.cli import _project_command, build_parser
from mosaico_cli.errors import DeviceError, SelectionError
from mosaico_cli.gateway import GatewaySession
from mosaico_cli.runtime import RunContext
from mosaico_cli.session_runtime import (
    LIFECYCLE_CAPABILITY,
    SessionScope,
    acquire_device,
)
from test_project_sessions import workspace


@pytest.mark.parametrize("command", [
    "monitor", "memory", "crash", "rpc", "install", "system-update",
    "enter-recovery", "recovery-wifi", "bridge-code", "recover",
])
def test_device_commands_auto_acquire_and_pin_the_returned_identity(tmp_path, monkeypatch, command):
    work = workspace(tmp_path)
    project = tmp_path / "projects/a"
    key = hashlib.sha256((str(work.root.resolve()) + "\0" + str(project)).encode()).hexdigest()
    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    directory = tmp_path / "state/esp-mosaico/project-sessions" / key
    directory.mkdir(parents=True)
    record = {"session_id": "owner", "project_id": key, "instance_id": "instance",
              "persistent": True, "url": "http://127.0.0.1:1234"}
    (directory / "connection.json").write_text(json.dumps(record))
    scope = SessionScope()
    scope.arguments = Namespace(command=command, project=str(project), device_id=None, endpoint=None)

    def request(url, path, body=None, **kwargs):
        if path == "/v1/health":
            return {"project_session": record, "capabilities": [LIFECYCLE_CAPABILITY]}
        assert path == "/v1/project/acquire"
        assert body["auto"] is True
        assert body["allow_none"] == (command == "recover")
        return {"device": {"device_id": "verified-device", "boot_id": 42}}

    with patch("mosaico_cli.session_runtime.request", side_effect=request) as http, \
            patch("mosaico_cli.gateway._require_compatible_gateway"), \
            patch("mosaico_cli.session_runtime.ClientLease"), \
            patch("mosaico_cli.session_runtime.subprocess.Popen") as spawn:
        scope.gateway(RunContext(work, "test", json_output=True), Path(sys.executable),
                      work.esp_iris_path / "components/esp_iris/tools/esp_iris.py", "revision")
    assert scope.arguments.device_id == "verified-device"
    assert http.call_count == 2
    spawn.assert_not_called()


@pytest.mark.parametrize("argv", [["iris", "status"], ["iris", "list"], ["recover", "--hardware-mac", "30:ed:a0:12:34:56"]])
def test_queries_and_explicit_rom_mac_do_not_auto_acquire(tmp_path, monkeypatch, argv):
    work = workspace(tmp_path)
    project = tmp_path / "projects/a"
    key = hashlib.sha256((str(work.root.resolve()) + "\0" + str(project)).encode()).hexdigest()
    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    directory = tmp_path / "state/esp-mosaico/project-sessions" / key
    directory.mkdir(parents=True)
    record = {"session_id": "owner", "project_id": key, "instance_id": "instance",
              "persistent": True, "url": "http://127.0.0.1:1234"}
    (directory / "connection.json").write_text(json.dumps(record))
    scope = SessionScope()
    scope.arguments = build_parser().parse_args([*argv, "--project", str(project)])
    with patch("mosaico_cli.session_runtime.request", return_value={"project_session": record, "capabilities": [LIFECYCLE_CAPABILITY]}) as http, \
            patch("mosaico_cli.gateway._require_compatible_gateway"), \
            patch("mosaico_cli.session_runtime.ClientLease"):
        scope.gateway(RunContext(work, "test", json_output=True), Path(sys.executable),
                      work.esp_iris_path / "components/esp_iris/tools/esp_iris.py", "revision")
    http.assert_called_once_with(record["url"], "/v1/health", timeout=2)


@pytest.mark.parametrize("error", [DeviceError("occupied"), SelectionError("multiple")])
def test_selection_failure_never_retries_another_target(error):
    arguments = Namespace(device_id="requested", endpoint=None)
    with patch("mosaico_cli.session_runtime.request", side_effect=error) as http, \
            pytest.raises(type(error)):
        acquire_device("http://gateway", arguments, Mock(), allow_none=True)
    http.assert_called_once()
    assert http.call_args.args[2]["auto"] is False
    assert arguments.device_id == "requested"


@pytest.mark.parametrize("created", [True, False])
def test_run_only_auto_connects_on_initial_start(created):
    arguments = build_parser().parse_args(["iris", "run", "--json"])
    session = GatewaySession(Path(sys.executable), Path("iris"), ("--url", "http://gateway"), None, created)
    with patch("mosaico_cli.gateway.ensure_gateway", return_value=session), \
            patch("mosaico_cli.session_runtime.acquire_device", return_value=None) as acquire, \
            patch("mosaico_cli.session_runtime.request", return_value={"session": {"session_id": "s"}}), \
            patch("mosaico_cli.cli.time.sleep", side_effect=KeyboardInterrupt):
        assert _project_command(arguments, Mock()) == 0
    assert acquire.call_count == int(created)


@pytest.mark.parametrize("argv,path", [
    (["iris", "claim"], "/v1/project/acquire"),
    (["iris", "release"], "/v1/project/release"),
])
def test_ownership_commands_can_omit_device_and_join_shared_gateway(argv, path):
    arguments = build_parser().parse_args(argv)
    session = GatewaySession(Path(sys.executable), Path("iris"), ("--url", "http://gateway"), None, False)
    context = Mock()
    with patch("mosaico_cli.gateway.ensure_gateway", return_value=session) as ensure, \
            patch("mosaico_cli.session_runtime.request", return_value={}) as http:
        assert _project_command(arguments, context) == 0
    ensure.assert_called_once_with(context, None, start=True)
    assert http.call_args.args[1] == path
    assert http.call_args.args[2]["auto"] is True


def test_takeover_and_reconcile_require_explicit_target():
    for argv in (["iris", "takeover", "start"], ["iris", "reconcile"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


def test_gateway_ambiguity_preserves_candidates_for_cli_errors():
    import io
    from urllib.error import HTTPError

    from mosaico_cli.session_runtime import request

    payload = {"error": {"code": "selection_error", "message": "Multiple USB devices",
                         "details": {"candidates": ["usb:location=a", "usb:location=b"]}}}
    failure = HTTPError("http://gateway", 400, "Bad Request", {}, io.BytesIO(json.dumps(payload).encode()))
    with patch("mosaico_cli.session_runtime.urlopen", side_effect=failure), \
            pytest.raises(SelectionError) as caught:
        request("http://gateway", "/v1/project/acquire", {"auto": True})
    assert caught.value.details["candidates"] == ["usb:location=a", "usb:location=b"]
    assert caught.value.exit_code == 2
