"""Local project-session HTTP control and device admission."""
from __future__ import annotations

import asyncio
import collections
import re
import time
import uuid
from typing import Any, NoReturn

from aiohttp import web

from .client_lifecycle import ClientLifecycle
from .compat import to_thread
from .device_selection import admit_candidates, identity_candidates
from .discovery import (
    discover_iris_usb_devices,
    iris_usb_allowed,
    resolve_usb_port,
    usb_endpoint,
)
from .host_worker import active_workers
from .http_support import error_response, request_is_loopback
from .ownership import CAPABILITY, OwnershipConflict, OwnershipRegistry
from .takeover import CAPABILITY as TAKEOVER_CAPABILITY
from .takeover import busy_error, drain_device, drain_timeout, public_record
from .takeover import register_routes as register_takeover_routes


class ProjectGateway:
    capability = CAPABILITY

    def __init__(self, registry: OwnershipRegistry, service: Any, hub: Any,
                 stop: asyncio.Event, *, pairing_token: str | None = None, shared: bool = True) -> None:
        self.registry, self.service, self.hub = registry, service, hub
        self.stop = stop
        self.pairing_token = pairing_token
        self.shared = shared
        self.tokens: dict[str, str] = {}
        self.closing = False
        self.drain_requested = asyncio.Event()
        self.drain_timed_out = False
        self.active: dict[str, int] = collections.defaultdict(int)
        self.blocked: set[str] = set()
        self.control_lock = asyncio.Lock()
        self.device_control: dict[str, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
        self.jobs = service.jobs
        self.clients = ClientLifecycle()
        self.control_requests = 0
        self.streams = 0

    def keepalive_reasons(self) -> dict[str, int]:
        workers = active_workers()
        resources = {value for claim in (self.registry.claims() if workers else [])
                     if claim["owner"] == self.registry.session_id
                     for value in (claim["resource"], claim.get("device_id")) if value}
        return {"requests": sum(self.active.values()) + self.control_requests,
                "operations": len(self.service.operations._pending),
                "host_workers": sum(bool(resources.intersection(worker["resources"])) for worker in workers),
                "jobs": len(self.jobs),
                "mirrors": len(self.hub._mirror_states), "streams": self.streams}

    async def idle_shutdown(self) -> None:
        while not self.closing:
            if self.clients.should_stop(self.keepalive_reasons()):
                self.request_stop()
                return
            await asyncio.sleep(0.1)

    def observe(self, event: dict[str, Any]) -> None:
        self.service.observe_device_activity(event)

    def busy(self, device_id: str | None = None) -> bool:
        if device_id is None:
            return any(self.keepalive_reasons().values())
        return bool(self.busy_reasons(device_id))

    def busy_reasons(self, device_id: str) -> list[dict[str, Any]]:
        state = self.service.operations.queue_state(device_id)
        reasons = [{"kind": "operation", "operation_id": value}
                   for value in state["running"] + state["queued"]]
        for worker in active_workers():
            if device_id in worker["resources"] and not any(
                reason.get("operation_id") == worker["operation_id"] for reason in reasons
            ):
                reasons.append({"kind": "operation", "operation_id": worker["operation_id"]})
        if self.active[device_id]:
            reasons.append({"kind": "request", "count": self.active[device_id]})
        reasons.extend({"kind": "mirror", "channel": channel} for channel in self.hub.active_mirrors(device_id))
        reasons.extend({"kind": "job", "job_id": key[1]} for key in self.jobs if key[0] == device_id)
        return reasons

    def request_stop(self) -> None:
        self.closing = True
        self.clients.closing = True
        self.drain_requested.set()

    async def drain(self, timeout: float = 900) -> None:
        await self.drain_requested.wait()
        deadline = time.monotonic() + timeout
        while self.busy() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        self.drain_timed_out = self.busy()
        self.stop.set()

    @web.middleware
    async def guard(self, request: web.Request, handler: Any) -> web.StreamResponse:
        device_id = request.match_info.get("device_id")
        if not device_id:
            # Status/health and client lease control are passive. All business
            # mutations retain a reference even if their HTTP client disappears.
            work = request.method not in {"GET", "HEAD", "OPTIONS"} and not request.path.startswith("/v1/project/clients")
            if not work:
                return await handler(request)
            if self.closing:
                raise OwnershipConflict("project session is draining")
            self.control_requests += 1
            self.clients.idle_since = None
            try:
                return await handler(request)
            finally:
                self.control_requests -= 1
        device_id = self.service.resolve_device(device_id)
        if self.closing or device_id in self.blocked:
            raise OwnershipConflict("project session or device is draining")
        self.registry._require("device:" + device_id, ("owned",))
        # Streams keep the Gateway available but don't block device transfer.
        if request.method == "GET" and "/streams/" in request.path:
            self.streams += 1
            self.clients.idle_since = None
            try:
                return await handler(request)
            finally:
                self.streams -= 1
        self.active[device_id] += 1
        self.clients.idle_since = None
        try:
            return await handler(request)
        finally:
            self.active[device_id] -= 1

    def snapshot(self) -> dict[str, Any]:
        claims = self.registry.claims()
        return {"session": self.registry.session(self.registry.session_id),
                "capability": CAPABILITY, "capabilities": [TAKEOVER_CAPABILITY], "closing": self.closing,
                "lifecycle": self.clients.snapshot(self.keepalive_reasons()) if self.shared else None,
                "pairing_configured": bool(self.pairing_token),
                "busy": self.busy(), "sessions": self.registry.sessions(),
                "claims": claims, "endpoints": self.service.list_endpoints(),
                "takeovers": [public_record(self.registry.transfer(key)) for key in sorted({
                    item["transfer_id"] for item in claims if item["transfer_id"]
                })]}

    async def wait_device(self, device_id: str | None, endpoint: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + min(30.0, max(0.1, timeout))
        while time.monotonic() < deadline:
            for item in self.hub.list_devices():
                if (device_id and item["device_id"] == device_id) or (
                    not device_id and item.get("endpoint") == endpoint
                ):
                    return item
            await asyncio.sleep(0.05)
        raise TimeoutError("device has not completed identity validation; ownership is retained")

    @staticmethod
    def selection_error(message: str, candidates: list[Any]) -> NoReturn:
        response = error_response(400, "selection_error", message, candidates=candidates)
        raise web.HTTPBadRequest(text=response.text, content_type="application/json")

    def owned_target(self) -> dict[str, str] | None:
        """Select ownership, including offline devices; never discover or acquire."""
        groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for claim in self.registry.claims():
            if claim["owner"] == self.registry.session_id:
                key = "device:" + claim["device_id"] if claim["device_id"] else claim["resource"]
                groups[key].append(claim)
        if len(groups) > 1:
            self.selection_error("Multiple owned devices; specify --device-id or --endpoint.", sorted(groups))
        if not groups:
            return None
        key, claims = next(iter(groups.items()))
        for claim in claims:
            self.registry._require(claim["resource"], ("owned",))
        return {"device_id": claims[0]["device_id"]} if claims[0]["device_id"] else {"endpoint": key}

    async def automatic_target(self) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
        """Prefer this session's live/owned device, otherwise one live free USB endpoint.

        Enumerate descriptors at request time: cached endpoints and mDNS adverts
        are not evidence of a uniquely attached USB device. This runs under the
        project control lock; the registry still arbitrates cross-project races.
        """
        connected = self.hub.list_devices()
        if len(connected) > 1:
            self.selection_error("Multiple connected devices; specify --device-id.",
                                 sorted(item["device_id"] for item in connected))
        if connected:
            device_id = str(connected[0]["device_id"])
            self.registry._require("device:" + device_id, ("owned",))
            return {"device_id": device_id}, None
        owned = self.owned_target()
        if owned is not None:
            return owned, None
        candidates = {}
        blocked = []
        for device in await to_thread(discover_iris_usb_devices):
            metadata = {
                "path": device.path, "device_path": device.device,
                "vid": device.vid, "pid": device.pid, "product": device.product,
                "serial_number": device.serial_number, "location": device.location,
            }
            if not iris_usb_allowed(metadata):
                continue
            endpoint = usb_endpoint(metadata)
            metadata["endpoint"] = endpoint
            claim = self.registry.claim(endpoint)
            if claim is not None:
                blocked.append(f"{endpoint} (owner={claim['owner']}, state={claim['state']})")
            else:
                candidates[endpoint] = metadata
        if len(candidates) > 1:
            self.selection_error("Multiple available USB devices; specify --endpoint.", sorted(candidates))
        if candidates:
            endpoint, metadata = next(iter(candidates.items()))
            return {"endpoint": endpoint}, metadata
        if blocked:
            raise OwnershipConflict("USB devices are reserved: " + ", ".join(blocked))
        return None, None

    async def acquire(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if self.closing:
            raise OwnershipConflict("project session is draining")
        device_id = body.get("device_id")
        if device_id and (not isinstance(device_id, str) or re.fullmatch(r"[0-9a-f]{32}", device_id) is None):
            raise ValueError("Device ID must contain 32 lowercase hexadecimal characters")
        endpoint = body.get("endpoint")
        token = body.get("pairing_token")
        if token is not None and (not isinstance(token, str) or len(bytes.fromhex(token)) != 32):
            raise ValueError("pairing token must contain 32 bytes")
        automatic = bool(body.get("auto")) and not device_id and not endpoint
        automatic_metadata = None
        if automatic:
            target, automatic_metadata = await self.automatic_target()
            if target is None:
                if body.get("allow_none"):
                    return None
                raise LookupError("No available ESP-Iris USB device was found.")
            device_id, endpoint = target.get("device_id"), target.get("endpoint")
        elif not device_id and not endpoint:
            raise ValueError("select --device-id or --endpoint, or request automatic selection")
        if device_id in self.blocked:
            raise OwnershipConflict("device is draining for handoff")
        if device_id:
            claim = self.registry.claim("device:" + str(device_id))
            if claim:
                self.registry._require("device:" + str(device_id), ("owned",))
                connected = next((item for item in self.hub.list_devices() if item["device_id"] == device_id), None)
                if connected is not None:
                    if endpoint and endpoint != connected.get("endpoint"):
                        canonical = endpoint
                        if str(connected.get("endpoint", "")).startswith("usb:") and not str(endpoint).startswith("tcp:"):
                            canonical = usb_endpoint(await to_thread(resolve_usb_port, str(endpoint)))
                        if canonical != connected.get("endpoint"):
                            self.selection_error("Requested endpoint differs from the active device connection.", [connected])
                    return connected
                if automatic:
                    # Its existing supervisors own reconnect. Never substitute
                    # a different board while this identity is temporarily absent.
                    return await self.wait_device(str(device_id), "", float(body.get("timeout", 10)))
        if endpoint:
            claim = self.registry.claim(str(endpoint))
            if claim and claim.get("device_id") in self.blocked:
                raise OwnershipConflict("device is draining for handoff")
        candidates = self.hub.list_endpoints()
        if automatic_metadata is not None:
            candidates = [automatic_metadata]
        elif endpoint:
            candidates = [item for item in candidates if item["endpoint"] == endpoint or item.get("path") == endpoint]
            if not candidates and str(endpoint).startswith("tcp:"):
                host, port = str(endpoint)[4:].rsplit(":", 1)
                if not host or not 1 <= int(port) <= 65535:
                    raise ValueError("invalid TCP endpoint")
                candidates = [{"endpoint": endpoint}]
            elif not candidates:
                metadata = await to_thread(resolve_usb_port, str(endpoint))
                if not iris_usb_allowed(metadata, explicit=True):
                    raise ValueError("endpoint is reserved for the ROM recovery workflow")
                candidates = [{"endpoint": usb_endpoint(metadata), **metadata}]
        else:
            candidates = await identity_candidates(self, str(device_id))
        for candidate in candidates:
            claim = self.registry.claim(candidate["endpoint"])
            if claim and claim.get("device_id") in self.blocked:
                raise OwnershipConflict("device is draining for handoff")
        if not candidates:
            self.selection_error("No available endpoint for the selected Device ID.", [])
        return await admit_candidates(self, candidates, str(device_id) if device_id else None,
                                      token, float(body.get("timeout", 10)))

    async def prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        device_id, target = str(body["device_id"]), str(body["target_session_id"])
        transfer_id = str(body.get("transfer_id") or uuid.uuid4())
        try:
            existing = self.registry.transfer(transfer_id)
        except KeyError:
            existing = None
        if existing:
            if (existing["device_id"], existing["source"], existing["target"]) != (
                device_id, self.registry.session_id, target,
            ):
                raise OwnershipConflict("transfer ID already describes a different request")
            if existing["state"] != "preparing":
                return existing
        self.registry._require("device:" + device_id, ("preparing",) if existing else ("owned",))
        if self.closing:
            raise OwnershipConflict("project session is draining")
        timeout = drain_timeout(body)
        if self.busy(device_id) and not body.get("force"):
            busy_error(self, device_id)
        self.blocked.add(device_id)
        try:
            if body.get("force"):
                await drain_device(self, device_id, timeout)
            self.registry.prepare(device_id, target, transfer_id)
            await self.hub.detach_owned(device_id)
            result = self.registry.offer(transfer_id)
            self.audit(result)
            return result
        finally:
            self.blocked.discard(device_id)

    def audit(self, transfer: dict[str, Any]) -> None:
        self.service.store.add_audit("project", self.registry.session_id,
                                     "device.transfer." + transfer["state"], transfer)

    async def accept(self, transfer_id: str) -> dict[str, Any]:
        if self.closing:
            raise OwnershipConflict("project session is draining")
        proposed = self.registry.transfer(transfer_id)
        if any(item["metadata"].get("pairing") == "hmac" for item in proposed["metadata"]["endpoints"]) and not self.pairing_token:
            raise OwnershipConflict("target requires its own pairing token before acceptance")
        result = self.registry.accept(transfer_id)
        if result["state"] == "completed":
            return result
        endpoints = result["metadata"]["endpoints"]
        # A single verified transport is sufficient. Credentials stay local.
        endpoint = endpoints[0]
        await self.hub.connect_owned(endpoint["resource"], endpoint["metadata"], pairing_token=self.pairing_token)
        await self.wait_device(result["device_id"], endpoint["resource"], 15)
        result = self.registry.complete(transfer_id, result["device_id"])
        self.audit(result)
        return result

    def register_routes(self, app: web.Application) -> None:
        async def clients(request: web.Request) -> web.Response:
            if not request_is_loopback(request):
                raise PermissionError("project client control is local-only")
            if not self.shared:
                raise OwnershipConflict("legacy Gateway requires its original lifetime owner")
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("client request must be an object")
            if body.get("session_id") != self.registry.session_id:
                raise OwnershipConflict("Gateway session changed; reconnect before submitting work")
            client_id = request.match_info.get("client_id")
            if client_id is None:
                return web.json_response(self.clients.register(body))
            token = str(body.get("lease_token", ""))
            if request.match_info["verb"] == "renew":
                return web.json_response(self.clients.renew(client_id, token))
            self.clients.release(client_id, token)
            return web.json_response({"released": client_id})

        register_takeover_routes(app, self)
        app.router.add_post("/v1/project/clients", clients)
        app.router.add_post("/v1/project/clients/{client_id}/{verb:renew|release}", clients)

        async def handle(request: web.Request) -> web.Response:
            if not request_is_loopback(request):
                raise PermissionError("project session control is local-only")
            action = request.match_info.get("action", "status")
            if request.method == "GET":
                return web.json_response(self.snapshot())
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("project request must be an object")
            async with self.control_lock:
                if action == "release" and body.get("auto") and not (body.get("device_id") or body.get("endpoint")):
                    target = self.owned_target()
                    if target is None:
                        self.selection_error("No uniquely identified owned device is available.", [])
                    body.update(target)
                if action == "acquire":
                    return web.json_response({"device": await self.acquire(body)})
                if action == "release":
                    resource = str(body.get("endpoint") or ("device:" + str(body["device_id"])))
                    claim = self.registry._require(resource, ("owned",))
                    device_id = claim["device_id"]
                    async with self.device_control[device_id or resource]:
                        self.registry._require(resource, ("owned",))
                        reasons = self.keepalive_reasons()
                        reasons["requests"] = max(0, reasons["requests"] - 1)
                        busy = self.busy(device_id) if device_id else any(reasons.values())
                        if busy:
                            raise OwnershipConflict("device is busy")
                        self.blocked.add(device_id or resource)
                        try:
                            if device_id:
                                await self.hub.detach_owned(device_id)
                            else:
                                await self.hub._remove_endpoint(resource)
                            self.registry.release(resource)
                        finally:
                            self.blocked.discard(device_id or resource)
                        return web.json_response({"released": resource})
                if action == "reconcile":
                    resource = str(body.get("endpoint") or ("device:" + str(body["device_id"])))
                    self.registry.reconcile_orphan(resource)
                    return web.json_response({"reconciled": resource})
                raise ValueError("unknown project action")

        app.router.add_get("/v1/project", handle)
        app.router.add_post("/v1/project/{action:acquire|release|reconcile}", handle)
