"""Real Gateway subprocess tests; discovery is passive and no device is opened."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / "tools"))

from mosaico_cli.errors import DeviceError, EnvironmentError
from mosaico_cli.runtime import RunContext
from mosaico_cli.session_runtime import SessionScope, request
from mosaico_cli.workspace import load_workspace


def workspace(tmp_path):
    for name in ("a", "b"):
        project = tmp_path / "projects" / name
        project.mkdir(parents=True)
        (project / "CMakeLists.txt").write_text("include($ENV{IDF_PATH}/tools/cmake/project.cmake)\nproject(test)\n")
    (tmp_path / ".mosaico.json").write_text(json.dumps({
        "schema_version": 1,
        "workspace": {"projects_dir": "projects", "default_project": "projects/a", "run_dir": ".runs"},
        "dependencies": {"bsp": "bsp", "esp_iris": str(TOOLS.parent / "ESP-Iris")},
        "build": {"runner": "builtin"},
        "devices": [{"id": "esp-mosaico", "target": "esp32s31"}],
    }))
    return load_workspace(TOOLS, start=tmp_path)


def scope(project, *, persistent=True):
    value = SessionScope()
    value.arguments = Namespace(project=str(project), command="session" if persistent else "list",
                                session_action="run" if persistent else None, device_id=None, endpoint=None)
    return value


def stop_scopes(*scopes):
    for value in scopes:
        value.close()
    for value in scopes:
        for process, _, _ in value.processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_unpublished_startup_timeout_reaps_only_its_child(tmp_path, monkeypatch):
    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    work = workspace(tmp_path)
    value = scope(tmp_path / "projects/a")
    process = Mock()
    process.poll.return_value = None
    clock = Mock()
    clock.monotonic.side_effect = [0, 21, 42]
    with patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")), \
            patch("mosaico_cli.session_runtime.subprocess.Popen", return_value=process), \
            patch("mosaico_cli.session_runtime.time", clock), \
            pytest.raises(EnvironmentError, match="startup timed out"):
        value.gateway(RunContext(work, "test", json_output=True), Path(sys.executable),
                      work.esp_iris_path / "components/esp_iris/tools/esp_iris.py", "test-revision")
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=3)
    assert not value.leases


def test_two_projects_reuse_and_creator_lifetime(tmp_path, monkeypatch):
    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    work = workspace(tmp_path)
    script = work.esp_iris_path / "components/esp_iris/tools/esp_iris.py"
    a, b, follower = scope(tmp_path / "projects/a"), scope(tmp_path / "projects/b"), scope(tmp_path / "projects/a")
    context = RunContext(work, "test", json_output=True)
    # This test exercises process/network lifetime, not ESP-IDF setup.
    with patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")):
        try:
            first = a.gateway(context, Path(sys.executable), script, "test-revision")
            second = b.gateway(context, Path(sys.executable), script, "test-revision")
            same = follower.gateway(context, Path(sys.executable), script, "test-revision")
            assert first.connection_args != second.connection_args
            assert same.connection_args == first.connection_args
            assert first.started_local and not same.started_local
            incompatible = scope(tmp_path / "projects/a")
            compatible = incompatible.gateway(context, Path(sys.executable), script, "different-revision")
            assert compatible.connection_args == first.connection_args
            assert not incompatible.processes
            incompatible.close()
            strict = scope(tmp_path / "projects/a")
            strict_context = RunContext(replace(work, gateway_source_policy="exact"), "exact", json_output=True)
            try:
                exact = strict.gateway(strict_context, Path(sys.executable), script, "different-revision")
                assert exact.connection_args == first.connection_args
            finally:
                strict.close()
            mismatch = scope(tmp_path / "projects/a")
            with patch("iris_gateway.client.source_identity", return_value={
                "algorithm": "sha256-runtime-v1", "fingerprint": "changed-runtime",
            }), pytest.raises(EnvironmentError, match="was not stopped"):
                mismatch.gateway(strict_context, Path(sys.executable), script, "test-revision")
            assert not mismatch.processes
            assert a.processes[0][0].poll() is None
            unavailable = scope(tmp_path / "projects/a")
            with patch("mosaico_cli.session_runtime.request", side_effect=DeviceError("probe timed out")), \
                    pytest.raises(EnvironmentError, match="alive but unavailable"):
                unavailable.gateway(context, Path(sys.executable), script, "test-revision")
            assert not unavailable.processes
            assert a.processes[0][2].exists()
            follower.close()
            assert request(first.connection_args[1], "/v1/health")["ready"]
            assert request(first.connection_args[1], "/v1/devices")["devices"] == []
            a.close()
            assert request(second.connection_args[1], "/v1/health")["ready"]
        finally:
            stop_scopes(a, b, follower)


def test_shared_session_reused_and_creator_exit_does_not_stop_follower(tmp_path, monkeypatch):
    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    work = workspace(tmp_path)
    script = work.esp_iris_path / "components/esp_iris/tools/esp_iris.py"
    owner = scope(tmp_path / "projects/a", persistent=False)
    follower = scope(tmp_path / "projects/a")
    context = RunContext(work, "test", json_output=True)
    with patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")):
        try:
            first = owner.gateway(context, Path(sys.executable), script, "test-revision")
            second = follower.gateway(context, Path(sys.executable), script, "test-revision")
            assert second.connection_args == first.connection_args
            assert not second.started_local
            before = request(first.connection_args[1], "/v1/project")
            assert {item["kind"] for item in before["lifecycle"]["clients"]} == {"cli", "run"}
            owner.close()
            time.sleep(0.2)
            assert owner.processes[0][0].poll() is None
            after = request(first.connection_args[1], "/v1/project")
            assert len(after["lifecycle"]["clients"]) == 1
            assert after["lifecycle"]["clients"][0]["kind"] == "run"
            assert after["lifecycle"]["idle_remaining_seconds"] is None
            record = owner.processes[0][2]
            follower.close()
            deadline = time.monotonic() + 14
            # Passive polling must not delay the ten-second idle shutdown.
            started = time.monotonic()
            while owner.processes[0][0].poll() is None and time.monotonic() < deadline:
                try:
                    request(first.connection_args[1], "/v1/project", timeout=1)
                except DeviceError:
                    pass
                time.sleep(0.2)
            assert owner.processes[0][0].poll() == 0
            assert time.monotonic() - started >= 9.5
            assert not record.exists()
        finally:
            stop_scopes(owner, follower)


def test_public_list_handles_passive_discovery_without_connected_devices(tmp_path, monkeypatch, capsys):
    from mosaico_cli.cli import _print_device_table, main

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    work = workspace(tmp_path)
    script = work.esp_iris_path / "components/esp_iris/tools/esp_iris.py"
    with patch("mosaico_cli.gateway.ensure_iris_tools", return_value=(Path(sys.executable), script)), \
            patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")):
        assert main(["--workspace", str(tmp_path), "list"], tool_root=TOOLS) == 0
    assert "DEVICE_ID" in capsys.readouterr().out
    records = list((tmp_path / "state").rglob("connection.json"))
    assert len(records) == 1
    value = json.loads(records[0].read_text())
    snapshot = request(value["url"], "/v1/project")
    assert snapshot["lifecycle"]["clients"] == []
    assert snapshot["lifecycle"]["idle_timeout_seconds"] == 10
    _print_device_table({"devices": [], "endpoints": [{"endpoint": "tcp:127.0.0.1:1234"}]}, False)
    assert "Endpoint: tcp:127.0.0.1:1234" in capsys.readouterr().out


def test_follower_reads_committed_result_after_owner_exits(tmp_path, monkeypatch):
    from mosaico_cli.gateway import _wait_gateway_operation
    from mosaico_cli.session_runtime import CURRENT_SCOPE

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    work = workspace(tmp_path)
    script = work.esp_iris_path / "components/esp_iris/tools/esp_iris.py"
    owner, follower = scope(tmp_path / "projects/a"), scope(tmp_path / "projects/a")
    context = RunContext(work, "test", json_output=True)
    with patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")):
        try:
            owner.gateway(context, Path(sys.executable), script, "test-revision")
            session = follower.gateway(context, Path(sys.executable), script, "test-revision")
            from iris_gateway.store import GatewayStore

            store = GatewayStore(owner.processes[0][2].parent / "state")
            try:
                accepted, _ = store.create_operation({
                    "operation_id": "finished-install", "device_id": "test-device",
                    "actor_type": "local", "actor_name": "test", "action": "firmware.ota",
                    "status": "queued", "created_ns": time.time_ns(),
                })
                store.update_operation("finished-install", status="succeeded", finished_ns=time.time_ns(),
                                       result_json={"healthy": True, "boot_id": 18446744073709551614})
            finally:
                store.close()
            # A live owner, even one with a terminal operation, must not be bypassed.
            assert follower.finished_operation(session, "finished-install") is None
            stop_scopes(owner, follower)
            token = CURRENT_SCOPE.set(follower)
            try:
                result = _wait_gateway_operation(context, session,
                    result=subprocess.CompletedProcess([], 0, json.dumps({"operation": accepted})),
                    started=time.monotonic(), timeout=15, action="Installation", progress_prefix="ota")
            finally:
                CURRENT_SCOPE.reset(token)
            assert result["operation"]["status"] == "succeeded"
            assert result["operation"]["result"]["healthy"] is True
            assert result["operation"]["result"]["boot_id_text"] == "18446744073709551614"
            assert not follower.processes  # No restarted Gateway and no replay.
        finally:
            stop_scopes(owner, follower)


@pytest.mark.parametrize("status,created_ns,finished_ns,expected", [
    ("succeeded", 11, 12, True),
    ("failed", 11, 12, True),
    ("reconnecting", 11, None, False),
    ("succeeded", 9, 12, False),
    ("succeeded", 11, None, False),
])
def test_finished_operation_requires_this_session_and_terminal_evidence(
    tmp_path, monkeypatch, status, created_ns, finished_ns, expected,
):
    monkeypatch.syspath_prepend(str(TOOLS.parent / "ESP-Iris/components/esp_iris/tools"))
    from iris_gateway.client import LocalProject
    from iris_gateway.store import GatewayStore

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / name)
    follower = SessionScope()
    session = object()
    local = LocalProject(tmp_path / "esp-mosaico", tmp_path, tmp_path / "project")
    follower.sessions[local.project_id] = session
    follower.records[local.project_id] = {"project_id": local.project_id, "created_ns": 10}
    follower.local_projects[local.project_id] = local
    store = GatewayStore(local.directory / "state")
    try:
        store.create_operation({"operation_id": "op", "device_id": "device", "actor_type": "local",
                                "actor_name": "test", "action": "firmware.ota", "status": status,
                                "created_ns": created_ns})
        store.update_operation("op", finished_ns=finished_ns)
    finally:
        store.close()
    assert (follower.finished_operation(session, "op") is not None) is expected
    assert follower.finished_operation(session, "missing") is None
    assert follower.finished_operation(object(), "op") is None


def test_status_without_gateway_does_not_bootstrap_or_spawn(tmp_path, monkeypatch, capsys):
    from mosaico_cli.cli import main

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    workspace(tmp_path)
    monkeypatch.setattr("mosaico_cli.gateway._pinned_source_revision", lambda path: "test-revision")
    with patch("mosaico_cli.gateway.ensure_iris_tools") as bootstrap, \
            patch("mosaico_cli.session_runtime.subprocess.Popen") as spawn:
        assert main(["--workspace", str(tmp_path), "iris", "status", "--json"], tool_root=TOOLS) == 0
    assert json.loads(capsys.readouterr().out) == {"running": False, "session": None}
    bootstrap.assert_not_called()
    spawn.assert_not_called()
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("action", [
    ["claim", "--endpoint", "usb:location=1-2"],
    ["takeover", "start", "--endpoint", "usb:location=1-2", "--force"],
    ["release", "--device-id", "board"],
    ["reconcile", "--device-id", "board"],
    ["takeover", "status", "--takeover-id", "34316aaf-5c53-49c0-9d71-44ad598f20ce"],
])
def test_ownership_without_gateway_requests_shared_start(tmp_path, monkeypatch, capsys, action):
    from mosaico_cli.cli import main

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    workspace(tmp_path)
    monkeypatch.setattr("mosaico_cli.gateway._pinned_source_revision", lambda path: "test-revision")
    with patch("mosaico_cli.gateway.ensure_iris_tools", side_effect=EnvironmentError("bootstrap requested")) as bootstrap, \
            patch("mosaico_cli.session_runtime.subprocess.Popen") as spawn:
        expected = 4 if action[:2] == ["takeover", "status"] else 3
        assert main(["--workspace", str(tmp_path), "iris", *action, "--json"], tool_root=TOOLS) == expected
    assert json.loads(capsys.readouterr().err)["error"] == ("gateway_not_running" if expected == 4 else "environment_error")
    assert bootstrap.call_count == (0 if expected == 4 else 1)
    spawn.assert_not_called()


@pytest.mark.parametrize("persistent", [True, False])
def test_status_observes_live_gateway_without_taking_its_lifetime(tmp_path, monkeypatch, capsys, persistent):
    from mosaico_cli.cli import main

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    work = workspace(tmp_path)
    script = work.esp_iris_path / "components/esp_iris/tools/esp_iris.py"
    owner = scope(tmp_path / "projects/a", persistent=persistent)
    with patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")), \
            patch("mosaico_cli.gateway._pinned_source_revision", return_value="test-revision"):
        try:
            session = owner.gateway(RunContext(work, "test", json_output=True), Path(sys.executable), script, "test-revision")
            with patch("mosaico_cli.gateway.ensure_iris_tools") as bootstrap, \
                    patch("mosaico_cli.session_runtime.subprocess.Popen") as spawn:
                assert main(["--workspace", str(tmp_path), "iris", "status", "--json"], tool_root=TOOLS) == 0
            state = json.loads(capsys.readouterr().out)
            assert state["running"] is True
            assert state["session"]["persistent"] == 1
            assert len(state["lifecycle"]["clients"]) == 1
            bootstrap.assert_not_called()
            spawn.assert_not_called()
            assert owner.processes[0][0].poll() is None
            assert request(session.connection_args[1], "/v1/health")["project_session"]["session_id"] == state["session"]["session_id"]
        finally:
            stop_scopes(owner)
        # A stopped session's retained state also must not trigger startup.
        with patch("mosaico_cli.session_runtime.subprocess.Popen") as spawn:
            assert main(["--workspace", str(tmp_path), "iris", "status", "--json"], tool_root=TOOLS) == 0
        assert json.loads(capsys.readouterr().out)["running"] is False
        spawn.assert_not_called()


def test_parallel_commands_create_one_shared_gateway(tmp_path, monkeypatch):
    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    work = workspace(tmp_path)
    script = work.esp_iris_path / "components/esp_iris/tools/esp_iris.py"
    scopes = [scope(tmp_path / "projects/a", persistent=False) for _ in range(3)]
    with patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")):
        try:
            def connect(value):
                return value.gateway(RunContext(work, "parallel", json_output=True), Path(sys.executable), script, "test-revision")
            with ThreadPoolExecutor(max_workers=3) as pool:
                sessions = list(pool.map(connect, scopes))
            assert len({item.connection_args for item in sessions}) == 1
            assert sum(item.started_local for item in sessions) == 1
            state = request(sessions[0].connection_args[1], "/v1/project")
            assert len(state["lifecycle"]["clients"]) == 3
        finally:
            stop_scopes(*scopes)


def test_all_status_without_local_gateway_sees_other_workspace(tmp_path, monkeypatch, capsys):
    import sqlite3

    from mosaico_cli.cli import main

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    first = workspace(tmp_path / "workspace-a")
    workspace(tmp_path / "workspace-b")
    script = first.esp_iris_path / "components/esp_iris/tools/esp_iris.py"
    owner = scope(first.root / "projects/a")
    with patch("mosaico_cli.runtime.resolve_idf_path", side_effect=EnvironmentError("no IDF needed")):
        try:
            owner.gateway(RunContext(first, "test", json_output=True), Path(sys.executable), script, "test-revision")
            # A synthetic ownership row, without opening any hardware endpoint.
            with sqlite3.connect(tmp_path / "state/esp-mosaico/ownership/ownership.sqlite3") as db:
                db.execute("INSERT INTO claims VALUES(?,?,?,'owned',?,'{}',NULL)",
                           ("device:fixture", owner.info["session_id"], 1, "fixture"))
            with patch("mosaico_cli.gateway.ensure_iris_tools") as bootstrap, \
                    patch("mosaico_cli.session_runtime.subprocess.Popen") as spawn:
                assert main(["--workspace", str(tmp_path / "workspace-b"), "iris", "status", "--all", "--json"], tool_root=TOOLS) == 0
                result = json.loads(capsys.readouterr().out)
                assert len(result["gateways"]) == 1
                gateway = result["gateways"][0]
                assert gateway["session"]["workspace_path"] == str(first.root)
                assert gateway["device_ids"] == ["fixture"]
                assert gateway["reachable"]
                assert len(gateway["lifecycle"]["clients"]) == 1
                assert "lease_token" not in json.dumps(result)
                bootstrap.assert_not_called()
                spawn.assert_not_called()
                assert main(["--workspace", str(tmp_path / "workspace-b"), "iris", "status", "--json"], tool_root=TOOLS) == 0
                assert json.loads(capsys.readouterr().out) == {"running": False, "session": None}
        finally:
            stop_scopes(owner)


def test_empty_global_query_does_not_create_state_or_resolve_project(tmp_path, monkeypatch, capsys):
    from mosaico_cli.cli import main

    monkeypatch.setattr("mosaico_cli.session_runtime.state_root", lambda name: tmp_path / "state" / name)
    workspace(tmp_path)
    with patch("mosaico_cli.gateway_status.resolve_project") as project, \
            patch("mosaico_cli.gateway.ensure_iris_tools") as bootstrap:
        assert main(["--workspace", str(tmp_path), "iris", "status", "--all", "--json"], tool_root=TOOLS) == 0
    assert json.loads(capsys.readouterr().out) == {"gateways": []}
    assert not (tmp_path / "state").exists()
    project.assert_not_called()
    bootstrap.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_takeover_cli_submits_to_receiver_and_retains_id_on_lost_response(tmp_path, monkeypatch, capsys, fails):
    from mosaico_cli.cli import main

    workspace(tmp_path)
    takeover_id = "34316aaf-5c53-49c0-9d71-44ad598f20ce"
    session = Mock(connection_args=("--url", "http://127.0.0.1:10000"))
    result = {"takeover": {"takeover_id": takeover_id, "state": "completed"}}
    with patch("mosaico_cli.gateway.ensure_gateway", return_value=session), \
            patch("mosaico_cli.session_runtime.request", side_effect=DeviceError("lost response") if fails else None,
                  return_value=result) as request_mock:
        status = main(["--workspace", str(tmp_path), "iris", "takeover", "start", "--project", "projects/b",
                       "--endpoint", "/dev/ttyACM0", "--force", "--timeout", "60",
                       "--takeover-id", takeover_id, "--json"], tool_root=TOOLS)
    request_mock.assert_called_once_with("http://127.0.0.1:10000", "/v1/project/takeovers", {
        "device_id": None, "endpoint": "/dev/ttyACM0", "force": True,
        "timeout": 60, "takeover_id": takeover_id,
    }, timeout=105)
    output = capsys.readouterr()
    if fails:
        assert status != 0
        assert json.loads(output.err)["details"]["takeover_id"] == takeover_id
    else:
        assert status == 0 and json.loads(output.out) == result


@pytest.mark.parametrize("action", ["status", "resume", "abort", "reconcile"])
def test_takeover_lifecycle_commands_use_record_routes(tmp_path, capsys, action):
    from mosaico_cli.cli import main

    workspace(tmp_path)
    takeover_id = "34316aaf-5c53-49c0-9d71-44ad598f20ce"
    session = Mock(connection_args=("--url", "http://127.0.0.1:10000"))
    with patch("mosaico_cli.gateway.ensure_gateway", return_value=session) as ensure, \
            patch("mosaico_cli.session_runtime.request", return_value={"takeover": {}}) as http:
        assert main(["--workspace", str(tmp_path), "iris", "takeover", action,
                     "--takeover-id", takeover_id, "--json"], tool_root=TOOLS) == 0
    assert ensure.call_args.kwargs["start"] is (action != "status")
    path = "/v1/project/takeovers/" + takeover_id
    if action == "status":
        http.assert_called_once_with("http://127.0.0.1:10000", path)
    else:
        http.assert_called_once_with("http://127.0.0.1:10000", path + "/" + action, {}, timeout=35)
    assert json.loads(capsys.readouterr().out) == {"takeover": {}}


@pytest.mark.parametrize("argv", [
    ["iris", "transfer", "start"], ["iris", "transfer", "status"],
    ["device", "transfer"], ["device", "transfer-status"],
    ["iris", "takeover", "--endpoint", "/dev/ttyACM0"],
    ["iris", "takeover", "status", "--transfer-id", "old"],
    ["iris", "takeover", "status", "--takeover-id", "invalid"],
])
def test_removed_transfer_syntax_has_no_compatibility_alias(argv):
    from mosaico_cli.cli import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2
