"""Implement the public ESP-Mosaico product commands."""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, TextIO

from .bundle_plan import inspect_bundle_plan
from .errors import (
    BuildError,
    DeviceError,
    EnvironmentError,
    OperationError,
    RecoveryRequiredError,
    SelectionError,
)
from .gateway import (
    connected_devices,
    ensure_gateway,
    ensure_iris_tools,
    enter_recovery_and_wait,
    gateway_devices,
    gateway_json,
    run_host_operation,
    run_ota,
    run_system_update_bundle,
    select_device,
)
from .project import discover_artifacts, partition_table_flash_sha256, resolve_project
from .recovery import (
    load_bundle,
    provisioning_candidate,
    read_rom_hardware_mac,
    record_recovery_build_defaults,
    record_recovery_verification,
    recovery_build_defaults_are_current,
    recovery_defaults_fingerprint,
    recovery_verification_details,
    rom_hardware_mac,
    rom_identity_command,
)
from .recovery_port import serial_jtag_candidate
from .registry import select_model
from .runtime import (
    RunContext,
    build_application,
    idf_target_command,
    resolve_idf_path,
    run_idf_target,
)

_IDF_MONITOR_LOG_PATTERN = re.compile(r"^(I|W|E) \([\d:\. -]+\)")
_IDF_MONITOR_COLORS = {
    "I": "\033[0;32m",
    "W": "\033[0;33m",
    "E": "\033[1;31m",
}
_ANSI_NORMAL = "\033[0m"

_RECOVERY_CONTROL_SERVICE_ID = "0x1202"
_RECOVERY_WIFI_CONNECT_METHOD = "1"
_RECOVERY_NETWORK_STATUS_METHOD = "2"
_RECOVERY_BRIDGE_OPEN_METHOD = "4"
_RECOVERY_BRIDGE_STATUS_METHOD = "5"


def _device_status(value: Any) -> dict[str, Any]:
    """Normalize Gateway status responses without bypassing host verification."""
    if not isinstance(value, dict):
        return {}
    status = value.get("device", value)
    return status if isinstance(status, dict) else {}


def _raw_rpc_payload(value: Any) -> bytes:
    encoded = value.get("payload_base64") if isinstance(value, dict) else None
    if not isinstance(encoded, str):
        raise DeviceError("ESP-Iris returned an invalid Recovery control response.")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise DeviceError("ESP-Iris returned malformed Recovery control data.") from error


def _recovery_control_device(arguments: Any, context: RunContext) -> tuple[Any, str]:
    session = ensure_gateway(context, getattr(arguments, "gateway_profile", None))
    device = select_device(
        connected_devices(context, session), getattr(arguments, "device_id", None)
    )
    device_id = str(device.get("device_id"))
    status = _device_status(gateway_json(context, session, "status", device_id))
    if (status.get("firmware_mode") or device.get("firmware_mode")) != "recovery":
        raise RecoveryRequiredError(
            "Recovery control requires a live Recovery service. "
            "Run 'python mosaico.py recover' first."
        )
    return session, device_id


