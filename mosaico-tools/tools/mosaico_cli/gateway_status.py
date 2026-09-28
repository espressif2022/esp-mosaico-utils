"""Passive project and same-user Gateway inventory; never starts a service."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import session_runtime
from .errors import DeviceError, EnvironmentError
from .iris import host_api
from .project import resolve_project


def status(workspace: Any, project: str | None, *, all_projects: bool = False) -> dict[str, Any]:
    root = session_runtime.state_root("esp-mosaico")
    api = host_api(workspace)
    try:
        snapshot = api.registry_snapshot(root)
    except api.LocalStateError as error:
        raise EnvironmentError(str(error)) from error
    sessions, claims = snapshot["sessions"], snapshot["claims"]
    selected_key = None
    if not all_projects:
        selected = resolve_project(workspace, project, Path.cwd())
        selected_key = api.LocalProject(root, workspace.root, selected).project_id
    if not sessions:
        return {"gateways": []} if all_projects else {"running": False, "session": None}
    candidates = [item for item in sessions if (selected_key is None or item["project_id"] == selected_key)
                  and (item["alive"] or any(claim["owner"] == item["session_id"] for claim in claims))]

    def inspect(session: dict[str, Any]) -> dict[str, Any]:
        owned = [claim for claim in claims if claim["owner"] == session["session_id"]]
        result = {"running": session["alive"], "reachable": False, "session": session,
                  "state": "unreachable" if session["alive"] else "orphaned", "claims": owned,
                  "device_ids": sorted({claim["device_id"] for claim in owned if claim["device_id"]}),
                  "unidentified_endpoints": [claim["resource"] for claim in owned if not claim["device_id"]],
                  "lifecycle": None}
        if not session["alive"]:
            return result
        try:
            address = urlsplit(session["url"])
            if address.scheme != "http" or address.hostname != "127.0.0.1":
                raise DeviceError("Registry URL is not a local project Gateway")
            value = session_runtime.request(session["url"], "/v1/project", timeout=2)
            if not isinstance(value, dict) or not isinstance(value.get("session"), dict):
                raise DeviceError("Invalid project status response")
            live = value.get("session") or {}
            if any(live.get(key) != session[key] for key in ("session_id", "project_id", "instance_id")):
                raise DeviceError("Gateway identity differs from the shared registry")
            lifecycle = value.get("lifecycle")
            if lifecycle is not None and not isinstance(lifecycle, dict):
                raise DeviceError("Invalid project lifecycle response")
            result.update(reachable=True, lifecycle=lifecycle, closing=value.get("closing", False),
                          state="draining" if value.get("closing") else (lifecycle or {}).get("state", "legacy"))
            if not all_projects:
                # Retain the existing single-project JSON fields.
                result.update({key: value[key] for key in ("sessions", "endpoints", "takeovers", "capability", "busy", "pairing_configured") if key in value})
        except DeviceError as error:
            result["error"] = str(error)
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(inspect, candidates))
    if all_projects:
        return {"gateways": sorted(results, key=lambda item: (item["session"]["project_path"], item["session"]["created_ns"]))}
    live = next((item for item in results if item["running"]), None)
    if live is not None:
        live["claims"] = claims  # Existing status includes the shared ownership snapshot.
        return live
    result = {"running": False, "session": None}
    if results:
        result["orphaned_sessions"] = results
    return result


def print_status(result: dict[str, Any]) -> None:
    gateways = result.get("gateways", [result])
    if not gateways:
        print("No live project Gateways or orphaned ownership records.")
    for gateway in gateways:
        session = gateway.get("session")
        if session is None:
            print("Project Gateway: not running.")
            for orphan in gateway.get("orphaned_sessions", []):
                print(f"Orphaned session: {orphan['session']['session_id']} devices={','.join(orphan['device_ids']) or '-'}")
            continue
        print(f"Project: {session['project_path']}")
        print(f"Workspace: {session.get('workspace_path') or '(legacy: not recorded)'}")
        print(f"Gateway: {gateway['state']}  {session['url']}  session={session['session_id']}")
        print(f"Revision: {session.get('source_revision', 'unknown')}")
        print(f"Devices: {', '.join(gateway['device_ids']) or '-'}")
        for claim in gateway["claims"]:
            if claim["owner"] == session["session_id"]:
                print(f"  {claim['resource']}  state={claim['state']}")
        lifecycle = gateway.get("lifecycle")
        if lifecycle is None:
            print("Clients: unavailable (legacy or unreachable Gateway)")
        else:
            for client in lifecycle["clients"]:
                print(f"  Client: {client['client_id']}  {client['kind']}  {client['command']}"
                      f"  pid={client.get('pid') or '-'} connected_ns={client['connected_ns']} last_seen_ns={client['last_seen_ns']}")
            reasons = ", ".join(f"{key}={value}" for key, value in lifecycle["keepalive"].items() if value)
            print(f"Keepalive: {reasons or 'none'}")
            remaining = lifecycle["idle_remaining_seconds"]
            if remaining is not None:
                print(f"Idle shutdown in: {remaining:.1f}s")
        if gateway.get("error"):
            print(f"Probe: {gateway['error']}")
