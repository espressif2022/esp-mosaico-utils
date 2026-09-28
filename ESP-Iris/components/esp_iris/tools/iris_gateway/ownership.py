"""Same-user device ownership, independent of physical connections.

SQLite transactions are short and never span an await or device I/O. Session
liveness comes from OS locks, not PIDs or HTTP timeouts. A crashed owner's
records remain orphaned until explicitly reconciled. Transfer reservations
survive both participants, so releasing a USB lock never opens a race to C.
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import sqlite3
import time
import uuid
from collections.abc import Iterator
from typing import Any

from .link import EndpointLock

CAPABILITY = "project-device-ownership/v1"


class OwnershipConflict(RuntimeError):
    pass


class OwnershipRegistry:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "ownership.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise RuntimeError("unsupported ownership database version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                project_path TEXT NOT NULL, instance_id TEXT NOT NULL,
                url TEXT NOT NULL, created_ns INTEGER NOT NULL,
                persistent INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS claims (
                resource TEXT PRIMARY KEY, owner TEXT NOT NULL,
                generation INTEGER NOT NULL, state TEXT NOT NULL,
                device_id TEXT, metadata TEXT NOT NULL,
                transfer_id TEXT);
            CREATE TABLE IF NOT EXISTS transfers (
                transfer_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                source TEXT NOT NULL, target TEXT NOT NULL,
                state TEXT NOT NULL, metadata TEXT NOT NULL,
                created_ns INTEGER NOT NULL, updated_ns INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS identities (
                endpoint TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS generations (
                resource TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS session_metadata (
                session_id TEXT PRIMARY KEY, workspace_path TEXT NOT NULL,
                lifecycle_capability TEXT NOT NULL, source_revision TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        self.session_id = ""
        self._live_lock: EndpointLock | None = None
        self._project_lock: EndpointLock | None = None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _lock(self, session_id: str) -> EndpointLock:
        return EndpointLock("session:" + session_id, root=self.root / "locks")

    def alive(self, session_id: str) -> bool:
        if session_id == self.session_id and self._live_lock is not None:
            return True
        return EndpointLock.held("session:" + session_id, self.root / "locks")

    def register(self, session_id: str, project_id: str, project_path: str,
                 instance_id: str, url: str = "", *, persistent: bool = True) -> None:
        if self._live_lock is not None:
            raise RuntimeError("session is already registered")
        lock = self._lock(session_id)
        project_lock = EndpointLock("project:" + project_id, root=self.root / "locks")
        try:
            project_lock.acquire()
            lock.acquire()
            with self.transaction():
                self.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", (
                    session_id, project_id, project_path, instance_id, url, time.time_ns(), int(persistent),
                ))
        except BaseException:
            lock.close()
            project_lock.close()
            raise
        self.session_id, self._live_lock = session_id, lock
        self._project_lock = project_lock

    def set_url(self, url: str) -> None:
        with self.transaction():
            self.db.execute("UPDATE sessions SET url=? WHERE session_id=?", (url, self.session_id))

    def set_metadata(self, workspace_path: str, lifecycle_capability: str, source_revision: str) -> None:
        # Keep sessions' seven-column layout readable/writable by older workspaces.
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO session_metadata VALUES(?,?,?,?)",
                            (self.session_id, workspace_path, lifecycle_capability, source_revision))

    def session(self, session_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        metadata = self.db.execute("SELECT * FROM session_metadata WHERE session_id=?", (session_id,)).fetchone()
        return {**dict(row), **(dict(metadata) if metadata else {}),
                "alive": self.alive(session_id), "capability": CAPABILITY}

    def sessions(self) -> list[dict[str, Any]]:
        return [self.session(str(row[0])) for row in self.db.execute("SELECT session_id FROM sessions")]

    def claim(self, resource: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM claims WHERE resource=?", (resource,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["owner_alive"] = self.alive(result["owner"])
        return result

    def claims(self) -> list[dict[str, Any]]:
        return [item for row in self.db.execute("SELECT resource FROM claims")
                if (item := self.claim(str(row[0]))) is not None]

    def _require(self, resource: str, states: tuple[str, ...] = ("owned", "accepting")) -> dict[str, Any]:
        claim = self.claim(resource)
        if claim is None or claim["owner"] != self.session_id or claim["state"] not in states:
            owner = claim["owner"] if claim else "none"
            raise OwnershipConflict(f"{resource} is not owned by this session (owner={owner})")
        return claim

    def allowed(self, resource: str) -> bool:
        item = self.claim(resource)
        return bool(item and item["owner"] == self.session_id and item["state"] in ("owned", "accepting"))

    def acquire(self, endpoint: str, metadata: dict[str, Any]) -> dict[str, Any]:
        with self.transaction():
            current = self.claim(endpoint)
            if current is not None:
                return self._require(endpoint)
            previous = self.db.execute("SELECT value FROM generations WHERE resource=?", (endpoint,)).fetchone()
            generation = int(previous[0]) + 1 if previous else 1
            self.db.execute("INSERT INTO claims VALUES(?,?,?,'owned',NULL,?,NULL)",
                            (endpoint, self.session_id, generation, json.dumps(metadata)))
            self._remember_generations()
        return self._require(endpoint)

    def bind(self, endpoint: str, device_id: str, *, verified: bool = True) -> None:
        with self.transaction():
            endpoint_claim = self._require(endpoint)
            expected = endpoint_claim.get("device_id")
            if expected and expected != device_id:
                raise OwnershipConflict("HELLO identity differs from the owned device")
            resource = "device:" + device_id
            existing = self.claim(resource)
            if existing:
                self._require(resource)
            else:
                previous = self.db.execute("SELECT value FROM generations WHERE resource=?", (resource,)).fetchone()
                generation = max(endpoint_claim["generation"], int(previous[0]) + 1 if previous else 1)
                self.db.execute("INSERT INTO claims VALUES(?,?,?,?,?,?,?)", (
                    resource, self.session_id, generation,
                    endpoint_claim["state"], device_id,
                    json.dumps(endpoint_claim["metadata"]), endpoint_claim["transfer_id"],
                ))
            self.db.execute("UPDATE claims SET device_id=? WHERE resource=?", (device_id, endpoint))
            if verified:
                metadata = dict(endpoint_claim["metadata"])
                metadata.pop("identity_probe", None)
                self.db.execute("UPDATE claims SET metadata=? WHERE resource IN (?,?) AND owner=?",
                                (json.dumps(metadata), endpoint, resource, self.session_id))
                self.db.execute("INSERT OR REPLACE INTO identities VALUES(?,?,?)", (
                    endpoint, device_id, json.dumps(metadata),
                ))
            self._remember_generations()

    def release_attempt(self, endpoint: str, generation: int, *, remove_device: bool) -> None:
        """Release only this failed attempt, after its physical session is closed."""
        with self.transaction():
            item = self.claim(endpoint)
            if not item or (item["owner"], item["generation"], item["state"]) != (self.session_id, generation, "owned"):
                return
            self.db.execute("DELETE FROM claims WHERE resource=?", (endpoint,))
            if remove_device and item["device_id"]:
                remaining = self.db.execute(
                    "SELECT 1 FROM claims WHERE owner=? AND device_id=? AND resource NOT LIKE 'device:%'",
                    (self.session_id, item["device_id"])).fetchone()
                if remaining is None:
                    self.db.execute("DELETE FROM claims WHERE resource=? AND owner=? AND state='owned'",
                                    ("device:" + item["device_id"], self.session_id))

    def _remember_generations(self) -> None:
        self.db.execute("""INSERT INTO generations SELECT resource, generation FROM claims WHERE 1
            ON CONFLICT(resource) DO UPDATE SET value=MAX(value, excluded.value)""")

    def known_endpoints(self, device_id: str) -> list[dict[str, Any]]:
        return [{"endpoint": row[0], **json.loads(row[1])} for row in self.db.execute(
            "SELECT endpoint, metadata FROM identities WHERE device_id=?", (device_id,))]

    def reconcile_orphan(self, resource: str) -> None:
        """Explicitly release a dead owner's ordinary claim, never a transfer
        reservation. Physical locks must also be available.
        """
        with contextlib.ExitStack() as locks, self.transaction():
            item = self.claim(resource)
            if item is None:
                return
            if self.alive(item["owner"]):
                raise OwnershipConflict("owner is still alive; request a transfer instead")
            claims = [entry for entry in self.claims() if entry["owner"] == item["owner"]
                      and (entry["device_id"] == item["device_id"] if item["device_id"] else entry["resource"] == resource)]
            if any(entry["state"] != "owned" for entry in claims):
                raise OwnershipConflict("transfer reservation requires its own recovery procedure")
            for entry in claims:
                if not entry["resource"].startswith("device:"):
                    lock = EndpointLock(entry["resource"])
                    locks.callback(lock.close)
                    lock.acquire()
            for entry in claims:
                self.db.execute("DELETE FROM claims WHERE resource=?", (entry["resource"],))

    def release(self, resource: str) -> None:
        """Caller must close physical sessions before releasing ownership."""
        with self.transaction():
            item = self._require(resource, ("owned",))
            if item["device_id"]:
                self.db.execute("DELETE FROM claims WHERE owner=? AND device_id=? AND state='owned'",
                                (self.session_id, item["device_id"]))
            else:
                self.db.execute("DELETE FROM claims WHERE resource=?", (resource,))

    def transfer(self, transfer_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise KeyError(transfer_id)
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def prepare(self, device_id: str, target: str, transfer_id: str) -> dict[str, Any]:
        uuid.UUID(transfer_id)
        with self.transaction():
            try:
                previous = self.transfer(transfer_id)
            except KeyError:
                previous = None
            if previous:
                if (previous["device_id"], previous["source"], previous["target"]) != (
                    device_id, self.session_id, target,
                ):
                    raise OwnershipConflict("transfer ID already describes a different request")
                return previous
            target_session = self.session(target)
            if target == self.session_id or not target_session["alive"]:
                raise OwnershipConflict("target must be a different live project session")
            if not target_session.get("lifecycle_capability") and not target_session["persistent"]:
                raise OwnershipConflict("legacy temporary sessions cannot receive transfers")
            self._require("device:" + device_id, ("owned",))
            endpoints = [item for item in self.claims()
                         if item["device_id"] == device_id and not item["resource"].startswith("device:")]
            if not endpoints:
                raise OwnershipConflict("device has no known endpoint")
            now = time.time_ns()
            self.db.execute("INSERT INTO transfers VALUES(?,?,?,?,'preparing',?,?,?)", (
                transfer_id, device_id, self.session_id, target,
                json.dumps({"endpoints": endpoints}), now, now,
            ))
            self.db.execute("UPDATE claims SET state='preparing', transfer_id=? WHERE device_id=?",
                            (transfer_id, device_id))
        return self.transfer(transfer_id)

    def offer(self, transfer_id: str) -> dict[str, Any]:
        """Called only after source supervisors and physical locks are closed."""
        with self.transaction():
            item = self.transfer(transfer_id)
            if item["source"] != self.session_id or item["state"] != "preparing":
                raise OwnershipConflict("transfer is not prepared by this source")
            self.db.execute("UPDATE claims SET state='offered' WHERE transfer_id=?", (transfer_id,))
            self.db.execute("UPDATE transfers SET state='offered', updated_ns=? WHERE transfer_id=?",
                            (time.time_ns(), transfer_id))
        return self.transfer(transfer_id)

    def accept(self, transfer_id: str) -> dict[str, Any]:
        with contextlib.ExitStack() as locks, self.transaction():
            item = self.transfer(transfer_id)
            if (item["target"] == self.session_id and item["state"] == "preparing"
                    and not self.alive(item["source"])):
                # Source died before publishing its release. Verify that every
                # physical lock was released before promoting the reservation.
                for endpoint in item["metadata"]["endpoints"]:
                    lock = EndpointLock(endpoint["resource"])
                    locks.callback(lock.close)
                    lock.acquire()
                item["state"] = "offered"
            if item["target"] != self.session_id or item["state"] not in ("offered", "accepting", "completed"):
                raise OwnershipConflict("transfer is not offered to this session")
            if item["state"] != "offered":
                return item
            self.db.execute("UPDATE claims SET owner=?, state='accepting', generation=generation+1 WHERE transfer_id=?",
                            (self.session_id, transfer_id))
            self._remember_generations()
            self.db.execute("UPDATE transfers SET state='accepting', updated_ns=? WHERE transfer_id=?",
                            (time.time_ns(), transfer_id))
        return self.transfer(transfer_id)

    def complete(self, transfer_id: str, device_id: str) -> dict[str, Any]:
        with self.transaction():
            item = self.transfer(transfer_id)
            if item["target"] != self.session_id or item["device_id"] != device_id:
                raise OwnershipConflict("transfer identity mismatch")
            if item["state"] == "completed":
                return item
            if item["state"] != "accepting":
                raise OwnershipConflict("transfer is not being accepted")
            self._require("device:" + device_id, ("accepting",))
            self.db.execute("UPDATE claims SET state='owned' WHERE transfer_id=?", (transfer_id,))
            self.db.execute("UPDATE transfers SET state='completed', updated_ns=? WHERE transfer_id=?",
                            (time.time_ns(), transfer_id))
        return self.transfer(transfer_id)

    def abort(self, transfer_id: str) -> dict[str, Any]:
        """Only the live source can reclaim, with proof that target is gone.

        A live target must explicitly close its endpoints before rollback; this
        implementation instead requires it to end its session. Timeouts alone
        never authorize rollback.
        """
        with contextlib.ExitStack() as locks, self.transaction():
            item = self.transfer(transfer_id)
            if item["source"] != self.session_id:
                raise OwnershipConflict("only the source can abort a transfer")
            if item["state"] == "aborted":
                return item
            if item["state"] == "completed":
                raise OwnershipConflict("completed transfers cannot be rolled back")
            if item["state"] == "accepting" and self.alive(item["target"]):
                raise OwnershipConflict("target may still be connected; end its session first")
            for endpoint in item["metadata"]["endpoints"]:
                lock = EndpointLock(endpoint["resource"])
                locks.callback(lock.close)
                lock.acquire()
            self.db.execute("UPDATE claims SET owner=?, state='owned', generation=generation+1, transfer_id=NULL WHERE transfer_id=?",
                            (self.session_id, transfer_id))
            self._remember_generations()
            self.db.execute("UPDATE transfers SET state='aborted', updated_ns=? WHERE transfer_id=?",
                            (time.time_ns(), transfer_id))
        return self.transfer(transfer_id)

    def reconcile_transfer(self, transfer_id: str) -> dict[str, Any]:
        """A new session of either participant explicitly resolves a transfer
        after both original sessions died. Never rolls back a completed one.
        """
        with contextlib.ExitStack() as locks, self.transaction():
            item = self.transfer(transfer_id)
            if item["state"] in ("completed", "aborted"):
                return item
            participants = [self.session(item[key]) for key in ("source", "target")]
            if any(part["alive"] for part in participants):
                raise OwnershipConflict("a participant is alive; query, accept or abort the original transfer")
            if self.session(self.session_id)["project_id"] not in {part["project_id"] for part in participants}:
                raise OwnershipConflict("only a participant project can reconcile this transfer")
            for endpoint in item["metadata"]["endpoints"]:
                lock = EndpointLock(endpoint["resource"])
                locks.callback(lock.close)
                lock.acquire()
            self.db.execute("UPDATE claims SET owner=?, state='owned', generation=generation+1, transfer_id=NULL WHERE transfer_id=?",
                            (self.session_id, transfer_id))
            self._remember_generations()
            metadata = {**item["metadata"], "reconciled_by": self.session_id}
            self.db.execute("UPDATE transfers SET state='aborted', metadata=?, updated_ns=? WHERE transfer_id=?",
                            (json.dumps(metadata), time.time_ns(), transfer_id))
        return self.transfer(transfer_id)

    def close(self, *, clean: bool = False) -> None:
        if clean and self.session_id:
            with self.transaction():
                self.db.execute("DELETE FROM claims WHERE owner=? AND state='owned'", (self.session_id,))
        self.db.close()
        if self._live_lock:
            self._live_lock.close()
            self._live_lock = None
        if self._project_lock:
            self._project_lock.close()
            self._project_lock = None