def configure_recovery_network(arguments: Any, context: RunContext) -> dict[str, Any]:
    session, device_id = _recovery_control_device(arguments, context)
    ssid = arguments.ssid.encode("utf-8")
    password_text = getpass.getpass("Wi-Fi password: ")
    password = password_text.encode("utf-8")
    password_text = ""
    if not 0 < len(ssid) < 33:
        raise DeviceError("Wi-Fi SSID must be between 1 and 32 UTF-8 bytes.")
    password_valid = len(password) == 0 or 8 <= len(password) <= 63
    if len(password) == 64:
        password_valid = re.fullmatch(rb"[0-9a-fA-F]{64}", password) is not None
    if not password_valid:
        raise DeviceError("Wi-Fi password must be empty, 8-63 bytes, or 64 hex bytes.")
    payload = bytes((len(ssid), len(password))) + ssid + password
    context.status("recovery: submitting Wi-Fi credentials over the active USB session")
    gateway_json(
        context,
        session,
        "rpc-raw",
        device_id,
        _RECOVERY_CONTROL_SERVICE_ID,
        _RECOVERY_WIFI_CONNECT_METHOD,
        "--payload-base64-stdin",
        "--deadline-ms",
        "5000",
        stdin_text=base64.b64encode(payload).decode("ascii"),
        timeout=10,
        sensitive_output=True,
    )
    password = b""
    payload = b""

    deadline = time.monotonic() + arguments.timeout
    latest: dict[str, Any] = {}
    while time.monotonic() < deadline:
        value = gateway_json(
            context,
            session,
            "rpc-raw",
            device_id,
            _RECOVERY_CONTROL_SERVICE_ID,
            _RECOVERY_NETWORK_STATUS_METHOD,
            "--deadline-ms",
            "2000",
            timeout=5,
            sensitive_output=True,
        )
        try:
            latest_value = json.loads(_raw_rpc_payload(value).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DeviceError("Recovery returned invalid network status JSON.") from error
        latest = latest_value if isinstance(latest_value, dict) else {}
        if latest.get("connected") is True:
            context.status("recovery: Wi-Fi connected")
            return {
                "command": "recovery-wifi",
                "status": "succeeded",
                "device_id": device_id,
                "network": latest,
                "gateway_started": session.started_local,
                "log": str(context.log_path),
            }
        time.sleep(0.5)
    raise OperationError(
        "Recovery did not connect to Wi-Fi before the timeout.",
        details={"device_id": device_id, "network": latest},
    )


def read_bridge_code(arguments: Any, context: RunContext) -> dict[str, Any]:
    session, device_id = _recovery_control_device(arguments, context)
    deadline = time.monotonic() + arguments.timeout
    method = _RECOVERY_BRIDGE_OPEN_METHOD
    timeout_message = "Bridge pairing code was not ready before the timeout."
    snapshot: dict[str, Any] = {}
    context.status("recovery: opening Bridge download over USB")
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OperationError(timeout_message, details={"bridge": snapshot})
        try:
            value = gateway_json(
                context, session, "rpc-raw", device_id,
                _RECOVERY_CONTROL_SERVICE_ID, method,
                "--deadline-ms", str(max(1, min(2000, int(remaining * 1000)))),
                timeout=remaining, sensitive_output=True,
            )
        except DeviceError:
            # The final subprocess also shares the overall deadline. Its startup
            # overhead can exhaust a short remaining interval before an RPC reply.
            if time.monotonic() >= deadline:
                raise OperationError(timeout_message, details={"bridge": snapshot}) from None
            raise
        method = _RECOVERY_BRIDGE_STATUS_METHOD
        try:
            snapshot = json.loads(_raw_rpc_payload(value).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DeviceError("Recovery returned invalid Bridge status JSON.") from error
        if not isinstance(snapshot, dict):
            raise DeviceError("Recovery returned invalid Bridge status.")
        state = snapshot.get("state")
        if state == "NOT_CONFIGURED":
            raise DeviceError("Recovery Bridge URL or board ID is not configured; rebuild with both settings.")
        code = snapshot.get("code")
        if state == "PAIRING" and code:
            if not isinstance(code, str):
                raise DeviceError("Recovery returned an invalid Bridge pairing code.")
            if not isinstance(snapshot.get("server_url"), str) or not snapshot["server_url"].startswith("https://"):
                raise DeviceError("Recovery returned an invalid Bridge server URL.")
            return {
                "command": "bridge-code", "status": "succeeded",
                "device_id": device_id, "bridge": snapshot,
                "server_url": snapshot["server_url"],
                "gateway_started": session.started_local, "log": str(context.log_path),
            }
        if state in {"PAIRED", "PRECHECK", "WRITING", "VERIFYING", "COMMITTING"}:
            raise DeviceError("Bridge session is already paired or flashing; no new code was generated.")
        if state not in {"WAITING_NETWORK", "REGISTERING", "PAIRING"} or snapshot.get("running") is not True:
            raise DeviceError(f"Bridge session ended: {state}.")
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))


def _recovery_verification_status(
    device: dict[str, Any], status_value: Any
) -> dict[str, Any]:
    """Reconcile the two Gateway snapshots used during install preflight."""
    status = _device_status(status_value)
    listed_mode = device.get("firmware_mode")
    if listed_mode == "normal":
        # The selected-device snapshot identifies the current application
        # boot. A second status request may briefly return an incomplete or
        # stale Recovery-shaped payload while Gateway reconciles reconnects.
        status = {**status, "firmware_mode": "normal"}
    elif listed_mode == "recovery" and not status.get("firmware_mode"):
        status = {**status, "firmware_mode": "recovery"}
    return status


class _MonitorTextRenderer:
    """Reassemble ESP-Iris log records and render complete device log lines."""

    def __init__(
        self,
        stream: TextIO,
        *,
        grep: str | None,
        color_enabled: bool,
    ) -> None:
        self._stream = stream
        self._grep = grep
        self._color_enabled = color_enabled
        self._buffer = ""

    def feed(self, text: str) -> None:
        self._buffer += text
        while True:
            newline = self._buffer.find("\n")
            if newline < 0:
                return
            line = self._buffer[: newline + 1]
            self._buffer = self._buffer[newline + 1 :]
            self._emit(line)

    def finish(self) -> None:
        if self._buffer:
            self._emit(self._buffer)
            self._buffer = ""

    def _emit(self, line: str) -> None:
        if self._grep and self._grep not in line:
            return
        if line.endswith("\r\n"):
            body, ending = line[:-2], "\r\n"
        elif line.endswith("\n"):
            body, ending = line[:-1], "\n"
        else:
            body, ending = line, ""

        match = _IDF_MONITOR_LOG_PATTERN.match(body) if self._color_enabled else None
        if match:
            color = _IDF_MONITOR_COLORS[match.group(1)]
            rendered = f"{color}{body}{_ANSI_NORMAL}{ending}"
        else:
            rendered = line
        self._stream.write(rendered)
        self._stream.flush()


