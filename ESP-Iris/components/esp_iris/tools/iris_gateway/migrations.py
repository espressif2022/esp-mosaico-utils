"""Versioned, transactional SQLite schema migrations for Gateway state."""

from __future__ import annotations

import sqlite3
from typing import Callable

Migration = Callable[[sqlite3.Connection], None]


def _migration_1(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
            device_id TEXT PRIMARY KEY, alias TEXT UNIQUE,
            first_seen_ns INTEGER NOT NULL, last_seen_ns INTEGER NOT NULL,
            cached_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
            boot_id TEXT, session_id TEXT, endpoint TEXT,
            started_ns INTEGER NOT NULL, ended_ns INTEGER
        );
        CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
            actor_type TEXT NOT NULL, actor_name TEXT NOT NULL,
            action TEXT NOT NULL, params_json TEXT NOT NULL,
            status TEXT NOT NULL, result_json TEXT, error TEXT,
            created_ns INTEGER NOT NULL, started_ns INTEGER,
            finished_ns INTEGER, queue_position INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS operations_device_created
            ON operations(device_id, created_ns DESC);
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT,
            category TEXT NOT NULL, host_receive_ns INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS events_device_cursor
            ON events(device_id, event_id);
        CREATE TABLE IF NOT EXISTS system_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_type TEXT NOT NULL, actor_name TEXT NOT NULL,
            action TEXT NOT NULL, details_json TEXT NOT NULL,
            created_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_tokens (
            token_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL, created_ns INTEGER NOT NULL,
            last_used_ns INTEGER, revoked_ns INTEGER
        );
        CREATE TABLE IF NOT EXISTS log_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
            path TEXT NOT NULL, first_event_id INTEGER NOT NULL,
            last_event_id INTEGER NOT NULL, first_ns INTEGER NOT NULL,
            last_ns INTEGER NOT NULL, compressed_bytes INTEGER NOT NULL DEFAULT 0,
            UNIQUE(device_id, path)
        );
        CREATE TABLE IF NOT EXISTS firmware_artifacts (
            artifact_id TEXT PRIMARY KEY, binary_sha256 TEXT NOT NULL UNIQUE,
            elf_sha256 TEXT NOT NULL, project_name TEXT NOT NULL,
            version TEXT NOT NULL, manifest_json TEXT NOT NULL,
            created_ns INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS firmware_artifacts_elf_sha
            ON firmware_artifacts(elf_sha256, created_ns DESC);
        """
    )


def _migration_2(db: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(operations)").fetchall()
    }
    if "progress_json" not in columns:
        db.execute("ALTER TABLE operations ADD COLUMN progress_json TEXT")


def _migration_3(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS agent_tokens ("
        "token_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
        "token_hash TEXT NOT NULL, created_ns INTEGER NOT NULL, "
        "last_used_ns INTEGER, revoked_ns INTEGER, "
        "scopes_json TEXT NOT NULL DEFAULT '[\"files.read\"]')"
    )
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(agent_tokens)").fetchall()
    }
    if "scopes_json" not in columns:
        db.execute(
            "ALTER TABLE agent_tokens ADD COLUMN scopes_json TEXT NOT NULL "
            "DEFAULT '[\"files.read\"]'"
        )


def _migration_4(db: sqlite3.Connection) -> None:
    # Reserved schema version. Host operations use the operations table.
    pass


def _migration_5(db: sqlite3.Connection) -> None:
    # Deliberately do not fabricate identities for historical requests.
    db.execute("ALTER TABLE operations ADD COLUMN request_fingerprint TEXT")


def _migration_6(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE operation_reconciliations ("
               "reconciliation_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL "
               "REFERENCES operations(operation_id), record_json TEXT NOT NULL)")
    db.execute("CREATE INDEX reconciliation_operation ON operation_reconciliations(operation_id)")


def _migration_7(db: sqlite3.Connection) -> None:
    db.execute("DROP TABLE IF EXISTS maintenance_leases")


MIGRATIONS: tuple[Migration, ...] = (
    _migration_1,
    _migration_2,
    _migration_3,
    _migration_4,
    _migration_5,
    _migration_6,
    _migration_7,
)
LATEST_SCHEMA_VERSION = len(MIGRATIONS)


def apply_migrations(db: sqlite3.Connection) -> int:
    current = int(db.execute("PRAGMA user_version").fetchone()[0])
    if current > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"gateway database schema {current} is newer than supported "
            f"version {LATEST_SCHEMA_VERSION}"
        )
    for version, migration in enumerate(MIGRATIONS, start=1):
        if version <= current:
            continue
        with db:
            migration(db)
            db.execute(f"PRAGMA user_version={version}")
    return LATEST_SCHEMA_VERSION


__all__ = ["LATEST_SCHEMA_VERSION", "MIGRATIONS", "apply_migrations"]
