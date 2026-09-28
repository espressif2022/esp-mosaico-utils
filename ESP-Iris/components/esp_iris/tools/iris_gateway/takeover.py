"""Receiver-initiated handoff, using the existing reserved transfer protocol."""
from __future__ import annotations

import asyncio
import math
import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .compat import to_thread
from .device_activity import stop_session_work
from .discovery import resolve_usb_port, usb_endpoint
from .http_support import error_response, json_body, request_is_loopback
from .ownership import OwnershipConflict

CAPABILITY = "device-takeover/v1"


def drain_timeout(body: dict) -> float:
    value = float(body.get("timeout", 120))
    if not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError("handoff timeout must be between zero and 3600 seconds")
    if not isinstance(body.get("force", False), bool):
        raise TypeError("force must be a boolean")
    return value


def busy_error(project: Any, device_id: str, *, timeout: bool = False) -> None:
    response = error_response(
        409, "handoff_timeout" if timeout else "device_busy",
        "Handoff timed out; ownership retained and the active write was not interrupted."
        if timeout else "Device is busy; use force to stop session work and wait for active writes.",
        device_id=device_id, busy_reasons=project.busy_reasons(device_id),
    )
    raise web.HTTPConflict(text=response.text, content_type="application/json")


async def drain_device(project: Any, device_id: str, timeout: float) -> None:
    """The caller closes admission until transfer or failure; writes are never cancelled."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        await project.service.operations.cancel_queued(device_id, reason="cancelled for device handoff")
        reasons = project.busy_reasons(device_id)
        if not any(reason["kind"] in {"operation", "request"} for reason in reasons):
            break
        if asyncio.get_running_loop().time() >= deadline:
            busy_error(project, device_id, timeout=True)
        await asyncio.sleep(0.05)
    try:
        await stop_session_work(project.service, device_id, max(0.001, deadline - asyncio.get_running_loop().time()))
    except (asyncio.TimeoutError, TimeoutError):
        busy_error(project, device_id, timeout=True)
    if project.busy(device_id):
        busy_error(project, device_id, timeout=True)


def public_record(record: dict) -> dict:
    """Expose the takeover API without leaking the internal transaction name."""
    return {"takeover_id": record["transfer_id"],
            **{key: value for key, value in record.items() if key != "transfer_id"}}


def local_session(project: Any, session_id: str) -> dict:
    session = project.registry.session(session_id)
    address = urlsplit(session["url"])
    if not session["alive"] or address.scheme != "http" or address.hostname != "127.0.0.1":
        raise OwnershipConflict("handoff requires a live local session; reconcile dead ownership explicitly")
    return session


async def peer_state(client: ClientSession, session: dict) -> dict:
    async with client.get(session["url"] + "/v1/project", allow_redirects=False) as response:
        state = await response.json()
        if (response.status != 200 or state.get("session", {}).get("session_id") != session["session_id"]
                or CAPABILITY not in state.get("capabilities", []) or state.get("closing")):
            raise OwnershipConflict("peer is unavailable, changed identity, or requires updated host tools")
        return state


async def request_handoff(project: Any, source_id: str, device_id: str, transfer_id: str, body: dict) -> None:
    timeout = drain_timeout(body)
    source = local_session(project, source_id)
    try:
        async with ClientSession(timeout=ClientTimeout(total=timeout + 30)) as client:
            await peer_state(client, source)
            request = {"device_id": device_id, "target_session_id": project.registry.session_id,
                       "source_session_id": source_id, "transfer_id": transfer_id,
                       "force": body.get("force", False), "timeout": timeout}
            async with client.post(source["url"] + "/v1/project/handoff/prepare", json=request,
                                   allow_redirects=False) as response:
                result = await response.json()
                if response.status != 200:
                    response_body = error_response(409, "takeover_rejected", "Owner could not hand off the device.",
                                                  takeover_id=transfer_id, cause=result.get("error", result))
                    raise web.HTTPConflict(text=response_body.text, content_type="application/json")
    except (ClientError, asyncio.TimeoutError) as error:
        raise OwnershipConflict(
            f"Takeover {transfer_id}: peer response unavailable; query its status before retrying"
        ) from error


async def resume(project: Any, transfer_id: str) -> dict:
    proposed = project.registry.transfer(transfer_id)
    if proposed["target"] != project.registry.session_id:
        raise OwnershipConflict("resume from the original receiving session; use reconcile if both sessions stopped")
    if proposed["state"] == "aborted":
        raise OwnershipConflict("takeover was aborted; start a new request")
    if proposed["state"] == "preparing" and project.registry.alive(proposed["source"]):
        await request_handoff(project, proposed["source"], proposed["device_id"], transfer_id, {})
    return await project.accept(transfer_id)


async def takeover(project: Any, body: dict) -> dict:
    """The receiver coordinates preparation and identity validation end to end."""
    drain_timeout(body)
    transfer_id = str(body.get("takeover_id") or uuid.uuid4())
    uuid.UUID(transfer_id)
    device_id, endpoint = body.get("device_id"), body.get("endpoint")
    if bool(device_id) == bool(endpoint):
        raise ValueError("select exactly one device_id or endpoint for takeover")
    if device_id and (not isinstance(device_id, str) or re.fullmatch(r"[0-9a-f]{32}", device_id) is None):
        raise ValueError("Device ID must contain 32 lowercase hexadecimal characters")
    if endpoint and not str(endpoint).startswith("tcp:"):
        endpoint = usb_endpoint(await to_thread(resolve_usb_port, str(endpoint)))
    try:
        previous = project.registry.transfer(transfer_id)
    except KeyError:
        previous = None
    if previous:
        endpoints = [item["resource"] for item in previous["metadata"]["endpoints"]]
        if (previous["target"] != project.registry.session_id
                or device_id and previous["device_id"] != device_id
                or endpoint and endpoint not in endpoints):
            raise OwnershipConflict("takeover ID already describes a different request")
        async with project.device_control[previous["device_id"]]:
            return {"takeover": public_record(await resume(project, transfer_id))}
    claim = project.registry.claim("device:" + device_id if device_id else str(endpoint))
    if claim is None or not claim.get("device_id"):
        raise OwnershipConflict("takeover requires an identified owned device; use claim for a free endpoint")
    device_id = claim["device_id"]
    if claim["owner"] == project.registry.session_id:
        async with project.control_lock:
            return {"device": await project.acquire({"device_id": device_id})}
    if claim["state"] != "owned":
        raise OwnershipConflict("device already has a reserved takeover; query its takeover ID")
    async with project.device_control[device_id]:
        await request_handoff(project, claim["owner"], device_id, transfer_id, body)
        # The durable reservation, not the HTTP response, is the authority.
        return {"takeover": public_record(await project.accept(transfer_id))}


def register_routes(app: web.Application, project: Any) -> None:
    def require_local(request: web.Request) -> None:
        if not request_is_loopback(request):
            raise PermissionError("project device handoff is local-only")

    async def start(request: web.Request) -> web.Response:
        require_local(request)
        body = await json_body(request)
        if set(body) - {"device_id", "endpoint", "takeover_id", "force", "timeout"}:
            raise ValueError("unexpected takeover parameter")
        return web.json_response(await takeover(project, body))

    async def record(request: web.Request) -> web.Response:
        require_local(request)
        transfer_id = request.match_info["takeover_id"]
        uuid.UUID(transfer_id)
        before = project.registry.transfer(transfer_id)
        if request.method == "GET":
            return web.json_response({"takeover": public_record(before)})
        action = request.match_info["verb"]
        async with project.device_control[before["device_id"]]:
            before = project.registry.transfer(transfer_id)
            if action == "resume":
                result = await resume(project, transfer_id)
            elif action == "abort":
                if before["source"] != project.registry.session_id:
                    raise OwnershipConflict("abort from the original owning project")
                if before["state"] == "completed":
                    raise OwnershipConflict("completed takeovers cannot be rolled back")
                if before["state"] != "aborted":
                    await project.hub.detach_owned(before["device_id"])
                result = project.registry.abort(transfer_id)
                project.audit(result)
            else:
                result = project.registry.reconcile_transfer(transfer_id)
                project.audit(result)
            return web.json_response({"takeover": public_record(result)})

    async def prepare(request: web.Request) -> web.Response:
        """Peer protocol only; a receiver requests release before validating locally."""
        require_local(request)
        body = await json_body(request)
        if body["source_session_id"] != project.registry.session_id:
            raise OwnershipConflict("owning session changed")
        uuid.UUID(str(body["transfer_id"]))
        target = local_session(project, str(body["target_session_id"]))
        if target["session_id"] == project.registry.session_id:
            raise OwnershipConflict("receiver must be a different session")
        async with ClientSession(timeout=ClientTimeout(total=5)) as client:
            state = await peer_state(client, target)
        claims = [item for item in project.registry.claims() if item["device_id"] == body["device_id"]]
        if any(item["metadata"].get("pairing") == "hmac" for item in claims) and not state.get("pairing_configured"):
            raise OwnershipConflict("configure the receiver's pairing token first")
        async with project.device_control[str(body["device_id"])]:
            return web.json_response({"handoff": await project.prepare(body)})

    app.router.add_post("/v1/project/takeovers", start)
    app.router.add_get("/v1/project/takeovers/{takeover_id}", record)
    app.router.add_post("/v1/project/takeovers/{takeover_id}/{verb:resume|abort|reconcile}", record)
    app.router.add_post("/v1/project/handoff/prepare", prepare)