def _monitor_color_enabled(arguments: Any, json_output: bool) -> bool:
    if json_output or getattr(arguments, "disable_auto_color", False):
        return False
    if getattr(arguments, "force_color", False):
        return True
    if not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        ctypes_module: Any = ctypes
        kernel = ctypes_module.windll.kernel32
        handle = kernel.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError, ValueError):
        return False


def list_devices(context: RunContext, gateway_profile: str | None) -> dict[str, Any]:
    """List live devices visible through the selected Gateway."""

    session = ensure_gateway(context, gateway_profile)
    devices = sorted(
        gateway_devices(context, session),
        key=lambda item: str(item.get("device_id") or ""),
    )
    for device in devices:
        device["online"] = device.get("connected") is not False
        device["connection"] = device.get("transport_name") or device.get("transport")
    from .session_runtime import CURRENT_SCOPE, request
    project_scope = CURRENT_SCOPE.get()
    discovered = (
        request(session.connection_args[1], "/v1/project").get("endpoints", [])
        if project_scope is not None and session.profile is None else []
    )
    return {
        "command": "list",
        "status": "succeeded",
        "gateway_started": session.started_local,
        "gateway_profile": session.profile,
        "devices": devices,
        "endpoints": discovered,
    }


def monitor_memory(arguments: Any, context: RunContext, json_output: bool) -> int:
    """Take a snapshot or poll memory without starting a device-side sampler."""

    session = ensure_gateway(context, arguments.gateway_profile)
    device = select_device(connected_devices(context, session), arguments.device_id)
    device_id = str(device.get("device_id"))
    while True:
        started = time.monotonic()
        snapshot = gateway_json(context, session, "memory", device_id)
        if not isinstance(snapshot, dict) or snapshot.get("device_id") != device_id:
            raise DeviceError("ESP-Iris returned a memory snapshot for another device.")
        if json_output:
            print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True), flush=True)
        else:
            print(f"Device {device_id}  Boot {snapshot.get('boot_id')}")
            for region in ("internal", "spiram"):
                total = int(snapshot.get(f"total_{region}_bytes") or 0)
                if total:
                    free = int(snapshot.get(f"free_{region}_bytes") or 0)
                    minimum = int(snapshot.get(f"min_free_{region}_bytes") or 0)
                    print(f"{region:8} free {free:>10} B  minimum {minimum:>10} B  total {total:>10} B")
                else:
                    print(f"{region:8} unavailable")
            print("Task ID    Minimum free stack")
            for task in sorted(snapshot.get("tasks", []), key=lambda item: int(item["stack_free_min_bytes"])):
                print(f"{int(task['task_number']):>7}    {int(task['stack_free_min_bytes']):>10} B")
            print(flush=True)
        if not arguments.follow:
            return 0
        time.sleep(max(0.0, arguments.interval - (time.monotonic() - started)))


def invoke_rpc(arguments: Any, context: RunContext) -> dict[str, Any]:
    """Invoke a raw application RPC through the managed Gateway."""

    session = ensure_gateway(context, arguments.gateway_profile)
    device = select_device(connected_devices(context, session), arguments.device_id)
    device_id = str(device.get("device_id"))
    call_arguments = [
        "rpc-raw",
        device_id,
        arguments.service_id,
        arguments.method_id,
    ]
    stdin_text = None
    if arguments.payload_hex is not None:
        try:
            payload = bytes.fromhex(arguments.payload_hex)
        except ValueError as error:
            raise SelectionError("--payload-hex must contain hexadecimal bytes.") from error
        call_arguments.append("--payload-base64-stdin")
        stdin_text = base64.b64encode(payload).decode("ascii")
    else:
        call_arguments.extend(("--payload", arguments.payload))
    call_arguments.extend(("--deadline-ms", str(arguments.deadline_ms)))
    response = gateway_json(
        context,
        session,
        *call_arguments,
        stdin_text=stdin_text,
        timeout=max(5.0, arguments.deadline_ms / 1000 + 3),
    )
    return {
        "command": "rpc",
        "status": "succeeded",
        "device_id": device_id,
        "response": response,
        "gateway_started": session.started_local,
        "log": str(context.log_path),
    }


def inspect_crash(arguments: Any, context: RunContext) -> dict[str, Any]:
    """Read, archive, and optionally save retained crash evidence."""

    session = ensure_gateway(context, arguments.gateway_profile)
    device = select_device(connected_devices(context, session), arguments.device_id)
    device_id = str(device.get("device_id"))
    report = gateway_json(context, session, "crash", device_id, timeout=10)
    archive = None
    if arguments.archive:
        archive = gateway_json(
            context, session, "crash-archive", device_id, timeout=120
        )
    saved_core = None
    if arguments.save_core is not None:
        saved_core = gateway_json(
            context,
            session,
            "coredump",
            device_id,
            str(arguments.save_core.expanduser().resolve()),
            timeout=60,
        )
    return {
        "command": "crash",
        "status": "succeeded",
        "device_id": device_id,
        "report": report,
        "archive": archive,
        "saved_core": saved_core,
        "gateway_started": session.started_local,
        "log": str(context.log_path),
    }


