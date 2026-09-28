"""Explicit create/update upload orchestration, without Gateway or device leases."""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from .bundle_plan import inspect_bundle_plan
from .errors import OperationError, SelectionError
from .gateway import ensure_iris_tools
from .platform_client import (
    PlatformClient,
    PlatformError,
    credential,
    private_root,
    read_state,
    server_url,
    write_state,
)
from .platform_manifest import (
    file_info,
    load_manifest,
    rewritten_readme,
    upload_version,
    validate_bundle,
)
from .project import resolve_project
from .runtime import resolve_idf_path, run_idf_target


def _segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def select_target(
    arguments: Any, client: PlatformClient, version: str
) -> dict[str, Any] | None:
    interactive = sys.stdin.isatty() and not arguments.json
    create, target_id = arguments.create, arguments.update
    if not create and not target_id:
        if not interactive:
            raise SelectionError(
                "Non-interactive and --json uploads require --create or --update APPLICATION_ID."
            )
        choice = (
            input("Create a new application [c] or update an owned application [u]? ")
            .strip()
            .lower()
        )
        if choice not in {"c", "u"}:
            raise SelectionError("Select c or u explicitly.")
        create = choice == "c"
    if create:
        if not arguments.yes and (
            not interactive
            or input("Create a new application at version " + version + "? [y/N] ")
            .strip()
            .lower()
            != "y"
        ):
            raise SelectionError(
                "Creation was not confirmed; use --yes for an unattended upload."
            )
        return None
    targets = client.request("GET", "/firmware-projects/upload-targets")
    if isinstance(targets, dict):
        targets = targets.get("items", [])
    if not target_id:
        if not targets:
            raise SelectionError("You do not own any upload targets.")
        for index, target in enumerate(targets, 1):
            print(
                "{}: {} | {} | {} | {}{}".format(
                    index,
                    target["title"],
                    target["current_version"],
                    target["visibility"],
                    target["id"],
                    " | blocked: " + str(target["blocked_reason"])
                    if not target["can_update"]
                    else "",
                ),
                file=sys.stderr,
            )
        try:
            selected = int(input("Select application number: "))
            if not 1 <= selected <= len(targets):
                raise ValueError()
            target_id = targets[selected - 1]["id"]
        except ValueError as error:
            raise SelectionError("Invalid application selection.") from error
    target = next((item for item in targets if item["id"] == target_id), None)
    if target is None:
        raise SelectionError("The application is not an owned upload target.")
    if not target["can_update"]:
        raise SelectionError(
            "Application cannot be updated: " + str(target.get("blocked_reason"))
        )
    message = "Update {} ({}) from {} to {}".format(
        target["title"], target["id"], target["current_version"], version
    )
    if target.get("draft_revision_id"):
        message += "; this replaces the existing draft"
    if not arguments.json:
        print(message, file=sys.stderr)
    if not arguments.yes and (
        not interactive or input(message + "? [y/N] ").strip().lower() != "y"
    ):
        raise SelectionError(
            "Update was not confirmed; use --yes to confirm replacing the draft."
        )
    return target


def upload_file(
    client: PlatformClient,
    path: Path,
    info: dict[str, Any],
    endpoint: str,
    fallback: str,
    key: str,
    ledger: dict[str, Any],
    ledger_path: Path,
) -> Any:
    completed = ledger.setdefault("assets", {})
    if key in completed:
        return completed[key]
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != info["sha256"]:
        raise SelectionError("A resource changed after validation; run upload again.")
    generations = ledger.setdefault("upload_generations", {})
    for attempt in range(3):
        generation = generations.get(key, 0)
        try:
            grant = client.request(
                "POST", endpoint, info, key=key + ":" + str(generation)
            )
            if grant.get("completed_asset"):
                result = grant["completed_asset"]
            else:
                client.put_object(grant, data)
                result = client.request(
                    "POST", "/asset-uploads/" + _segment(grant["id"]) + "/complete"
                )
        except PlatformError as error:
            if error.code == "DIRECT_UPLOAD_UNAVAILABLE":
                result = client.multipart(
                    fallback,
                    info["filename"],
                    info["media_type"],
                    data,
                    key=key + ":multipart",
                )
            elif error.code == "UPLOAD_EXPIRED" and attempt < 2:
                generations[key] = generation + 1
                write_state(ledger_path, ledger)
                continue
            elif error.code == "NETWORK_ERROR" and attempt < 2:
                continue
            else:
                raise
        completed[key] = result
        write_state(ledger_path, ledger)
        return result
    raise PlatformError("UPLOAD_EXPIRED")


