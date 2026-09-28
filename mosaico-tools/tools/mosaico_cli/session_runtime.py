"""Shared project Gateways with independently renewable client leases."""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import (
    DeviceError,
    EnvironmentError,
    GatewayNotRunningError,
    SelectionError,
)
from .host import state_root
from .iris import host_api
from .project import resolve_project

CURRENT_SCOPE: ContextVar[Any] = ContextVar("mosaico_project_scope", default=None)
LIFECYCLE_CAPABILITY = "project-client-lifecycle/v1"


class GatewayDraining(DeviceError):
    pass


class ClientLease:
    def __init__(self, url: str, record: dict[str, Any], args: Any) -> None:
        self.url = url
        self.body = {"session_id": record["session_id"], "client_id": str(uuid.uuid4()),
                     "lease_token": secrets.token_hex(32), "pid": os.getpid(),
                     "kind": "run" if getattr(args, "session_action", None) == "run" else "cli",
                     "command": getattr(args, "public_command", None) or getattr(args, "command", "cli")}
        result = request(url, "/v1/project/clients", self.body, timeout=3)
        self.interval = float(result["renew_seconds"])
        self.duration = float(result["lease_seconds"])
        self.last_success = time.monotonic()
        self.lost = False
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._renew, daemon=True, name="mosaico-client-lease")
        self.thread.start()

    def _renew(self) -> None:
        while not self.stopped.wait(self.interval):
            try:
                request(self.url, f"/v1/project/clients/{self.body['client_id']}/renew", self.body, timeout=2)
                self.last_success = time.monotonic()
            except DeviceError:
                if time.monotonic() - self.last_success >= self.duration - self.interval:
                    self.lost = True
                    return

    def check(self) -> None:
        if self.lost:
            raise DeviceError("Gateway client lease was lost; inspect the original operation before retrying. No write was replayed.")

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=3)
        with contextlib.suppress(DeviceError):
            request(self.url, f"/v1/project/clients/{self.body['client_id']}/release", self.body, timeout=2)


def read_pairing_token(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) != 64 or len(bytes.fromhex(value)) != 32:
            raise ValueError("invalid token length")
        return value
    except (OSError, ValueError) as error:
        raise EnvironmentError(f"Pairing token file must contain 64 hexadecimal characters: {path}") from error


