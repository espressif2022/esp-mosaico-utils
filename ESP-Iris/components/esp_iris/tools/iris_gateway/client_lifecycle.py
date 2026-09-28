"""Event-loop-owned client leases and idle shutdown for a project Gateway."""
from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any

CAPABILITY = "project-client-lifecycle/v1"
IDLE_SECONDS = 10.0
LEASE_SECONDS = 20.0
RENEW_SECONDS = 5.0


class ClientLifecycle:
    def __init__(self, *, idle_seconds: float = IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.idle_since: float | None = clock()
        self.clients: dict[str, dict[str, Any]] = {}
        self.closing = False

    def register(self, body: dict[str, Any], *, connected: bool = False) -> dict[str, Any]:
        if self.closing:
            raise RuntimeError("project Gateway is draining; reconnect after it exits")
        client_id = str(body.get("client_id") or uuid.uuid4())
        uuid.UUID(client_id)
        kind = body.get("kind", "cli")
        if kind not in {"cli", "run", "workbench"}:
            raise ValueError("unknown project client kind")
        command = body.get("command", kind)
        pid = body.get("pid")
        if not isinstance(command, str) or len(command) > 160:
            raise ValueError("client command must be a short name without arguments")
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise ValueError("client PID must be a positive integer")
        token = body.get("lease_token") or secrets.token_hex(32)
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise ValueError("invalid client lease token")
        self.expire()
        if client_id in self.clients:
            self._require(client_id, token)
            return self.renew(client_id, token)
        now = time.time_ns()
        self.clients[client_id] = {
            "client_id": client_id, "kind": kind, "command": command, "pid": pid,
            "connected_ns": now, "last_seen_ns": now, "lease_token": token,
            "expires": None if connected else self.clock() + LEASE_SECONDS,
        }
        self.idle_since = None
        return {"client_id": client_id, "lease_token": token,
                "lease_seconds": LEASE_SECONDS, "renew_seconds": RENEW_SECONDS}

    def _require(self, client_id: str, token: str) -> dict[str, Any]:
        self.expire()
        client = self.clients[client_id]
        if not secrets.compare_digest(client["lease_token"], token):
            raise PermissionError("client lease token differs")
        return client

    def renew(self, client_id: str, token: str) -> dict[str, Any]:
        if self.closing:
            raise RuntimeError("project Gateway is draining")
        client = self._require(client_id, token)
        client["last_seen_ns"] = time.time_ns()
        if client["expires"] is not None:
            client["expires"] = self.clock() + LEASE_SECONDS
        return {"client_id": client_id, "lease_token": token,
                "lease_seconds": LEASE_SECONDS, "renew_seconds": RENEW_SECONDS}

    def release(self, client_id: str, token: str) -> None:
        self.expire()
        if client_id in self.clients:
            self._require(client_id, token)
            del self.clients[client_id]
        if not self.clients and self.idle_since is None:
            self.idle_since = self.clock()

    def expire(self) -> None:
        now = self.clock()
        for key, client in list(self.clients.items()):
            if client["expires"] is not None and client["expires"] <= now:
                del self.clients[key]
        if not self.clients and self.idle_since is None:
            self.idle_since = now

    def snapshot(self, reasons: dict[str, int]) -> dict[str, Any]:
        self.expire()
        now = self.clock()
        if self.clients or any(reasons.values()):
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        remaining = None if self.idle_since is None else max(0.0, self.idle_seconds - (now - self.idle_since))
        clients = [{key: value for key, value in client.items() if key not in {"expires", "lease_token"}}
                   for client in self.clients.values()]
        return {"capability": CAPABILITY,
                "state": "draining" if self.closing else "running" if remaining is None else "idle",
                "idle_timeout_seconds": self.idle_seconds, "idle_remaining_seconds": remaining,
                "clients": clients, "keepalive": {"clients": len(clients), **reasons}}

    def should_stop(self, reasons: dict[str, int]) -> bool:
        state = self.snapshot(reasons)
        return state["idle_remaining_seconds"] == 0 and not self.closing