def enter_recovery(arguments: Any, context: RunContext) -> dict[str, Any]:
    """Enter retained Recovery without starting an installation."""

    session = ensure_gateway(context, arguments.gateway_profile)
    device = select_device(connected_devices(context, session), arguments.device_id)
    device_id = str(device.get("device_id"))
    status = _device_status(gateway_json(context, session, "status", device_id))
    mode = status.get("firmware_mode") or device.get("firmware_mode")
    before_boot_id = status.get("boot_id") or device.get("boot_id")
    before_boot_id_text = (
        status.get("boot_id_text")
        or device.get("boot_id_text")
        or (str(before_boot_id) if before_boot_id is not None else None)
    )
    if mode == "recovery":
        recovery = status
        transition = "already_recovery"
    else:
        if mode != "normal":
            raise DeviceError(
                "The device firmware mode is unknown; refusing the Recovery transition."
            )
        context.status("recovery: entering retained Recovery")
        recovery = enter_recovery_and_wait(
            context,
            session,
            device_id,
            previous_boot_id=before_boot_id,
            timeout=arguments.timeout,
        )
        transition = "entered_recovery"
    return {
        "command": "enter-recovery",
        "status": "succeeded",
        "transition": transition,
        "device_id": device_id,
        "boot_id_before": before_boot_id,
        "boot_id_before_text": before_boot_id_text,
        "boot_id": recovery.get("boot_id"),
        "boot_id_text": recovery.get("boot_id_text")
        or (
            str(recovery.get("boot_id"))
            if recovery.get("boot_id") is not None
            else None
        ),
        "firmware_mode": recovery.get("firmware_mode"),
        "app_version": recovery.get("app_version"),
        "gateway_started": session.started_local,
        "log": str(context.log_path),
    }


def start_system_update(arguments: Any, context: RunContext) -> dict[str, Any]:
    """Build or select a full-system bundle and apply it through Recovery."""

    manifest_path = getattr(arguments, "manifest_path", None)
    bundle_argument = getattr(arguments, "bundle", None)
    project_argument = getattr(arguments, "project", None)
    skip_build = bool(getattr(arguments, "skip_build", False))
    timeout = float(getattr(arguments, "timeout", 900.0))
    external_source = manifest_path is not None

    project: Path | None = None
    bundle: Path | None = None
    reused_build = False
    if external_source:
        if project_argument or skip_build:
            raise SelectionError(
                "--project and --skip-build apply only to local System Update bundles."
            )
    elif bundle_argument is not None:
        if project_argument or skip_build:
            raise SelectionError(
                "--bundle cannot be combined with --project or --skip-build."
            )
        bundle = Path(bundle_argument).expanduser()
        bundle = (
            (Path.cwd() / bundle).resolve()
            if not bundle.is_absolute()
            else bundle.resolve()
        )
        reused_build = True
    else:
        project = resolve_project(context.workspace, project_argument, Path.cwd())
        context.status(f"project: {project}")
        ensure_gateway(context, arguments.gateway_profile, select=False)
        if not skip_build:
            context.status("system update: building application + declared resources + system bundle")
            iris_python, _ = ensure_iris_tools(context)
            run_idf_target(
                context,
                idf_path=resolve_idf_path(context.workspace, project),
                project=project,
                build_dir=project / "build",
                target="system-update-bundle",
                definitions={"ESP_IRIS_PYTHON": str(iris_python)},
                timeout=3600,
            )
        else:
            context.status("build: reusing existing System Update bundle (--skip-build)")
        artifacts = discover_artifacts(project)
        bundle = (
            artifacts.build_dir
            / f"{artifacts.project_name}-system-update.irisfw"
        )
        reused_build = skip_build

    if bundle is not None:
        if bundle.suffix != ".irisfw" or not bundle.is_file() or bundle.stat().st_size == 0:
            raise BuildError(
                f"A complete local System Update bundle was not found: {bundle}"
            )
        context.status(
            f"bundle: {bundle} ({bundle.stat().st_size} bytes)"
        )
        bundle_plan = inspect_bundle_plan(context, bundle)

    context.status("gateway: connecting")
    session = ensure_gateway(context, arguments.gateway_profile)
    device = select_device(connected_devices(context, session), arguments.device_id)
    device_id = str(device.get("device_id"))
    status = _device_status(
        gateway_json(context, session, "status", device_id)
    )
    firmware_mode = status.get("firmware_mode") or device.get("firmware_mode")
    context.status(
        f"device: {device_id} mode={firmware_mode or 'unknown'} "
        f"boot_id={status.get('boot_id') or device.get('boot_id') or 'unknown'}"
    )
    if external_source and firmware_mode != "recovery":
        raise RecoveryRequiredError(
            "External System Update requires a live Recovery service. "
            "Run 'python mosaico.py recover' first."
        )

    if bundle is not None:
        context.status("system update: submitting local atomic bundle")
        operation = run_system_update_bundle(
            context,
            session,
            device_id=device_id,
            bundle=bundle,
            timeout=timeout,
        )
        context.status("validation: target firmware and system partitions are healthy")
        return {
            "command": "system-update",
            "status": "succeeded",
            "device_id": device_id,
            "firmware_mode": firmware_mode,
            "source": "bundle" if bundle_argument is not None else "local_build",
            "project": str(project) if project is not None else None,
            "bundle": str(bundle),
            "write_plan": bundle_plan,
            "reused_build": reused_build,
            "gateway_started": session.started_local,
            "operation": operation,
            "log": str(context.log_path),
        }

    method_id = "2"
    payload = arguments.manifest_path
    source = "nand"
    action = "NAND LittleFS read"
    context.status(f"system update: requesting Recovery {action}")
    response = gateway_json(
        context,
        session,
        "rpc-raw",
        device_id,
        "0x1201",
        method_id,
        "--payload",
        payload,
        "--deadline-ms",
        "5000",
        timeout=10,
        sensitive_output=True,
    )
    context.status("system update: accepted by Recovery; update is running")
    return {
        "command": "system-update",
        "status": "accepted",
        "device_id": device_id,
        "firmware_mode": firmware_mode,
        "source": source,
        "gateway_started": session.started_local,
        "response": response,
        "log": str(context.log_path),
    }