def request(url: str, path: str, body: dict[str, Any] | None = None,
            *, timeout: float = 20) -> dict[str, Any]:
    raw = json.dumps(body).encode() if body is not None else None
    req = Request(url + path, data=raw, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            details = json.loads(error.read())
        except (ValueError, OSError):
            details = {}
        remote_error = details.get("error") if isinstance(details, dict) else None
        if isinstance(remote_error, dict) and remote_error.get("code") == "selection_error":
            raise SelectionError(remote_error.get("message", "Select a device."),
                                 details=remote_error.get("details", {})) from error
        raise DeviceError(f"Project Gateway rejected the request: {details or error.reason}",
                          details={"remote_error": remote_error} if isinstance(remote_error, dict) else {}) from error
    except (URLError, OSError, ValueError) as error:
        raise DeviceError(f"Project Gateway is unavailable: {error}") from error


def acquire_device(url: str, arguments: Any, context: Any, *, allow_none: bool = False) -> dict[str, Any] | None:
    """Resolve and pin one live device for the rest of this command."""
    selected = getattr(arguments, "device_id", None)
    endpoint = getattr(arguments, "endpoint", None)
    body = {"device_id": selected, "endpoint": endpoint,
            "auto": not (selected or endpoint), "allow_none": allow_none}
    token_file = getattr(arguments, "pairing_token_file", None)
    if token_file:
        body["pairing_token"] = read_pairing_token(Path(token_file))
    device = request(url, "/v1/project/acquire", body, timeout=35)["device"]
    if device is not None:
        arguments.device_id = device["device_id"]
        context.status(f"device: {'selected' if selected or endpoint else 'automatically selected'} "
                       f"{device['device_id']} endpoint={device.get('endpoint', '')}")
    return device


class SessionScope:
    def __init__(self) -> None:
        self.arguments: Any = None
        self.sessions: dict[str, Any] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.processes: list[tuple[subprocess.Popen, str, Path]] = []
        self.info: dict[str, Any] = {}
        self.leases: dict[str, ClientLease] = {}
        self.admitted: set[str] = set()
        self.local_projects: dict[str, Any] = {}

    def gateway(self, context: Any, python: Path, script: Path, revision: str, *, start: bool = True,
                select: bool = True) -> Any:
        deadline = time.monotonic() + 20
        while True:
            try:
                return self._gateway(context, python, script, revision, start=start, select=select)
            except GatewayDraining:
                if time.monotonic() >= deadline:
                    raise DeviceError("Project Gateway is still draining; inspect 'iris status' and retry when it exits.")
                time.sleep(0.1)

    def _gateway(self, context: Any, python: Path, script: Path, revision: str, *, start: bool,
                 select: bool) -> Any:
        from .gateway import GatewaySession, _require_compatible_gateway

        args = self.arguments
        project = resolve_project(context.workspace, getattr(args, "project", None), Path.cwd())
        api = host_api(context.workspace)
        local = api.LocalProject(state_root("esp-mosaico"), context.workspace.root, project)
        expected_source = (api.source_identity(context.workspace.esp_iris_path)
                           if context.workspace.gateway_source_policy == "exact" else None)
        project_key = local.project_id
        self.local_projects[project_key] = local
        if project_key in self.sessions:
            if project_key in self.leases:
                self.leases[project_key].check()
            if select and start:
                self._admit(project_key, self.records[project_key]["url"], context)
            return self.sessions[project_key]
        directory = local.directory
        if not start and not directory.exists():
            raise GatewayNotRunningError("This project's Gateway is not running; start 'iris run' first.")
        connection_file = local.connection_file
        with local.starting():
            existing = None
            try:
                record = local.connection()
                health = request(record["url"], "/v1/health", timeout=2)
                live = health.get("project_session") or {}
                if (live.get("session_id"), live.get("project_id"), live.get("instance_id")) != (
                    record["session_id"], project_key, record["instance_id"],
                ):
                    raise EnvironmentError("Project Gateway identity differs from the connection record")
                if start:
                    _require_compatible_gateway(health, expected_source=expected_source)
                existing = live
            except (OSError, ValueError, KeyError, DeviceError):
                pass
            if existing:
                if start and LIFECYCLE_CAPABILITY not in health.get("capabilities", []):
                    raise EnvironmentError("Existing Gateway uses the legacy owner lifetime; end its original session before using shared clients.")
                if start and (health.get("lifecycle") or {}).get("state") == "draining":
                    raise GatewayDraining("Gateway is draining")
                record = existing
                started = False
            else:
                # A failed HTTP probe is not proof that the owner died. The
                # Gateway holds this lock for its entire process lifetime.
                if local.running():
                    raise EnvironmentError("This project's Gateway is alive but unavailable; inspect its log before retrying")
                if not start:
                    raise GatewayNotRunningError("This project's Gateway is not running; start 'iris run' first.")
                session_id = str(uuid.uuid4())
                instance_id = "project-" + session_id
                local.discard_stopped_connection()
                environment = os.environ.copy()
                environment["ESP_IRIS_SOURCE_REVISION"] = revision
                token_file = getattr(args, "pairing_token_file", None)
                if token_file:
                    environment["ESP_IRIS_PAIRING_TOKEN"] = read_pairing_token(Path(token_file))
                # Decoder context must not silently come from another project.
                from .host import HostEnvironmentError, prepare_idf_environment
                from .runtime import resolve_idf_path
                try:
                    prepared = prepare_idf_environment(resolve_idf_path(context.workspace, project), base_environment=environment)
                    environment.update(prepared.values)
                    environment["ESP_IRIS_IDF_PATH"] = str(prepared.root)
                    environment["ESP_IRIS_IDF_PYTHON"] = str(prepared.python)
                except (EnvironmentError, HostEnvironmentError):
                    pass
                log = local.log_file.open("a", encoding="utf-8")
                try:
                    process = subprocess.Popen([
                        str(python), str(script), "web", "--listen", "127.0.0.1", "--port", "0",
                        "--instance-id", instance_id, *local.launch_arguments(session_id), "--no-tls",
                    ], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        env=environment, close_fds=True,
                        **({"start_new_session": True} if os.name != "nt" else {
                            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                        }))
                except OSError as error:
                    raise EnvironmentError(f"Could not launch this project's Gateway: {error}") from error
                finally:
                    log.close()
                self.processes.append((process, "", connection_file))
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise EnvironmentError(f"Project Gateway exited; inspect {directory / 'gateway.log'}")
                        try:
                            record = local.connection()
                            if record["session_id"] == session_id:
                                health = request(record["url"], "/v1/health", timeout=2)
                                _require_compatible_gateway(health, expected_source=expected_source)
                                break
                        except (OSError, ValueError, KeyError, DeviceError):
                            pass
                        time.sleep(0.1)
                    else:
                        raise EnvironmentError("Project Gateway startup timed out")
                except BaseException:
                    # Before publication no client can discover this launch,
                    # and its HTTP idle watchdog may not have started. Reap
                    # only our unpublished child; ready Gateways own lifetime.
                    if not connection_file.exists() and process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                    raise
                self.processes[-1] = (process, record["url"], connection_file)
                started = True
            if start:
                try:
                    self.leases[project_key] = ClientLease(record["url"], record, args)
                except DeviceError:
                    # Only admission may retry; no business request has run yet.
                    try:
                        state = request(record["url"], "/v1/health", timeout=2)
                    except DeviceError:
                        raise GatewayDraining("Gateway exited during client admission")
                    if (state.get("lifecycle") or {}).get("state") == "draining":
                        raise GatewayDraining("Gateway started draining during client admission")
                    raise
            result = GatewaySession(python, script, ("--url", record["url"]), None, started)
            self.sessions[project_key] = result
            self.records[project_key] = dict(record)
            self.info = record
        if start:
            action = "created shared" if started else "reusing shared"
            context.status(f"gateway: project={project} {action} Gateway at {record['url']}")
        if select and start:
            self._admit(project_key, record["url"], context)
        return result

    def _admit(self, project_key: str, url: str, context: Any) -> None:
        if project_key in self.admitted:
            return
        args = self.arguments
        selected = getattr(args, "device_id", None)
        endpoint = getattr(args, "endpoint", None)
        command = getattr(args, "command", "")
        # Discovery/status stay passive. Ownership management has separate
        # selection rules. A ROM hardware MAC is never an implicit USB selector.
        automatic = command in {
            "monitor", "memory", "crash", "rpc", "install", "system-update",
            "enter-recovery", "recovery-wifi", "bridge-code", "recover",
        } and not getattr(args, "hardware_mac", None)
        if command not in {"device", "list"} and (selected or endpoint or automatic):
            acquire_device(url, args, context, allow_none=command == "recover")
        self.admitted.add(project_key)

    def finished_operation(self, session: Any, operation_id: str) -> dict[str, Any] | None:
        """Read committed evidence after this local Gateway has stopped.

        The owner may finish draining between a follower's status polls. Never
        restart a Gateway or replay a write just to obtain its final result.
        """
        project_key = next((key for key, value in self.sessions.items() if value is session), None)
        if project_key is None:
            return None  # Remote profiles have no registered local store.
        local = self.local_projects.get(project_key)
        return local.finished_operation(self.records[project_key], operation_id) if local else None

    def close(self) -> None:
        for lease in self.leases.values():
            lease.close()
        self.leases.clear()
