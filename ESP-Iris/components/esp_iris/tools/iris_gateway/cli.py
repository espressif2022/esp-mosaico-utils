from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import ipaddress
import json
import os
import pathlib
import signal
import ssl
import sys
import threading
import uuid
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from aiohttp import (
    ClientConnectorCertificateError,
    ClientResponse,
    ClientSession,
    Fingerprint,
    FormData,
    WSMsgType,
    web,
)

from . import __version__
from .client_lifecycle import CAPABILITY as LIFECYCLE_CAPABILITY
from .compat import BooleanOptionalAction
from .compatibility import compatibility_expectation
from .demo import DemoHub
from .discovery import discover_iris_usb_devices
from .gateway import (
    DEFAULT_OTA_VALIDATION_MODE,
    OTA_VALIDATION_MODES,
    GatewayService,
    create_app,
)
from .hub import IrisHub
from .link import EndpointLock
from .ownership import OwnershipRegistry
from .project_gateway import ProjectGateway
from .recovery_transition import recovery_preconditions
from .security import DEFAULT_DEVELOPER_PASSWORD
from .store import GatewayStore
from .system_update import (
    build_system_update_bundle,
    load_system_update_bundle,
)
from .tls import ensure_certificate, ssl_context


def _default_state_dir(instance_id: str) -> pathlib.Path:
    if os.name == "nt":
        root = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home()))
    elif sys.platform == "darwin":
        root = pathlib.Path.home() / "Library" / "Application Support"
    else:
        root = pathlib.Path(os.environ.get("XDG_STATE_HOME", pathlib.Path.home() / ".local/state"))
    return root / "esp-iris" / instance_id


def _config_dir() -> pathlib.Path:
    if os.name == "nt":
        root = pathlib.Path(os.environ.get("APPDATA", pathlib.Path.home()))
    elif sys.platform == "darwin":
        root = pathlib.Path.home() / "Library" / "Application Support"
    else:
        root = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
    return root / "esp-iris"


def _read_secret_file(path: str | None) -> str | None:
    if not path:
        return None
    return pathlib.Path(path).read_text(encoding="utf-8").strip()


def _tcp_endpoint(value: str) -> tuple[str, int]:
    if value.startswith("["):
        host, separator, port = value[1:].partition("]:")
        return (host, int(port)) if separator else (value.strip("[]"), 19772)
    host, separator, port = value.rpartition(":")
    return (host, int(port)) if separator and port.isdigit() else (value, 19772)


