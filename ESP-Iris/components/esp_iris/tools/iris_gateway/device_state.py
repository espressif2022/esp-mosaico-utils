"""One presentation state, with ownership and firmware mode kept orthogonal."""
from __future__ import annotations

from typing import Any

from .discovery import resolve_usb_port

DEVICE_STATES = ("offline", "connecting", "idle", "busy", "needs_recovery")


def describe(service: Any, item: dict, *, workers: list[dict]) -> dict:
    device_id = str(item.get("device_id") or "")
    endpoint = str(item.get("endpoint") or "")
    queue = service.operations.queue_state(device_id or "endpoint::" + endpoint)
    reasons = [{"kind": "operation", "operation_id": value}
               for value in queue["running"] + queue["queued"]]
    hub = service.hub
    reasons.extend({"kind": "mirror", "channel": channel} for channel in (hub.active_mirrors(device_id) if hub else []))
    reasons.extend({"kind": "job", "job_id": key[1]}
                   for key in service.jobs if key[0] == device_id)
    for worker in workers:
        if ((device_id in worker["resources"] or endpoint in worker["resources"])
                and not any(reason.get("operation_id") == worker["operation_id"] for reason in reasons)):
            reasons.append({"kind": "operation", "operation_id": worker["operation_id"]})
    if service.project is not None and device_id in service.project.blocked:
        reasons.append({"kind": "handoff"})
    owner = None
    if service.project is not None:
        claim = service.project.registry.claim("device:" + device_id if device_id else endpoint)
        owner = claim["owner"] if claim else None
    mode = item.get("firmware_mode") or "unknown"
    endpoint_state: dict[str, Any] = next((value for value in (hub.list_endpoints() if hub else [])
                           if value.get("endpoint") == endpoint), {})
    if reasons:
        state = "busy"
    elif item.get("connected", endpoint_state.get("state") == "ready"):
        state = "idle"
    else:
        state = "connecting" if endpoint_state.get("state") in {
            "connecting", "negotiating", "handshaking"
        } else "offline"
        # Only current descriptors justify a ROM diagnosis. A failed operation,
        # stale firmware cache or ordinary reconnect timeout never does.
        if endpoint.startswith("usb:"):
            try:
                current = resolve_usb_port(endpoint)
            except OSError:
                pass
            else:
                if current.get("vid") == 0x303A and current.get("pid") == 0x0020:
                    mode, state = "rom", "needs_recovery"
                else:
                    state = "connecting"
    return {"state": state, "busy_reasons": reasons, "owner_session_id": owner,
            "firmware_mode": mode}