def install(arguments: Any, context: RunContext) -> dict[str, Any]:
    workspace = context.workspace
    project = resolve_project(workspace, arguments.project, Path.cwd())
    context.status(f"project: {project}")
    # Keep the project service available through a long build without opening
    # an unselected device until the artifacts have passed validation.
    ensure_gateway(context, arguments.gateway_profile, select=False)
    reused = bool(arguments.skip_build)
    if not reused:
        build_application(context, project)
    else:
        context.status("build: reusing existing artifacts (--skip-build)")
    artifacts = discover_artifacts(project)
    context.status(
        f"artifact: {artifacts.project_name} {artifacts.project_version} "
        f"({artifacts.image.stat().st_size} bytes, {artifacts.target})"
    )
    model = select_model(workspace, None)
    recovery_manifest = load_bundle(workspace.recovery_dir, model.target)
    if artifacts.target != model.target:
        raise BuildError(
            f"The project target is {artifacts.target!r}, but the device requires "
            f"{model.target!r}."
        )
    expected_layout_sha256 = partition_table_flash_sha256(
        artifacts.partition_table
    )

    context.status("gateway: connecting")
    session = ensure_gateway(context, arguments.gateway_profile)
    device = select_device(connected_devices(context, session), arguments.device_id)
    device_id = str(device.get("device_id"))
    context.status(
        f"device: {device_id} mode={device.get('firmware_mode', 'unknown')} "
        f"boot_id={device.get('boot_id', 'unknown')}"
    )
    status = _recovery_verification_status(
        device, gateway_json(context, session, "status", device_id)
    )
    recovery_version = str(recovery_manifest.get("version") or "")
    recovery_verified, verification = recovery_verification_details(
        device_id, status, recovery_version, workspace
    )
    context.note(
        "recovery verification: "
        + json.dumps(verification, ensure_ascii=False, sort_keys=True)
    )
    if not recovery_verified:
        raise RecoveryRequiredError(
            "The device has not completed Recovery initialization or verification. "
            "Run 'python mosaico.py recover' first. "
            f"Verification details: {json.dumps(verification, ensure_ascii=False)}"
        )
    # Product requirements are data submitted with the operation. Iris owns
    # transition, reconnect, live verification and writing as one state machine.
    preconditions = {"recovery_version": recovery_version,
                     "partition_table_sha256": expected_layout_sha256}
    context.status(f"ota: starting recovery-first installation ({arguments.validation})")
    try:
        operation = run_ota(
            context,
            session,
            device_id=device_id,
            image=artifacts.image,
            elf=artifacts.elf,
            map_file=artifacts.map_file,
            validation=arguments.validation,
            timeout=arguments.timeout,
            preconditions=preconditions,
        )
    except OperationError as error:
        failure = ((error.details.get("result") or {}).get("result") or {}).get("failure") or {}
        if failure.get("code") != "partition_layout_mismatch":
            raise
        command = ["python3", "mosaico.py", "iris", "system-update", "--project", str(project),
                   "--device-id", device_id]
        if getattr(arguments, "endpoint", None):
            command.extend(["--endpoint", arguments.endpoint])
        if arguments.gateway_profile:
            command.extend(["--gateway-profile", arguments.gateway_profile])
        suggestion = shlex.join(command)
        raise OperationError(
            f"Partition layout differs (device={failure.get('current_sha256')}, "
            f"build={failure.get('target_sha256')}). Install the project's intended layout with: {suggestion}",
            details={**error.details, **failure, "suggested_command": suggestion},
        ) from error
    completed = operation.get("operation", operation)
    evidence = (completed.get("result") or {}).get("recovery") or {}
    if evidence.get("device_id") == device_id and evidence.get("recovery_version") == recovery_version:
        try:
            record_recovery_verification(device_id, recovery_version, evidence.get("boot_id"))
        except OSError as error:
            context.note(f"warning: could not refresh Recovery verification: {error}")
    context.status("validation: installed application is connected and healthy")
    return {
        "command": "install",
        "status": "succeeded",
        "project": str(project),
        "device_id": device_id,
        "firmware": {
            "name": artifacts.project_name,
            "version": artifacts.project_version,
            "target": artifacts.target,
        },
        "validation": arguments.validation,
        "reused_build": reused,
        "gateway_started": session.started_local,
        "operation": operation,
        "log": str(context.log_path),
    }


