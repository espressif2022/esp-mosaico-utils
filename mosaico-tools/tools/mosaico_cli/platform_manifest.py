"""External project metadata; firmware archives never contain platform identity."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import urllib.parse
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .errors import SelectionError

SCHEMA = "mosaico-ideas/project-upload/v1"
MAX_TOTAL = 100 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
CATEGORIES = {"smart-home", "sensors", "display", "audio", "communication", "motor"}
BOARDS = {"camera", "ethernet", "battery"}
# Both inline links (including angle destinations) and reference definitions.
LINKS = re.compile(
    r"(?P<prefix>!?\[[^\]\n]*\]\(\s*|^\s{0,3}\[[^\]\n]+\]:\s*)(?P<url><[^>\n]+>|[^\s)]+)",
    re.MULTILINE,
)
CODE = re.compile(
    r"(```[^\n]*\n.*?^```[^\n]*$|~~~[^\n]*\n.*?^~~~[^\n]*$|`[^`\n]*`)",
    re.MULTILINE | re.DOTALL,
)


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - keys:
        raise SelectionError("Invalid or unknown fields in " + label + ".")
    return value


def _text(value: Any, label: str, maximum: int, minimum: int = 0) -> str:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or "\x00" in value
    ):
        raise SelectionError("Invalid " + label + ".")
    return value


def _strings(
    value: Any, label: str, maximum: int, choices: set[str] | None = None
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise SelectionError("Invalid " + label + " list.")
    result = [_text(item, label, 50, 1) for item in value]
    if len(set(result)) != len(result) or (
        choices is not None and set(result) - choices
    ):
        raise SelectionError("Duplicate or unsupported " + label + ".")
    return result


def local_path(root: Path, value: str) -> Path:
    _text(value, "resource path", 1024, 1)
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or "\\" in value
        or ".." in path.parts
    ):
        raise SelectionError(
            "Resource paths must remain inside the application directory."
        )
    resolved = (root / value).resolve()
    if root.resolve() not in resolved.parents or not resolved.is_file():
        raise SelectionError(
            "Resource is missing or escapes the application directory: " + value
        )
    if len(resolved.name.encode("utf-8")) > 255 or any(
        ord(c) < 32 or ord(c) == 127 for c in resolved.name
    ):
        raise SelectionError("Invalid resource filename.")
    return resolved


def file_info(path: Path, *, firmware: bool = False) -> dict[str, Any]:
    media = (
        "application/vnd.esp-iris.system-update+zip"
        if firmware
        else (mimetypes.guess_type(path.name)[0] or "application/octet-stream")
    )
    limit = (
        32 * 1024 * 1024
        if firmware
        else (10 if path.suffix.lower() in IMAGE_EXTENSIONS else 20) * 1024 * 1024
    )
    size = path.stat().st_size
    if not 1 <= size <= limit:
        raise SelectionError(
            "Resource exceeds its size limit or is empty: " + path.name
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "filename": path.name,
        "media_type": media,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _local_destination(value: str) -> tuple[str, str] | None:
    value = value.strip("<>")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in {"http", "https", "mailto"} or value.startswith("#"):
        return None
    if parsed.scheme or parsed.netloc or parsed.query:
        raise SelectionError("Unsupported README resource URL.")
    return urllib.parse.unquote(parsed.path), (
        "#" + parsed.fragment if parsed.fragment else ""
    )


def map_markdown(markdown: str, replace: Any) -> str:
    sections = CODE.split(markdown)
    for index in range(0, len(sections), 2):
        sections[index] = LINKS.sub(
            lambda match: match["prefix"] + replace(match["url"]), sections[index]
        )
    return "".join(sections)


def load_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    path = root / "mosaico-ideas.json"
    try:
        if path.stat().st_size > 64 * 1024:
            raise SelectionError("mosaico-ideas.json exceeds 64 KiB.")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SelectionError(
            "Cannot read mosaico-ideas.json from the application directory."
        ) from error
    value = _object(
        value, {"schema", "project", "content", "release"}, "mosaico-ideas.json"
    )
    if value.get("schema") != SCHEMA:
        raise SelectionError("Unsupported mosaico-ideas.json schema.")
    metadata = _object(
        value.get("project"),
        {"title", "categories", "boards", "tags", "license", "external_links"},
        "project",
    )
    metadata["title"] = _text(metadata.get("title"), "title", 200, 1)
    metadata["license"] = _text(metadata.get("license", ""), "license", 500)
    metadata["categories"] = _strings(
        metadata.get("categories"), "categories", 6, CATEGORIES
    )
    if not metadata["categories"]:
        raise SelectionError("At least one category is required.")
    metadata["boards"] = _strings(metadata.get("boards", []), "boards", 2, BOARDS)
    metadata["tags"] = _strings(metadata.get("tags", []), "tags", 20)
    links = metadata.setdefault("external_links", [])
    if not isinstance(links, list) or len(links) > 10:
        raise SelectionError("Invalid external_links.")
    for link in links:
        _object(link, {"label", "url", "platform"}, "external link")
        _text(link.get("label"), "link label", 120, 1)
        url = _text(link.get("url"), "link URL", 2048, 1)
        if urllib.parse.urlsplit(url).scheme not in {"http", "https"} or link.get(
            "platform"
        ) not in {"github", "oshwhub", "makerworld", "other"}:
            raise SelectionError("Invalid external link URL or platform.")
    release = _object(value.get("release", {}), {"changelog"}, "release")
    changelog = _text(release.get("changelog", ""), "changelog", 10000)
    content = _object(
        value.get("content"),
        {"readme", "covers", "resources", "attachments"},
        "content",
    )
    readme = local_path(root, content.get("readme"))
    if readme.stat().st_size > 400000:
        raise SelectionError("README exceeds 400 KiB.")
    try:
        markdown = readme.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise SelectionError("README must be UTF-8.") from error
    _text(markdown, "README", 100000)
    files: dict[Path, dict[str, Any]] = {}
    groups: dict[str, list[Path]] = {}
    for group, maximum in (("covers", 9), ("resources", 100), ("attachments", 20)):
        values = content.get(group, [])
        if not isinstance(values, list) or len(values) > maximum:
            raise SelectionError("Too many " + group + ".")
        groups[group] = []
        for name in values:
            resource = local_path(root, name)
            if resource == readme or resource in files:
                raise SelectionError("Duplicate resource file: " + name)
            if group == "covers" and resource.suffix.lower() not in IMAGE_EXTENSIONS:
                raise SelectionError("Covers must be JPEG, PNG, GIF or WebP images.")
            files[resource] = file_info(resource)
            groups[group].append(resource)

    destinations: dict[str, tuple[Path, str]] = {}

    def discover(raw: str) -> str:
        destination = _local_destination(raw)
        if destination is not None:
            relative, fragment = destination
            if (
                PurePosixPath(relative).is_absolute()
                or ".." in PurePosixPath(relative).parts
            ):
                raise SelectionError(
                    "README resource paths must remain inside the application directory."
                )
            resource = local_path(
                root, (readme.parent / relative).relative_to(root).as_posix()
            )
            if resource == readme:
                raise SelectionError("Use a #fragment to link within this README.")
            files.setdefault(resource, file_info(resource))
            destinations[raw] = resource, fragment
        return raw

    map_markdown(markdown, discover)
    referenced = {path for path, _ in destinations.values()}
    if any(path not in referenced for path in groups["resources"]):
        raise SelectionError(
            "Each content.resources file must be linked from README; use attachments for standalone files."
        )
    if len(files) > 100 or sum(item["size"] for item in files.values()) > MAX_TOTAL:
        raise SelectionError("Resources exceed 100 files or 100 MiB in total.")
    return {
        "project": metadata,
        "markdown": markdown,
        "changelog": changelog,
        "files": files,
        "groups": groups,
        "destinations": destinations,
    }


def rewritten_readme(manifest: dict[str, Any], uploaded: dict[Path, Any]) -> str:
    def replace(raw: str) -> str:
        destination = manifest["destinations"].get(raw)
        if destination is None:
            return raw
        path, fragment = destination
        return uploaded[path]["url"] + fragment

    result = map_markdown(manifest["markdown"], replace)
    return _text(result, "rewritten README", 100000)


def upload_version(
    plan: dict[str, Any], explicit: str | None, build_version: str | None
) -> str:
    values = [
        value
        for value in (explicit, build_version, plan.get("release"))
        if value is not None
    ]
    if not values:
        raise SelectionError("The bundle has no release; supply --version.")
    for value in values:
        _text(value, "version", 80, 1)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value) is None:
            raise SelectionError(
                "Version must begin with a letter/digit and contain only letters, digits, '.', '_', '+', '-'."
            )
    if len(set(values)) != 1:
        raise SelectionError(
            "--version, build project_version and bundle release must match exactly."
        )
    return values[0]


def validate_bundle(plan: dict[str, Any]) -> None:
    components = plan.get("components", [])
    if plan.get("key_id") or plan.get("signature_verified"):
        raise SelectionError(
            "Mosaico Ideas currently accepts unsigned .irisfw bundles only."
        )
    if (
        not components
        or sum(item.get("kind") == "application" for item in components) != 1
        or any(
            item.get("kind") not in {"application", "partition_table", "data"}
            or item.get("flags", 0) != 0
            for item in components
        )
    ):
        raise SelectionError(
            "Bundle requires exactly one application and only unflagged application/data/partition_table components."
        )
    if plan.get("chip_id") != 32:
        raise SelectionError("Mosaico Ideas Iris bundles require ESP32-S31 (chip_id 32).")
    flash = plan.get("flash_size", 0)
    if not isinstance(flash, int) or not 1 <= flash <= 32 * 1024 * 1024:
        raise SelectionError("Invalid bundle flash size.")
    for item in components:
        if (
            item.get("target_offset", -1) < 0
            or item.get("size", 0) < 1
            or item["target_offset"] + item["size"] > flash
        ):
            raise SelectionError("Bundle component exceeds flash capacity.")