def _listen_is_loopback(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback
    except ValueError:
        return False


def _recover_interrupted(store: GatewayStore) -> None:
    rows = store.db.execute(
        "SELECT operation_id, action FROM operations WHERE status IN "
        "('queued','running','entering_recovery','waiting_recovery',"
        "'recovery_connected','preparing_ota','erasing',"
        "'preserving_evidence','validating_plan','transferring','verifying',"
        "'committing','waiting_device','reconnecting')"
    ).fetchall()
    for row in rows:
        uncertain = str(row["action"]).startswith(
            ("rpc.", "device.restart", "firmware.ota", "firmware.system_update", "host.recovery")
        )
        store.update_operation(
            row["operation_id"],
            status="outcome_unknown" if uncertain else "interrupted",
            error="gateway restarted; operation was not replayed",
            finished_ns=__import__("time").time_ns(),
        )
    if rows:
        store.add_audit("system", "gateway", "operations.recovered", {"count": len(rows), "replayed": False})


async def _web(args: argparse.Namespace) -> None:
    state_dir = pathlib.Path(args.state_dir) if args.state_dir else _default_state_dir(args.instance_id)
    lock = EndpointLock("gateway-state:" + os.path.normcase(str(state_dir.expanduser().resolve())))
    try:
        lock.acquire()
        await _web_owned(args, state_dir.expanduser().resolve())
    finally:
        lock.close()


async def _web_owned(args: argparse.Namespace, state_dir: pathlib.Path) -> None:
    store = GatewayStore(state_dir)
    _recover_interrupted(store)
    system_update_trust_key = None
    if args.system_update_trust_key:
        trust_path = pathlib.Path(args.system_update_trust_key).expanduser().resolve()
        system_update_trust_key = trust_path.read_bytes()
    service = GatewayService(
        store,
        instance_id=args.instance_id,
        demo=args.demo,
        require_local_auth=args.require_local_auth,
        frontend_dist=pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist",
        system_update_trust_key=system_update_trust_key,
        ota_health_timeout=args.ota_health_timeout,
    )
    listener_is_loopback = _listen_is_loopback(args.listen)
    default_password_selected = False
    if not service.auth.configured:
        password = os.environ.get("ESP_IRIS_DEVELOPER_PASSWORD") or _read_secret_file(args.password_file)
        if not password:
            password = DEFAULT_DEVELOPER_PASSWORD
            default_password_selected = True
        service.auth.set_initial_password(
            password,
            "gateway-default" if default_password_selected else "gateway-first-run",
        )

    registry = None
    stop = asyncio.Event()
    owner_task = None
    drain_task = None
    idle_task = None
    shared = getattr(args, "project_shared", False)
    project_session_id = getattr(args, "project_session_id", None)
    if project_session_id:
        if args.demo or not listener_is_loopback or not args.ownership_dir:
            raise ValueError("project sessions require a local real Gateway and ownership directory")
        registry = OwnershipRegistry(pathlib.Path(args.ownership_dir))
        registry.register(project_session_id, args.project_id, args.project_path, args.instance_id,
                          persistent=shared or args.project_persistent)
        if shared:
            registry.set_metadata(args.project_workspace or "", LIFECYCLE_CAPABILITY,
                                  os.environ.get("ESP_IRIS_SOURCE_REVISION", "unknown"))
    if args.demo:
        hub: Any = DemoHub(service.on_device_event)
    else:
        hub = IrisHub(instance_id=args.instance_id, event_sink=service.on_device_event, ownership=registry)
    service.attach_hub(hub)
    if registry is not None:
        service.project = ProjectGateway(registry, service, hub, stop, pairing_token=args.pairing_token, shared=shared)
        drain_task = asyncio.create_task(service.project.drain())
    try:
        if args.demo:
            await hub.start()
        else:
            for endpoint in args.tcp:
                host, port = _tcp_endpoint(endpoint)
                await hub.add_tcp(host, port, pairing_token=args.pairing_token)
            for port in args.usb:
                await hub.add_usb(port)
            for port in args.usb_serial_jtag:
                await hub.add_usb(port, usb_serial_jtag=True)
            explicit_endpoints = bool(args.usb or args.usb_serial_jtag or args.tcp)
            discover_usb = args.discover_usb if args.discover_usb is not None else not explicit_endpoints
            discover_mdns = args.discover_mdns if args.discover_mdns is not None else not explicit_endpoints
            if discover_usb:
                await hub.start_usb_discovery(
                    args.usb_discovery_interval,
                    include_usb_serial_jtag=args.discover_usb_serial_jtag,
                )
            if discover_mdns:
                await hub.start_mdns_discovery(pairing_token=args.pairing_token)

        context = None
        fingerprint = None
        scheme = "http"
        tls_requested = bool(args.tls or args.tls_cert or args.tls_key)
        if args.no_tls and tls_requested:
            raise ValueError("--no-tls cannot be combined with TLS options")
        if tls_requested:
            cert, key, fingerprint = ensure_certificate(
                state_dir,
                cert_path=pathlib.Path(args.tls_cert) if args.tls_cert else None,
                key_path=pathlib.Path(args.tls_key) if args.tls_key else None,
            )
            context = ssl_context(cert, key)
            scheme = "https"

        store.set_setting(
            "tls",
            {"enabled": context is not None, "fingerprint_sha256": fingerprint},
        )
        store.add_audit(
            "system",
            "gateway",
            "gateway.started",
            {
                "instance_id": args.instance_id,
                "listen": args.listen,
                "port": args.port,
                "https": context is not None,
                "demo": args.demo,
                "mode": service.mode,
                "local_authentication_required": service.require_local_auth,
                "remote_authentication_required": True,
            },
        )

        runner = web.AppRunner(create_app(service), shutdown_timeout=5 if registry is not None else 60)
        await runner.setup()
        await web.TCPSite(runner, args.listen, args.port, ssl_context=context).start()
        if registry is not None:
            bound_port = runner.addresses[0][1]
            url = f"{scheme}://{args.listen}:{bound_port}"
            registry.set_url(url)
            if shared:
                # Start the idle clock only after HTTP can admit its first client.
                service.project.clients.idle_since = service.project.clients.clock()
                idle_task = asyncio.create_task(service.project.idle_shutdown())
            if args.project_connection_file:
                connection = pathlib.Path(args.project_connection_file)
                temporary = connection.with_suffix(".tmp")
                temporary.write_text(json.dumps(registry.session(str(project_session_id))), encoding="utf-8")
                temporary.replace(connection)
            if args.owner_stdin and not shared:
                loop = asyncio.get_running_loop()
                lost_owner = asyncio.Event()

                def watch_owner() -> None:
                    sys.stdin.buffer.read()
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(lost_owner.set)

                async def owner_lifetime() -> None:
                    await lost_owner.wait()
                    service.project.request_stop()

                threading.Thread(target=watch_owner, daemon=True).start()
                owner_task = asyncio.create_task(owner_lifetime())
        label = " [DEMO]" if args.demo else ""
        print(f"ESP-Iris {args.instance_id}{label} listening at {scheme}://{args.listen}:{runner.addresses[0][1]}")
        print(
            "Authentication: "
            f"local={'required' if service.require_local_auth else 'disabled'}, "
            "remote=required"
        )
        if fingerprint:
            print(f"TLS SHA-256 fingerprint: {fingerprint}")
        elif not listener_is_loopback:
            print("WARNING: LAN HTTP does not encrypt passwords, tokens, or device data")
        if service.auth.verify_password(DEFAULT_DEVELOPER_PASSWORD):
            print("WARNING: default developer password 'espressif' is active")
        print(f"State: {state_dir}")
        loop = asyncio.get_running_loop()
        def request_stop() -> None:
            if service.project is None:
                stop.set()
            else:
                service.project.request_stop()

        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(getattr(signal, name), request_stop)
        await stop.wait()
        await runner.cleanup()
    finally:
        if idle_task is not None:
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
        if owner_task is not None:
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)
        if drain_task is not None:
            drain_task.cancel()
            await asyncio.gather(drain_task, return_exceptions=True)
        await service.operations.close()
        await hub.close()
        if registry is not None:
            if args.project_connection_file:
                with contextlib.suppress(OSError, ValueError):
                    connection = pathlib.Path(args.project_connection_file)
                    if json.loads(connection.read_text()).get("session_id") == project_session_id:
                        connection.unlink()
            registry.close(clean=not service.project.drain_timed_out)
        store.add_audit(
            "system", "gateway", "gateway.stopped", {"instance_id": args.instance_id}
        )
        store.cleanup_raw_logs()
        store.close()


