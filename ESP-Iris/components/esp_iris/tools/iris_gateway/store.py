from __future__ import annotations

import gzip
import hashlib
import json
import pathlib
import shutil
import sqlite3
import tempfile
import time
import zipfile
from collections.abc import Iterable
from typing import Any

from .boot_identity import boot_id_text
from .migrations import apply_migrations
from .operation_identity import request_fingerprint, require_same_request


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


class GatewayStore:
    """Durable gateway state and append-only evidence indexes.

    SQLite stores structured state. Raw logs and saved artifacts stay as files so
    large sessions do not turn the database into a blob store.
    """

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs_dir = root / "logs"
        self.artifacts_dir = root / "artifacts"
        self.logs_dir.mkdir(exist_ok=True)
        self.artifacts_dir.mkdir(exist_ok=True)
        self.db = sqlite3.connect(root / "gateway.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._last_log_cleanup_ns = 0
        self.schema_version = apply_migrations(self.db)

    def close(self) -> None:
        self.db.close()

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return _loads(row["value"], default) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.db.execute(
            """INSERT INTO settings(key, value, updated_ns) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                   updated_ns=excluded.updated_ns""",
            (key, _json(value), time.time_ns()),
        )
        self.db.commit()

    def remember_device(self, info: dict[str, Any]) -> None:
        device_id = str(info["device_id"])
        now = time.time_ns()
        existing = self.db.execute(
            "SELECT cached_json FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        cached = _loads(existing["cached_json"], {}) if existing else {}
        cached.update(info)
        self.db.execute(
            """INSERT INTO devices(device_id, first_seen_ns, last_seen_ns, cached_json)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(device_id) DO UPDATE SET
                   last_seen_ns=excluded.last_seen_ns,
                   cached_json=excluded.cached_json""",
            (device_id, now, now, _json(cached)),
        )
        boot_id = info.get("boot_id")
        session_id = info.get("session_id")
        endpoint = info.get("endpoint")
        if session_id is not None:
            active = self.db.execute(
                "SELECT id, boot_id, session_id FROM sessions "
                "WHERE device_id=? AND ended_ns IS NULL ORDER BY id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            if active is None or str(active["session_id"]) != str(session_id):
                if active is not None:
                    self.db.execute(
                        "UPDATE sessions SET ended_ns=? WHERE id=?", (now, active["id"])
                    )
                self.db.execute(
                    "INSERT INTO sessions(device_id, boot_id, session_id, endpoint, started_ns) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (device_id, str(boot_id), str(session_id), endpoint, now),
                )
        self.db.commit()

    def set_alias(self, device_id: str, alias: str | None) -> None:
        try:
            self.db.execute(
                "UPDATE devices SET alias=? WHERE device_id=?",
                (alias or None, device_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"device alias is already in use: {alias}") from exc
        self.db.commit()

    def remove_device(self, device_id: str) -> bool:
        """Remove a device from inventory without deleting its evidence history."""

        with self.db:
            cursor = self.db.execute(
                "DELETE FROM devices WHERE device_id=?", (device_id,)
            )
            self.db.execute(
                "DELETE FROM settings WHERE key=?", (f"status.{device_id}",)
            )
        return cursor.rowcount > 0

    def cached_devices(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT device_id, alias, last_seen_ns, cached_json FROM devices "
            "ORDER BY COALESCE(alias, device_id)"
        ).fetchall()
        result = []
        for row in rows:
            item = _loads(row["cached_json"], {})
            item.update(
                alias=row["alias"],
                last_seen_ns=row["last_seen_ns"],
                cached=True,
            )
            result.append(item)
        return result

    def resolve_device(self, value: str) -> str:
        row = self.db.execute(
            "SELECT device_id FROM devices WHERE device_id=? OR alias=?", (value, value)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown device or alias: {value}")
        return str(row["device_id"])

    def append_event(
        self, category: str, payload: dict[str, Any], device_id: str | None = None
    ) -> dict[str, Any]:
        host_ns = int(payload.get("host_receive_wall_ns") or time.time_ns())
        cursor = self.db.execute(
            "INSERT INTO events(device_id, category, host_receive_ns, payload_json) "
            "VALUES(?, ?, ?, ?)",
            (device_id, category, host_ns, _json(payload)),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an event ID")
        event_id = int(cursor.lastrowid)
        self.db.commit()
        item = dict(payload)
        item.update(event_id=event_id, category=category, host_receive_ns=host_ns)
        if category == "log" and device_id:
            self._append_raw_log(device_id, event_id, host_ns, item)
        return item

    def events_after(
        self,
        cursor: int,
        *,
        device_id: str | None = None,
        categories: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], bool]:
        oldest_row = self.db.execute("SELECT MIN(event_id) AS value FROM events").fetchone()
        oldest = int(oldest_row["value"] or 0)
        history_gap = bool(cursor and oldest and cursor < oldest - 1)
        clauses = ["event_id > ?"]
        values: list[Any] = [cursor]
        if device_id:
            clauses.append("device_id=?")
            values.append(device_id)
        category_values = list(categories or ())
        if category_values:
            clauses.append(
                f"category IN ({','.join('?' * len(category_values))})"
            )
            values.extend(category_values)
        values.append(max(1, min(limit, 5000)))
        rows = self.db.execute(
            "SELECT * FROM events WHERE " + " AND ".join(clauses)
            + " ORDER BY event_id LIMIT ?",
            values,
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = _loads(row["payload_json"], {})
            item.update(
                event_id=row["event_id"],
                category=row["category"],
                host_receive_ns=row["host_receive_ns"],
            )
            items.append(item)
        return items, history_gap

    def latest_events(
        self,
        *,
        device_id: str | None = None,
        categories: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if device_id:
            clauses.append("device_id=?")
            values.append(device_id)
        category_values = list(categories or ())
        if category_values:
            clauses.append(
                f"category IN ({','.join('?' * len(category_values))})"
            )
            values.extend(category_values)
        values.append(max(1, min(limit, 5000)))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.execute(
            "SELECT * FROM events" + where + " ORDER BY event_id DESC LIMIT ?",
            values,
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = _loads(row["payload_json"], {})
            item.update(
                event_id=row["event_id"],
                category=row["category"],
                host_receive_ns=row["host_receive_ns"],
            )
            items.append(item)
        return items

    def create_operation(self, operation: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        fingerprint = operation.get("request_fingerprint") or request_fingerprint(operation)
        cursor = self.db.execute(
            """INSERT INTO operations(
                   operation_id, device_id, actor_type, actor_name, action,
                   params_json, status, created_ns, queue_position, request_fingerprint)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(operation_id) DO NOTHING""",
            (
                operation["operation_id"],
                operation["device_id"],
                operation["actor_type"],
                operation["actor_name"],
                operation["action"],
                _json(operation.get("params", {})),
                operation["status"],
                operation["created_ns"],
                operation.get("queue_position", 0),
                fingerprint,
            ),
        )
        self.db.commit()
        stored = self.operation(str(operation["operation_id"]))
        assert stored is not None
        require_same_request(stored, fingerprint)
        return stored, cursor.rowcount == 1

    def update_operation(self, operation_id: str, **changes: Any) -> dict[str, Any]:
        columns = {
            "status",
            "result_json",
            "error",
            "started_ns",
            "finished_ns",
            "queue_position",
            "progress_json",
        }
        updates = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in columns:
                continue
            updates.append(f"{key}=?")
            values.append(
                _json(value) if key in {"result_json", "progress_json"} else value
            )
        if updates:
            values.append(operation_id)
            self.db.execute(
                f"UPDATE operations SET {', '.join(updates)} WHERE operation_id=?",
                values,
            )
            self.db.commit()
        item = self.operation(operation_id)
        if item is None:
            raise KeyError(operation_id)
        return item

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return self._operation_row(row) if row else None

    def operations(
        self, device_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if device_id:
            rows = self.db.execute(
                "SELECT * FROM operations WHERE device_id=? "
                "ORDER BY created_ns DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM operations ORDER BY created_ns DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._operation_row(row) for row in rows]

    @staticmethod
    def _operation_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["params"] = _loads(item.pop("params_json"), {})
        item["result"] = _loads(item.pop("result_json"), None)
        item["progress"] = _loads(item.pop("progress_json", None), None)
        display = boot_id_text(item)
        display["params"] = item["params"]
        return display

    def save_firmware_artifact(
        self,
        *,
        binary: bytes,
        elf: bytes,
        map_data: bytes,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one complete, content-addressed firmware evidence bundle."""

        binary_sha = hashlib.sha256(binary).hexdigest()
        elf_sha = hashlib.sha256(elf).hexdigest()
        map_sha = hashlib.sha256(map_data).hexdigest()
        artifact_id = elf_sha
        existing = self.firmware_artifact(artifact_id)
        if existing is not None:
            if existing["binary_sha256"] != binary_sha:
                raise ValueError("ELF SHA already exists with a different firmware binary")
            if existing.get("map_sha256") != map_sha:
                raise ValueError("ELF SHA already exists with a different linker map")
            return existing

        folder = self.artifacts_dir / "firmware" / artifact_id
        folder.mkdir(parents=True, exist_ok=True)
        files = {
            "firmware.bin": binary,
            "firmware.elf": elf,
            "firmware.map": map_data,
        }
        for name, data in files.items():
            target = folder / name
            try:
                with target.open("xb") as handle:
                    handle.write(data)
            except FileExistsError:
                if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(data).digest():
                    raise ValueError(f"artifact file collision: {target}")
        manifest = {
            "schema": "esp-iris-firmware-artifact/v1",
            "artifact_id": artifact_id,
            "binary_sha256": binary_sha,
            "elf_sha256": elf_sha,
            "map_sha256": map_sha,
            "project_name": str(metadata["project_name"]),
            "version": str(metadata["version"]),
            "chip_id": int(metadata["chip_id"]),
            "sizes": {name: len(data) for name, data in files.items()},
            "sha256": {
                name: hashlib.sha256(data).hexdigest()
                for name, data in files.items()
            },
            "files": {name: name for name in files},
            "created_ns": time.time_ns(),
        }
        (folder / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.db.execute(
            """INSERT INTO firmware_artifacts(
                   artifact_id, binary_sha256, elf_sha256, project_name,
                   version, manifest_json, created_ns)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact_id,
                binary_sha,
                elf_sha,
                manifest["project_name"],
                manifest["version"],
                _json(manifest),
                manifest["created_ns"],
            ),
        )
        self.db.commit()
        return {**manifest, "path": str(folder)}

    def firmware_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM firmware_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if row is None:
            return None
        manifest = _loads(row["manifest_json"], {})
        folder = self.artifacts_dir / "firmware" / str(row["artifact_id"])
        return {**manifest, "path": str(folder)}

    def firmware_artifacts(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT artifact_id FROM firmware_artifacts ORDER BY created_ns DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [
            artifact
            for row in rows
            if (artifact := self.firmware_artifact(str(row["artifact_id"]))) is not None
        ]

    def add_audit(
        self,
        actor_type: str,
        actor_name: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time_ns()
        cursor = self.db.execute(
            "INSERT INTO system_audit(actor_type, actor_name, action, details_json, created_ns) "
            "VALUES(?, ?, ?, ?, ?)",
            (actor_type, actor_name, action, _json(details or {}), now),
        )
        self.db.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an audit ID")
        return {
            "audit_id": int(cursor.lastrowid),
            "actor_type": actor_type,
            "actor_name": actor_name,
            "action": action,
            "details": details or {},
            "created_ns": now,
        }

    def audits(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM system_audit ORDER BY audit_id DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = _loads(item.pop("details_json"), {})
            result.append(boot_id_text(item))
        return result

    def save_artifact(
        self, device_id: str, kind: str, data: bytes, suffix: str
    ) -> pathlib.Path:
        safe_device = "".join(c for c in device_id if c.isalnum() or c in "-_")
        folder = self.artifacts_dir / safe_device
        folder.mkdir(exist_ok=True)
        created_ns = time.time_ns()
        stamp = time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(created_ns // 1_000_000_000)
        )
        digest = hashlib.sha256(data).hexdigest()[:12]
        nanoseconds = created_ns % 1_000_000_000
        stem = f"{stamp}-{nanoseconds:09d}-{kind}-{digest}"
        extension = suffix.lstrip(".")
        for collision in range(1_000):
            discriminator = f"-{collision}" if collision else ""
            target = folder / f"{stem}{discriminator}.{extension}"
            try:
                with target.open("xb") as handle:
                    handle.write(data)
                return target
            except FileExistsError:
                continue
        raise FileExistsError("could not allocate a unique artifact filename")

    def export_zip(self, target: pathlib.Path | None = None) -> pathlib.Path:
        if target is None:
            target = self.root / f"esp-iris-export-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        manifest = {
            "schema": "esp-iris-export/v1",
            "created_ns": time.time_ns(),
            "mode": self.get_setting("mode", "develop"),
        }
        with tempfile.TemporaryDirectory(prefix="esp-iris-export-") as temp_name:
            temp = pathlib.Path(temp_name)
            (temp / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
            with (temp / "operations.jsonl").open("w", encoding="utf-8") as handle:
                for item in reversed(self.operations(limit=1_000_000)):
                    handle.write(_json(item) + "\n")
            with (temp / "reconciliations.jsonl").open("w", encoding="utf-8") as handle:
                for row in self.db.execute("SELECT record_json FROM operation_reconciliations ORDER BY rowid"):
                    handle.write(row[0] + "\n")
            with (temp / "system-audit.jsonl").open("w", encoding="utf-8") as handle:
                for item in reversed(self.audits(limit=1_000_000)):
                    handle.write(_json(item) + "\n")
            if self.logs_dir.exists():
                shutil.copytree(self.logs_dir, temp / "logs", dirs_exist_ok=True)
            if self.artifacts_dir.exists():
                shutil.copytree(self.artifacts_dir, temp / "artifacts", dirs_exist_ok=True)
            files = sorted(path for path in temp.rglob("*") if path.is_file())
            with (temp / "checksums.sha256").open("w", encoding="utf-8") as handle:
                for path in files:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    handle.write(f"{digest}  {path.relative_to(temp).as_posix()}\n")
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(p for p in temp.rglob("*") if p.is_file()):
                    archive.write(path, path.relative_to(temp).as_posix())
        return target

    def cleanup_raw_logs(
        self, max_age_days: int = 7, max_bytes: int = 1024 * 1024 * 1024
    ) -> None:
        files = sorted(
            (path for path in self.logs_dir.rglob("*.jsonl.gz") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
        cutoff = time.time() - max_age_days * 86400
        for path in list(files):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                files.remove(path)
        total = sum(path.stat().st_size for path in files)
        while files and total > max_bytes:
            path = files.pop(0)
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
        for row in self.db.execute("SELECT id, path FROM log_index").fetchall():
            if not (self.root / row["path"]).is_file():
                self.db.execute("DELETE FROM log_index WHERE id=?", (row["id"],))
        self.db.commit()

    def _append_raw_log(
        self, device_id: str, event_id: int, host_ns: int, payload: dict[str, Any]
    ) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(host_ns / 1_000_000_000))
        folder = self.logs_dir / device_id
        folder.mkdir(exist_ok=True)
        path = folder / f"{day}.jsonl.gz"
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write(_json(payload) + "\n")
        relative = path.relative_to(self.root).as_posix()
        size = path.stat().st_size
        self.db.execute(
            """INSERT INTO log_index(
                   device_id, path, first_event_id, last_event_id,
                   first_ns, last_ns, compressed_bytes)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(device_id, path) DO UPDATE SET
                   last_event_id=excluded.last_event_id,
                   last_ns=excluded.last_ns,
                   compressed_bytes=excluded.compressed_bytes""",
            (device_id, relative, event_id, event_id, host_ns, host_ns, size),
        )
        self.db.commit()
        if host_ns - self._last_log_cleanup_ns >= 60_000_000_000:
            self._last_log_cleanup_ns = host_ns
            self.cleanup_raw_logs()


__all__ = ["GatewayStore"]