def recover(arguments: Any, context: RunContext) -> dict[str, Any]:
    workspace = context.workspace
    if getattr(arguments, "gateway_profile", None):
        raise DeviceError(
            "Remote Recovery is not supported; run recovery on the Gateway host."
        )
    model = select_model(workspace, arguments.model)
    context.status(
        f"recovery: model={model.id} target={model.target} source={arguments.source}"
    )
    recovery_project = workspace.recovery_project
    idf_path = resolve_idf_path(workspace, recovery_project)
    context.status(f"idf: environment ready at {idf_path}")
    bundle_dir = workspace.recovery_dir
    manifest: dict[str, Any] | None = None
    if arguments.source == "reviewed":
        manifest = load_bundle(bundle_dir, model.target)
        recovery_image = manifest.get("images", {}).get("recovery", {})
        context.status(
            f"bundle: verified {manifest.get('version', 'unknown')} "
            f"({recovery_image.get('size', 'unknown')} bytes)"
        )
    elif not arguments.dry_run:
        print(
            "Warning: building an unreviewed Recovery candidate bundle from the current source.",
            file=sys.stderr,
        )
        context.status("bundle: current-source Recovery candidate selected")

    prior_device_id: str | None = arguments.device_id
    selected_hardware_mac: str | None = getattr(arguments, "hardware_mac", None)
    prior_boot_id: str | None = None
    prior_session = None
    context.status("gateway: checking the currently connected device")
    try:
        prior_session = ensure_gateway(context, None)
        devices = connected_devices(context, prior_session)
    except DeviceError:
        if not arguments.dry_run:
            raise
        devices = []
        context.note("warning: local Gateway was unavailable during dry-run")
    prior_device = None
    if arguments.device_id:
        matches = [item for item in devices if item.get("device_id") == arguments.device_id]
        if len(matches) == 1:
            prior_device = matches[0]
        else:
            raise DeviceError(
                "The requested Device ID is not reachable; refusing to select another ROM device. "
                "Use --hardware-mac to explicitly identify an offline recovery target."
            )
    elif selected_hardware_mac:
        matches = [
            item for item in devices
            if str(item.get("hardware_mac") or "").lower() == selected_hardware_mac
        ]
        if len(matches) == 1:
            prior_device = matches[0]
        elif len(matches) > 1:
            raise SelectionError(
                f"Multiple managed devices reported hardware MAC {selected_hardware_mac}."
            )
    elif len(devices) == 1:
        prior_device = devices[0]
    elif len(devices) > 1:
        raise SelectionError(
            "Multiple managed devices were found; specify the recovery target with --device-id."
        )
    if prior_device is not None:
        prior_device_id = str(prior_device.get("device_id"))
        selected_hardware_mac = str(
            prior_device.get("hardware_mac") or selected_hardware_mac or ""
        ) or None
        prior_boot_id = str(prior_device.get("boot_id") or "") or None
        context.status(
            f"device: {prior_device_id} mode="
            f"{prior_device.get('firmware_mode', 'unknown')} "
            f"boot_id={prior_boot_id or 'unknown'}"
        )
    else:
        context.status(
            "gateway: managed device unavailable; checking the recovery interface"
        )

    independent_port = getattr(arguments, "recovery_port", None)
    independent_identity = None
    if independent_port:
        if prior_device is None or prior_session is None:
            raise DeviceError("--recovery-port requires a live managed Device ID before flashing.")
        live = _device_status(gateway_json(context, prior_session, "status", prior_device_id))
        if live.get("device_id") != prior_device_id or not live.get("boot_id"):
            raise DeviceError("The managed device did not provide live Device ID and Boot ID evidence.")
        prior_boot_id = str(live["boot_id"])
        independent_identity = serial_jtag_candidate(independent_port)
        context.status(f"device: explicit independent USB Serial/JTAG at {independent_identity['path']}")

    unowned_port: str | None = None
    if prior_device is None:
        context.status("device: detecting an unowned ROM configuration interface")

        def probe_unowned_rom_mac(port: str) -> str:
            if prior_session is None:
                return read_rom_hardware_mac(context, model, port, idf_path)
            result = run_host_operation(context, prior_session, {
                "action": "host.probe", "endpoint": port,
                "commands": [rom_identity_command(model, "{port}", idf_path)],
                "timeout": min(arguments.timeout, 30),
            }, timeout=min(arguments.timeout, 45))
            return rom_hardware_mac(result["stdout"])

        unowned_port = provisioning_candidate(
            context,
            model,
            hardware_mac=selected_hardware_mac,
            idf_path=idf_path,
            mac_reader=probe_unowned_rom_mac,
        )
        if selected_hardware_mac:
            context.status(
                f"device: recovery interface ready at {unowned_port} "
                f"hardware_mac={selected_hardware_mac}"
            )
        elif arguments.source == "current":
            selected_hardware_mac = probe_unowned_rom_mac(unowned_port)
            context.status(
                f"device: recovery interface ready at {unowned_port} "
                f"hardware_mac={selected_hardware_mac}"
            )

    build_dir = recovery_project / "build-mosaico-recovery"
    plan = {
        "command": "recover",
        "status": "dry_run" if arguments.dry_run else "planned",
        "model": model.id,
        "source": arguments.source,
        "device_id": prior_device_id,
        "hardware_mac": selected_hardware_mac,
        "target": model.target,
        "recovery_version": manifest.get("version") if manifest else "current-source",
        "recovery_port": independent_identity,
        "previous_boot_id": prior_boot_id,
        "checks": {
            "idf": str(idf_path),
            "bundle_verified": manifest is not None,
            "device_unique": prior_device is not None or unowned_port is not None,
            "gateway_local": True,
        },
        "log": str(context.log_path),
    }
    if arguments.dry_run:
        context.status("validation: recovery preflight checks passed; no write performed")
        return plan

    if prior_session is None:
        raise DeviceError("The local Gateway is required to verify Recovery.")

    context.status("bundle: preparing all Recovery artifacts before acquiring the device")
    required_recovery_components = (
        workspace.bsp_path / "components" / "esp-mosaico-bsp",
        workspace.esp_iris_path / "components" / "esp_iris",
    )
    optional_boot_splash = workspace.bsp_path / "components" / "mosaico_boot_splash"
    recovery_components = required_recovery_components + (
        (optional_boot_splash,) if (optional_boot_splash / "CMakeLists.txt").is_file() else ()
    )
    missing_components = [
        str(path)
        for path in required_recovery_components
        if not (path / "CMakeLists.txt").is_file()
    ]
    if missing_components:
        raise EnvironmentError(
            "Required Recovery components are unavailable.",
            details={"missing": missing_components},
        )
    recovery_definitions = {
        "MOSAICO_RECOVERY_SOURCE": arguments.source,
        "EXTRA_COMPONENT_DIRS": ";".join(
            path.resolve().as_posix() for path in recovery_components
        ),
    }
    defaults_fingerprint: str | None = None
    if arguments.source == "current":
        defaults_fingerprint = recovery_defaults_fingerprint(recovery_project)
        if not recovery_build_defaults_are_current(
            build_dir, defaults_fingerprint
        ):
            context.status(
                "bundle: Recovery defaults changed; clearing the stale generated sdkconfig"
            )
            run_idf_target(
                context,
                idf_path=idf_path,
                project=recovery_project,
                build_dir=build_dir,
                target="fullclean",
                definitions=recovery_definitions,
                timeout=arguments.timeout,
            )
    run_idf_target(
        context,
        idf_path=idf_path,
        project=recovery_project,
        build_dir=build_dir,
        target="mosaico-recover-prepare",
        definitions=recovery_definitions,
        timeout=arguments.timeout,
    )
    if defaults_fingerprint is not None:
        record_recovery_build_defaults(build_dir, defaults_fingerprint)
    prepared_dir = build_dir / (
        "recovery" if arguments.source == "reviewed" else "recovery-current"
    )
    prepared_manifest = load_bundle(prepared_dir, model.target)
    expected_version = str(prepared_manifest.get("version") or "")

    commands = []
    if independent_identity is not None:
        if serial_jtag_candidate(independent_identity["path"]) != independent_identity:
            raise DeviceError("USB Serial/JTAG identity changed during Recovery preparation.")
    if selected_hardware_mac and (unowned_port is not None or independent_identity is not None):
        probe = rom_identity_command(model, "{port}", idf_path)
        probe["expect"] = {"pattern": r"(?i)MAC:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})",
                           "value": selected_hardware_mac}
        commands.append(probe)
    commands.append(idf_target_command(
        context, idf_path=idf_path, project=recovery_project, build_dir=build_dir,
        target="mosaico-recover-flash", definitions=recovery_definitions,
        port="{port}", timeout=arguments.timeout,
    ))
    context.status("gateway: running ROM write and Recovery verification as one operation")
    completed = run_host_operation(context, prior_session, {
        "action": "host.recovery", "device_id": prior_device_id if prior_device else None,
        "endpoint": unowned_port,
        "write_endpoint": independent_identity["path"] if independent_identity else None,
        "expected_version": expected_version, "expected_hardware_mac": selected_hardware_mac,
        "commands": commands, "timeout": arguments.timeout,
    }, timeout=arguments.timeout * 2 + 30)
    status = completed.get("evidence", {}).get("verification", {})
    if (status.get("firmware_mode") != "recovery" or not status.get("boot_id")
            or (prior_boot_id and str(status["boot_id"]) == prior_boot_id)
            or status.get("app_version") != expected_version
            or "ota" not in status.get("capability_names", [])
            or (selected_hardware_mac and status.get("hardware_mac") != selected_hardware_mac)):
        raise OperationError("ROM operation did not verify the expected Recovery identity and new boot.")

    verified_device_id = str(status.get("device_id") or prior_device_id or "")
    if prior_device_id and verified_device_id != prior_device_id:
        raise OperationError("Recovery returned a different Device ID than the selected target.")
    if not verified_device_id:
        raise OperationError(
            "Recovery is ready, but the Device ID could not be confirmed."
        )
    recovery_version = expected_version or str(status.get("app_version") or "current-source")
    record_recovery_verification(
        verified_device_id, recovery_version, status.get("boot_id")
    )
    context.status(
        f"validation: Recovery {recovery_version} ready on {verified_device_id} "
        f"boot_id={status.get('boot_id', 'unknown')}"
    )
    return {
        **plan,
        "status": "succeeded",
        "device_id": verified_device_id,
        "hardware_mac": status.get("hardware_mac") or selected_hardware_mac,
        "boot_id": status.get("boot_id"),
        "gateway_started": prior_session.started_local,
        "operation_id": completed["operation_id"],
    }