def _profile_path() -> pathlib.Path:
    override = os.environ.get("ESP_IRIS_PROFILE_FILE")
    return pathlib.Path(override) if override else _config_dir() / "profiles.json"


def _load_profiles() -> dict[str, Any]:
    path = _profile_path()
    if not path.exists():
        return {"default": "default", "profiles": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_profiles(data: dict[str, Any]) -> None:
    path = _profile_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _profile(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    profiles = _load_profiles()
    name = args.profile or profiles.get("default", "default")
    profile = dict(profiles.get("profiles", {}).get(name, {}))
    profile.setdefault("url", "http://127.0.0.1:8443")
    if args.url:
        profile["url"] = args.url.rstrip("/")
    if args.ca:
        profile["ca"] = str(pathlib.Path(args.ca).resolve())
    if args.fingerprint:
        profile["fingerprint"] = args.fingerprint.replace(":", "").lower()
    profile["name"] = name
    return profiles, profile


def _client_ssl(profile: dict[str, Any], insecure: bool) -> ssl.SSLContext | Fingerprint | bool:
    if urlparse(str(profile.get("url", ""))).scheme != "https":
        return False
    if insecure:
        return False
    if profile.get("fingerprint"):
        return Fingerprint(bytes.fromhex(profile["fingerprint"]))
    context = ssl.create_default_context(cafile=profile.get("ca"))
    if not profile.get("ca"):
        default_ca = _default_state_dir("default") / "tls" / "gateway.crt"
        if default_ca.exists():
            context.load_verify_locations(default_ca)
    return context


def _headers(profile: dict[str, Any]) -> dict[str, str]:
    token = os.environ.get("ESP_IRIS_AGENT_TOKEN")
    token_file = os.environ.get("ESP_IRIS_AGENT_TOKEN_FILE")
    if not token and token_file:
        token = pathlib.Path(token_file).read_text(encoding="utf-8").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    if profile.get("session"):
        return {"Cookie": f"esp_iris_session={profile['session']}"}
    return {}


async def _response_json(response: ClientResponse) -> Any:
    try:
        body = await response.json()
    except (json.JSONDecodeError, ValueError):
        body = {"error": {"code": "http_error", "message": await response.text()}}
    if response.status >= 400:
        error = body.get("error", body) if isinstance(body, dict) else body
        raise RuntimeError(f"HTTP {response.status}: {json.dumps(error, ensure_ascii=False)}")
    return body


def _output(value: Any, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def _firmware_paths(args: argparse.Namespace) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    binary = pathlib.Path(args.image).expanduser().resolve()
    elf = pathlib.Path(args.elf).expanduser().resolve() if args.elf else binary.with_suffix(".elf")
    map_path = pathlib.Path(args.map).expanduser().resolve() if args.map else binary.with_suffix(".map")
    missing = [str(path) for path in (binary, elf, map_path) if not path.is_file()]
    if missing:
        raise ValueError(
            "complete OTA evidence requires BIN, ELF and map; missing: "
            + ", ".join(missing)
        )
    return binary, elf, map_path


async def _upload_firmware_artifact(
    session: ClientSession,
    base: str,
    ssl_value: Any,
    paths: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
) -> dict[str, Any]:
    form = FormData()
    for field, path in zip(("bin", "elf", "map"), paths):
        form.add_field(
            field,
            path.read_bytes(),
            filename=path.name,
            content_type="application/octet-stream",
        )
    async with session.post(base + "/v1/firmware-artifacts", data=form, ssl=ssl_value) as response:
        body = await _response_json(response)
    return dict(body["artifact"])


async def _operation_watch(
    session: ClientSession,
    base: str,
    ssl_value: Any,
    operation_id: str,
    *,
    interval: float,
) -> dict[str, Any]:
    terminal = {"succeeded", "failed", "cancelled", "interrupted", "outcome_unknown"}
    while True:
        async with session.get(base + f"/v1/operations/{operation_id}", ssl=ssl_value) as response:
            body = await _response_json(response)
        operation = body.get("operation", body)
        if operation.get("status") in terminal:
            return operation
        await asyncio.sleep(max(0.1, interval))


async def _ctl(args: argparse.Namespace) -> int:
    profiles, profile = _profile(args)
    base = profile["url"]
    ssl_value = _client_ssl(profile, args.insecure)
    headers = _headers(profile)
    try:
        async with ClientSession(headers=headers) as session:
            if args.ctl_command == "login":
                password = os.environ.get("ESP_IRIS_DEVELOPER_PASSWORD") or getpass.getpass("Developer password: ")
                async with session.post(base + "/v1/auth/login", json={"password": password}, ssl=ssl_value) as response:
                    result = await _response_json(response)
                    cookie = response.cookies.get("esp_iris_session")
                    if cookie is None:
                        raise RuntimeError("gateway did not return a session cookie")
                    profile["session"] = cookie.value
                    profile.pop("name", None)
                    profiles.setdefault("profiles", {})[args.profile or profiles.get("default", "default")] = profile
                    _save_profiles(profiles)
                    _output(result, args.json)
                    return 0

            if args.ctl_command == "profile":
                profile.pop("name", None)
                name = args.profile or profiles.get("default", "default")
                profiles.setdefault("profiles", {})[name] = profile
                if args.make_default:
                    profiles["default"] = name
                _save_profiles(profiles)
                _output({"profile": name, **profile}, args.json)
                return 0

            command = args.ctl_command
            if command == "health":
                async with session.get(base + "/v1/health", ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "devices":
                async with session.get(base + "/v1/devices", ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "status":
                async with session.get(base + f"/v1/devices/{args.device}", ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "memory":
                async with session.get(base + f"/v1/devices/{args.device}/memory", ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "system-inventory":
                url = base + f"/v1/devices/{args.device}/system-inventory"
                async with session.get(url, ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "operation-reconcile":
                async with session.post(
                    base + f"/v1/operations/{args.operation_id}/reconcile", ssl=ssl_value
                ) as response:
                    _output(await _response_json(response), args.json)
            elif command in ("operation-status", "operation-watch"):
                if command == "operation-watch":
                    result = await _operation_watch(
                        session,
                        base,
                        ssl_value,
                        args.operation_id,
                        interval=args.interval,
                    )
                    _output({"operation": result}, args.json)
                else:
                    async with session.get(
                        base + f"/v1/operations/{args.operation_id}", ssl=ssl_value
                    ) as response:
                        _output(await _response_json(response), args.json)
            elif command == "host-operation":
                from .host_worker import publish_request
                spec = json.loads(pathlib.Path(args.request_file).read_text(encoding="utf-8"))
                request_id = publish_request(spec)
                url = base + "/v1/host-operations"
                try:
                    async with session.post(url, json={"request_id": request_id}, ssl=ssl_value) as response:
                        _output(await _response_json(response), args.json)
                except Exception as exc:
                    raise RuntimeError(f"host operation {request_id}: {exc}; inspect this operation ID before retrying") from exc
            elif command == "crash":
                url = base + f"/v1/devices/{args.device}/crashes"
                async with session.get(url, ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "crash-archive":
                url = base + f"/v1/devices/{args.device}/crashes/archive"
                async with session.post(url, ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "coredump":
                url = base + f"/v1/devices/{args.device}/crashes/core-dump"
                async with session.get(url, ssl=ssl_value) as response:
                    if response.status >= 400:
                        await _response_json(response)
                    if response.content_type != "application/octet-stream":
                        raise RuntimeError(
                            f"unexpected coredump content type: {response.content_type}"
                        )
                    data = await response.read()
                    output = pathlib.Path(args.output).expanduser().resolve()
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(data)
                    _output(
                        {
                            "device_id": args.device,
                            "path": str(output),
                            "bytes": len(data),
                            "sha256": response.headers.get("X-ESP-Iris-SHA256", ""),
                        },
                        args.json,
                    )
            elif command == "input":
                gesture = json.loads(args.gesture)
                if not isinstance(gesture, dict):
                    raise ValueError("gesture must be a JSON object")
                url = base + f"/v1/devices/{args.device}/input"
                async with session.post(
                    url,
                    json=gesture,
                    headers={"X-Operation-ID": str(uuid.uuid4())},
                    ssl=ssl_value,
                ) as response:
                    _output(await _response_json(response), args.json)
            elif command == "mode":
                if args.value:
                    async with session.put(base + "/v1/mode", json={"mode": args.value}, ssl=ssl_value) as response:
                        _output(await _response_json(response), args.json)
                else:
                    async with session.get(base + "/v1/mode", ssl=ssl_value) as response:
                        _output(await _response_json(response), args.json)
            elif command in ("rpc", "rpc-raw"):
                if command == "rpc":
                    payload = json.loads(args.params)
                    url = base + f"/v1/devices/{args.device}/rpc/{args.method}"
                    body = {"params": payload, "deadline_ms": args.deadline_ms}
                else:
                    url = base + f"/v1/devices/{args.device}/rpc/raw"
                    body = {
                        "service_id": int(args.service_id, 0),
                        "method_id": int(args.method_id, 0),
                        "deadline_ms": args.deadline_ms,
                    }
                    if args.payload_base64_stdin:
                        body["payload_base64"] = sys.stdin.read().strip()
                    else:
                        body["payload_text"] = args.payload
                async with session.post(url, json=body, headers={"X-Operation-ID": str(uuid.uuid4())}, ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "restart":
                url = base + f"/v1/devices/{args.device}/restart"
                async with session.post(url, json={"delay_ms": args.delay_ms}, headers={"X-Operation-ID": str(uuid.uuid4())}, ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command == "console":
                url = base + f"/v1/devices/{args.device}/console"
                async with session.post(
                    url,
                    json={"line": " ".join(args.line)},
                    headers={"X-Operation-ID": str(uuid.uuid4())},
                    ssl=ssl_value,
                ) as response:
                    _output(await _response_json(response), args.json)
            elif command == "factory":
                url = base + f"/v1/devices/{args.device}/factory-recovery"
                async with session.post(
                    url,
                    json={"wait": args.wait, "timeout": args.wait_timeout},
                    headers={"X-Operation-ID": str(uuid.uuid4())},
                    ssl=ssl_value,
                ) as response:
                    _output(await _response_json(response), args.json)
            elif command in ("jobs", "cancel"):
                url = base + f"/v1/devices/{args.device}/jobs/{args.job_id}"
                method = session.delete if command == "cancel" else session.get
                async with method(url, headers={"X-Operation-ID": str(uuid.uuid4())}, ssl=ssl_value) as response:
                    _output(await _response_json(response), args.json)
            elif command in ("firmware-add", "ota"):
                paths = _firmware_paths(args)
                artifact = await _upload_firmware_artifact(
                    session, base, ssl_value, paths
                )
                if command == "firmware-add":
                    _output({"artifact": artifact}, args.json)
                else:
                    url = base + f"/v1/devices/{args.device}/ota"
                    operation_id = str(uuid.uuid4())
                    async with session.post(
                        url,
                        json={
                            "artifact_id": artifact["artifact_id"],
                            "execution_mode": args.execution_mode,
                            "validation_mode": args.validation_mode,
                            "compatibility": args.compatibility,
                            "preconditions": args.preconditions,
                        },
                        headers={"X-Operation-ID": operation_id},
                        ssl=ssl_value,
                    ) as response:
                        accepted = await _response_json(response)
                    if args.wait:
                        operation = await _operation_watch(
                            session,
                            base,
                            ssl_value,
                            operation_id,
                            interval=args.interval,
                        )
                        accepted["operation"] = operation
                    _output(accepted, args.json)
                    if args.wait and operation["status"] != "succeeded":
                        return 1
            elif command == "system-update":
                bundle_path = pathlib.Path(args.bundle).expanduser().resolve()
                if not bundle_path.is_file():
                    raise ValueError(f"system-update bundle does not exist: {bundle_path}")
                operation_id = str(uuid.uuid4())
                url = base + f"/v1/devices/{args.device}/system-update"
                async with session.post(
                    url,
                    data=bundle_path.read_bytes(),
                    headers={
                        "Content-Type": "application/vnd.esp-iris.system-update+zip",
                        "X-Iris-Compatibility": json.dumps(args.compatibility),
                        "X-Operation-ID": operation_id,
                    },
                    ssl=ssl_value,
                ) as response:
                    accepted = await _response_json(response)
                if args.wait:
                    operation = await _operation_watch(
                        session,
                        base,
                        ssl_value,
                        operation_id,
                        interval=args.interval,
                    )
                    accepted["operation"] = operation
                _output(accepted, args.json)
                if args.wait and operation["status"] != "succeeded":
                    return 1
            elif command == "screenshot":
                url = base + f"/v1/devices/{args.device}/screenshot?save=true"
                body = {
                    key: value
                    for key, value in {
                        "width": args.width,
                        "height": args.height,
                    }.items()
                    if value is not None
                }
                async with session.post(
                    url,
                    json=body,
                    headers={"X-Operation-ID": str(uuid.uuid4())},
                    ssl=ssl_value,
                ) as response:
                    if response.status >= 400:
                        await _response_json(response)
                    data = await response.read()
                    output = pathlib.Path(args.output).expanduser().resolve()
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(data)
                    metadata = json.loads(response.headers.get("X-ESP-Iris-Media", "{}"))
                    _output(
                        {
                            "device_id": args.device,
                            "path": str(output),
                            "bytes": len(data),
                            "content_type": response.headers.get("Content-Type", ""),
                            "operation_id": response.headers.get("X-Operation-ID", ""),
                            "saved_artifact": response.headers.get("X-Saved-Artifact", ""),
                            "description": metadata,
                        },
                        args.json,
                    )
            elif command == "mirror":
                url = base + f"/v1/devices/{args.device}/mirror/{args.action}"
                mirror_body: dict[str, Any] = {"channel": args.channel}
                if args.action == "start":
                    mirror_body["fps"] = args.fps
                async with session.post(
                    url,
                    json=mirror_body,
                    headers={"X-Operation-ID": str(uuid.uuid4())},
                    ssl=ssl_value,
                ) as response:
                    _output(await _response_json(response), args.json)
            elif command == "logs":
                await _logs(session, base, ssl_value, args)
            else:
                raise RuntimeError(f"unsupported ctl command: {command}")
    except ClientConnectorCertificateError as exc:
        raise RuntimeError("TLS certificate is not trusted; configure --ca or --fingerprint") from exc
    return 0


async def _logs(session: ClientSession, base: str, ssl_value: Any, args: argparse.Namespace) -> None:
    query = {"categories": "log"}
    if args.device:
        query["device_id"] = args.device
    async with session.get(base + "/v1/events?" + urlencode(query), ssl=ssl_value) as response:
        history = await _response_json(response)
    for item in history["events"]:
        _output(item if args.json else item.get("text", item), args.json)
    if not args.follow:
        return
    parts = urlparse(base)
    ws_base = urlunparse(("wss" if parts.scheme == "https" else "ws", parts.netloc, "", "", "", ""))
    query.update(cursor=str(history["next_cursor"]))
    async with session.ws_connect(ws_base + "/v1/events/ws?" + urlencode(query), ssl=ssl_value, heartbeat=20) as websocket:
        async for message in websocket:
            if message.type == WSMsgType.TEXT:
                item = json.loads(message.data)
                if item.get("category") == "log":
                    _output(item if args.json else item.get("text", item), args.json)


def _doctor(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "python_supported": sys.version_info >= (3, 8),
        "devices": [],
    }
    try:
        report["devices"] = [
            {
                "path": port.path,
                "vid": f"{port.vid:04x}",
                "pid": f"{port.pid:04x}",
                "serial_number": port.serial_number,
                "product": port.product,
                "transport": port.transport,
            }
            for port in discover_iris_usb_devices(include_usb_serial_jtag=True)
        ]
    except ImportError:
        report["pyserial"] = False
    _output(report, args.json)
    return 0 if report["python_supported"] else 1


def _bundle(args: argparse.Namespace) -> int:
    if args.bundle_command == "build":
        manifest_path = pathlib.Path(args.manifest).expanduser().resolve()
        signing_key = (
            pathlib.Path(args.signing_key).expanduser().resolve().read_bytes()
            if args.signing_key
            else None
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        output = build_system_update_bundle(
            pathlib.Path(args.output).expanduser().resolve(),
            manifest,
            pathlib.Path(args.component_root).expanduser().resolve(),
            signing_private_key=signing_key,
            signing_key_password=(
                pathlib.Path(args.signing_key_password_file)
                .expanduser()
                .resolve()
                .read_bytes()
                .rstrip(b"\r\n")
                if args.signing_key_password_file
                else None
            ),
        )
        _output({"path": str(output), "bytes": output.stat().st_size}, args.json)
        return 0
    trust_key = (
        pathlib.Path(args.trust_key).expanduser().resolve().read_bytes()
        if args.trust_key
        else None
    )
    bundle = load_system_update_bundle(
        pathlib.Path(args.bundle).expanduser().resolve(),
        trusted_public_key=trust_key,
    )
    _output(bundle.as_dict(), args.json)
    return 0


def _add_common_ctl(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile")
    parser.add_argument("--url")
    parser.add_argument("--ca")
    parser.add_argument("--fingerprint")
    parser.add_argument("--insecure", action="store_true", help="disable TLS verification for a trusted LAN test")
    parser.add_argument("--json", action="store_true", help="stable machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="esp_iris.py")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    web_parser = subparsers.add_parser("web", help="run USB Hub, gateway, and frontend")
    web_parser.add_argument("--tcp", action="append", default=[], metavar="HOST[:PORT]")
    web_parser.add_argument("--pairing-token", default=os.environ.get("ESP_IRIS_PAIRING_TOKEN"))
    web_parser.add_argument("--usb", action="append", default=[], metavar="PORT")
    web_parser.add_argument(
        "--usb-serial-jtag", action="append", default=[], metavar="PORT"
    )
    web_parser.add_argument("--discover-usb", action=BooleanOptionalAction, default=None,
                            help="discover USB devices (default: enabled unless endpoints are explicit)")
    web_parser.add_argument(
        "--discover-usb-serial-jtag",
        action=BooleanOptionalAction,
        default=False,
        help="also probe Espressif USB Serial/JTAG ports; disabled by default",
    )
    web_parser.add_argument("--usb-discovery-interval", type=float, default=1.0)
    web_parser.add_argument(
        "--discover-mdns",
        action=BooleanOptionalAction,
        default=None,
        help="automatically discover _esp-iris._tcp devices on the local network",
    )
    web_parser.add_argument("--listen", default="127.0.0.1")
    web_parser.add_argument("--port", type=int, default=8443)
    web_parser.add_argument("--instance-id", default="default")
    web_parser.add_argument("--ota-health-timeout", type=float,
                            help="override product HELLO health timeout, seconds (1..600)")
    web_parser.add_argument("--state-dir")
    web_parser.add_argument("--project-session-id")
    web_parser.add_argument("--project-id")
    web_parser.add_argument("--project-path")
    web_parser.add_argument("--project-connection-file")
    web_parser.add_argument("--ownership-dir")
    web_parser.add_argument("--owner-stdin", action="store_true")
    web_parser.add_argument("--project-persistent", action="store_true")
    web_parser.add_argument("--project-shared", action="store_true", help="Use shared client leases and 10-second idle shutdown")
    web_parser.add_argument("--project-workspace")
    web_parser.add_argument("--password-file")
    web_parser.add_argument(
        "--require-local-auth",
        action="store_true",
        help="also require password or Agent Token authentication on loopback",
    )
    web_parser.add_argument("--tls-cert")
    web_parser.add_argument("--tls-key")
    web_parser.add_argument("--tls", action="store_true", help="enable HTTPS, generating a certificate when needed")
    web_parser.add_argument("--no-tls", action="store_true", help="explicitly select the default HTTP transport")
    web_parser.add_argument("--demo", action="store_true", help="run a labeled multi-device virtual fleet without USB")
    web_parser.add_argument(
        "--system-update-trust-key",
        help="PEM ECDSA P-256 public key trusted for signed .irisfw bundles",
    )

    ctl = subparsers.add_parser("ctl", help="gateway-only developer and Agent CLI")
    _add_common_ctl(ctl)
    commands = ctl.add_subparsers(dest="ctl_command", required=True)
    commands.add_parser("login")
    profile = commands.add_parser("profile")
    profile.add_argument("--make-default", action="store_true")
    commands.add_parser("devices")
    commands.add_parser("health")
    reconcile = commands.add_parser("operation-reconcile", help="read-only device reconciliation")
    reconcile.add_argument("operation_id")
    status = commands.add_parser("status")
    status.add_argument("device")
    memory = commands.add_parser("memory", help="read live heap and task stack watermarks")
    memory.add_argument("device")
    system_inventory = commands.add_parser("system-inventory")
    system_inventory.add_argument("device")
    crash = commands.add_parser("crash")
    crash.add_argument("device")
    crash_archive = commands.add_parser("crash-archive")
    crash_archive.add_argument("device")
    coredump = commands.add_parser("coredump")
    coredump.add_argument("device")
    coredump.add_argument("output")
    pointer_input = commands.add_parser("input")
    pointer_input.add_argument("device")
    pointer_input.add_argument(
        "--gesture",
        required=True,
        help="normalized begin/moves/end gesture JSON (coordinates 0..10000)",
    )
    logs = commands.add_parser("logs")
    logs.add_argument("--device")
    logs.add_argument("--follow", action="store_true")
    rpc = commands.add_parser("rpc")
    rpc.add_argument("device")
    rpc.add_argument("method")
    rpc.add_argument("--params", default="{}")
    rpc.add_argument("--deadline-ms", type=int, default=1000)
    raw = commands.add_parser("rpc-raw")
    raw.add_argument("device")
    raw.add_argument("service_id")
    raw.add_argument("method_id")
    raw.add_argument("--payload", default="")
    raw.add_argument(
        "--payload-base64-stdin",
        action="store_true",
        help="read a base64 RPC payload from stdin instead of argv",
    )
    raw.add_argument("--deadline-ms", type=int, default=1000)
    console = commands.add_parser("console")
    console.add_argument("device")
    console.add_argument("line", nargs="+")
    ota = commands.add_parser("ota")
    ota.add_argument("device")
    ota.add_argument("image")
    ota.add_argument("--elf")
    ota.add_argument("--map")
    ota.add_argument(
        "--execution-mode",
        choices=("recovery", "application"),
        default="recovery",
    )
    ota.add_argument(
        "--validation-mode",
        choices=OTA_VALIDATION_MODES,
        default=DEFAULT_OTA_VALIDATION_MODE,
        help="post-reboot firmware identity comparison (default: elf_sha256)",
    )
    ota.add_argument("--wait", action="store_true")
    ota.add_argument("--interval", type=float, default=0.5)
    ota.add_argument("--preconditions-json", dest="preconditions", default={},
                     type=lambda value: recovery_preconditions(json.loads(value)),
                     help="Required live Recovery version and partition table hash, checked before writing")
    system_update = commands.add_parser("system-update")
    system_update.add_argument("device")
    system_update.add_argument("bundle")
    system_update.add_argument("--wait", action="store_true")
    system_update.add_argument("--interval", type=float, default=0.5)
    for update_parser in (ota, system_update):
        update_parser.add_argument(
            "--compatibility-json", dest="compatibility", default={},
            type=lambda value: compatibility_expectation(json.loads(value)),
            help="explicit expected chip_target/product_contract/board_id/layout_id/recovery_abi JSON object",
        )
    firmware_add = commands.add_parser("firmware-add")
    firmware_add.add_argument("image")
    firmware_add.add_argument("--elf")
    firmware_add.add_argument("--map")
    operation_status = commands.add_parser("operation-status")
    operation_status.add_argument("operation_id")
    operation_watch = commands.add_parser("operation-watch")
    operation_watch.add_argument("operation_id")
    operation_watch.add_argument("--interval", type=float, default=0.5)
    host_operation = commands.add_parser("host-operation")
    host_operation.add_argument("request_file")

    screenshot = commands.add_parser("screenshot")
    screenshot.add_argument("device")
    screenshot.add_argument("output")
    screenshot.add_argument("--width", type=int)
    screenshot.add_argument("--height", type=int)
    mirror = commands.add_parser("mirror")
    mirror.add_argument("device")
    mirror.add_argument("action", choices=("start", "stop"))
    mirror.add_argument("--channel", choices=("screen", "image", "audio"), default="screen")
    mirror.add_argument("--fps", type=int, default=5)
    restart = commands.add_parser("restart")
    restart.add_argument("device")
    restart.add_argument("--delay-ms", type=int, default=250)
    factory = commands.add_parser("factory")
    factory.add_argument("device")
    factory.add_argument("--wait", action="store_true", help="Wait for the same device in Recovery with a new Boot ID")
    factory.add_argument("--wait-timeout", type=float, default=30)
    jobs = commands.add_parser("jobs")
    jobs.add_argument("device")
    jobs.add_argument("job_id", type=int)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("device")
    cancel.add_argument("job_id", type=int)
    mode = commands.add_parser("mode")
    mode.add_argument("value", nargs="?", choices=("develop", "observe"))

    bundle = subparsers.add_parser(
        "bundle", help="build or inspect system-update bundles"
    )
    bundle.add_argument("--json", action="store_true")
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_build = bundle_commands.add_parser("build")
    bundle_build.add_argument("manifest")
    bundle_build.add_argument("--component-root", required=True)
    bundle_build.add_argument(
        "--signing-key",
        help="optional PEM ECDSA P-256 signing key; omit for unsigned bundles",
    )
    bundle_build.add_argument(
        "--signing-key-password-file",
        help="file containing the encrypted PEM password (never pass it as an argument)",
    )
    bundle_build.add_argument("--output", required=True)
    bundle_inspect = bundle_commands.add_parser("inspect")
    bundle_inspect.add_argument("bundle")
    bundle_inspect.add_argument(
        "--trust-key",
        help="optional PEM ECDSA P-256 trust key; omit for unsigned bundles",
    )

    doctor = subparsers.add_parser("doctor", help="cross-platform environment and USB diagnostics")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 8):  # noqa: UP036 - retain an explicit startup guard
        print("ESP-Iris gateway requires Python 3.8 or newer", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "bundle":
            return _bundle(args)
        if args.command == "ctl":
            return asyncio.run(_ctl(args))
        asyncio.run(_web(args))
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"esp_iris.py: error: {exc}", file=sys.stderr)
        return 2
    return 0


__all__ = ["build_parser", "main"]