def project_upload(arguments: Any, context: Any) -> dict[str, Any]:
    if (
        not arguments.create
        and not arguments.update
        and (arguments.json or not sys.stdin.isatty())
    ):
        raise SelectionError(
            "Non-interactive and --json uploads require --create or --update APPLICATION_ID."
        )
    if arguments.bundle and arguments.skip_build:
        raise SelectionError("--bundle and --skip-build cannot be combined.")
    server = server_url(arguments.server)
    client = PlatformClient(server, credential(server))
    project = resolve_project(
        context.workspace, arguments.project, Path.cwd()
    ).resolve()
    manifest = load_manifest(project)
    build_version = None
    if arguments.bundle:
        bundle = Path(arguments.bundle).expanduser().resolve()
    else:
        if not arguments.skip_build:
            python, _ = ensure_iris_tools(context)
            run_idf_target(
                context,
                idf_path=resolve_idf_path(context.workspace, project),
                project=project,
                build_dir=project / "build",
                target="system-update-bundle",
                definitions={"ESP_IRIS_PYTHON": str(python)},
                timeout=3600,
            )
        try:
            description = json.loads(
                (project / "build/project_description.json").read_text(encoding="utf-8")
            )
            build_version = description["project_version"]
            bundle = (
                project
                / "build"
                / (description["project_name"] + "-system-update.irisfw")
            )
        except (OSError, ValueError, KeyError) as error:
            raise SelectionError(
                "A complete build/project_description.json is required."
            ) from error
    if bundle.suffix != ".irisfw" or not bundle.is_file():
        raise SelectionError("Upload requires a local .irisfw bundle.")
    info = file_info(bundle, firmware=True)
    plan = inspect_bundle_plan(context, bundle)
    validate_bundle(plan)
    version = upload_version(plan, arguments.release_version, build_version)
    target = select_target(arguments, client, version)
    metadata = manifest["project"]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "project": str(project),
                "target": target["id"] if target else "create",
                "metadata": metadata,
                "markdown": manifest["markdown"],
                "changelog": manifest["changelog"],
                "version": version,
                "bundle": info,
                "files": {str(path): item for path, item in manifest["files"].items()},
                "groups": {
                    name: [str(path) for path in paths]
                    for name, paths in manifest["groups"].items()
                },
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    ledger_path = private_root(server) / ("upload-" + fingerprint + ".json")
    ledger = read_state(ledger_path)
    if not ledger:
        ledger = {"key": str(uuid.uuid4()), "assets": {}}
        write_state(ledger_path, ledger)
    key = ledger["key"]
    if "project" not in ledger:
        if target:
            selected = {"id": target["id"], "version": target["version"]}
        else:
            selected = client.request(
                "POST",
                "/firmware-projects",
                {
                    **metadata,
                    "description": "",
                    "firmware_variants": [],
                    "cover_images": [],
                    "current_version": version,
                },
                key=key + ":create",
            )
        ledger["project"] = selected
        write_state(ledger_path, ledger)
    selected = ledger["project"]
    project_path = "/firmware-projects/" + _segment(selected["id"])
    if "draft" not in ledger:
        ledger["draft"] = client.request(
            "POST",
            project_path + "/draft",
            {"expected_project_version": selected["version"]},
            key=key + ":draft",
        )
        write_state(ledger_path, ledger)
    draft = ledger["draft"]
    revision_path = project_path + "/revisions/" + _segment(draft["revision_id"])
    uploaded_bundle = upload_file(
        client,
        bundle,
        {**info, "kind": "firmware"},
        revision_path + "/iris-uploads",
        revision_path + "/iris-files",
        key + ":bundle",
        ledger,
        ledger_path,
    )
    article_path = (
        "/articles/"
        + _segment(draft["article_id"])
        + "/revisions/"
        + _segment(draft["revision_id"])
    )
    uploaded = {}
    for index, (path, resource) in enumerate(manifest["files"].items()):
        uploaded[path] = upload_file(
            client,
            path,
            {**resource, "kind": "article_resource"},
            article_path + "/uploads",
            article_path + "/files",
            key + ":resource:" + str(index),
            ledger,
            ledger_path,
        )
    attachments = []
    for path in manifest["groups"]["attachments"]:
        asset = uploaded[path]
        attachments.append(
            {
                "id": asset["id"],
                "name": asset["filename"],
                "size": asset["size"],
                "media_type": asset["media_type"],
                "upload_id": asset["id"],
            }
        )
    result = client.request(
        "PUT",
        project_path + "/draft/iris-version",
        {
            **metadata,
            "revision_id": draft["revision_id"],
            "expected_project_version": draft["version"],
            "expected_revision_version": draft["revision_version"],
            "bundle_asset_id": uploaded_bundle["id"],
            "current_version": version,
            "changelog": manifest["changelog"],
            "description": rewritten_readme(manifest, uploaded),
            "cover_images": [
                uploaded[path]["url"] for path in manifest["groups"]["covers"]
            ],
            "attachments": attachments,
        },
        key=key + ":finalize",
    )
    if result.get("status") != "draft":
        raise OperationError("Mosaico Ideas returned an unexpected final upload status.")
    ledger_path.unlink()
    return {"command": "project upload", **result}