def monitor(arguments: Any, context: RunContext, json_output: bool) -> int:
    session = ensure_gateway(context, arguments.gateway_profile)
    device = select_device(connected_devices(context, session), arguments.device_id)
    device_id = str(device.get("device_id"))
    argv = session.ctl_argv(
        "logs", "--device", device_id, *( () if arguments.snapshot else ("--follow",) ),
        # Always consume structured events. The ESP-Iris text formatter uses
        # print() even though event text already carries its line ending.
        json_output=True,
    )
    context.note("$ " + " ".join(argv))
    monitor_environment = os.environ.copy()
    # The ESP-Iris CLI writes into a pipe here, so Python would otherwise use
    # block buffering. Force each printed log line through to this process,
    # whose user-facing print calls already use flush=True below.
    monitor_environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        env=monitor_environment,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    deadline = time.monotonic() + arguments.timeout if arguments.timeout else None
    try:
        stdout = process.stdout
        assert stdout is not None
        sentinel = object()
        records: Queue[str | object] = Queue()

        def read_records() -> None:
            try:
                for record in stdout:
                    records.put(record)
            finally:
                records.put(sentinel)

        reader = Thread(target=read_records, daemon=True)
        reader.start()
        invalid_child_output = False
        stopped_by_us = False
        renderer = _MonitorTextRenderer(
            sys.stdout,
            grep=arguments.grep,
            color_enabled=_monitor_color_enabled(arguments, json_output),
        )

        def handle_record(record: str) -> None:
            nonlocal invalid_child_output
            context.note(record)
            try:
                item = json.loads(record)
            except json.JSONDecodeError:
                invalid_child_output = True
                if not json_output:
                    renderer.feed(record)
                return

            text = str(item.get("text", "")) if isinstance(item, dict) else ""
            if json_output:
                if not arguments.grep or arguments.grep in text:
                    sys.stdout.write(record)
                    sys.stdout.flush()
                return
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                renderer.feed(item["text"])
            else:
                renderer.feed(json.dumps(item, ensure_ascii=False) + "\n")

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                process.terminate()
                stopped_by_us = True
                break
            wait = 0.2
            if deadline is not None:
                wait = max(0, min(wait, deadline - time.monotonic()))
            try:
                record = records.get(timeout=wait)
            except Empty:
                if process.poll() is not None:
                    break
                continue
            if record is sentinel:
                break
            handle_record(str(record))
        renderer.finish()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)
        reader.join(timeout=1)
        stdout.close()
        if not stopped_by_us and return_code != 0:
            raise DeviceError(
                "The log connection ended unexpectedly.",
                details={"log": str(context.log_path)},
            )
        if invalid_child_output:
            raise DeviceError(
                "ESP-Iris returned an invalid log event.",
                details={"log": str(context.log_path)},
            )
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        return 0
    return 0
