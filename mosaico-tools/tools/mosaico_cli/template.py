"""Render declarative, workspace-owned project templates without executing code."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

from .errors import EnvironmentError
from .workspace import WorkspaceConfig


RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}
_TOKEN = re.compile(r"\{\{([^{}]*)\}\}")


def shell_quote(value: str) -> str:
    return subprocess.list2cmdline([value]) if os.name == "nt" else shlex.quote(value)


def _object(value: Any, allowed: set[str], required: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise EnvironmentError(f"Template {label} must be an object.")
    if set(value) - allowed or required - set(value):
        raise EnvironmentError(f"Template {label} has missing or unsupported fields.")
    return value


def _string(value: Any, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        requirement = "a string" if empty else "a non-empty string"
        raise EnvironmentError(f"Template {label} must be {requirement}.")
    return value


def _file_path(value: Any, label: str) -> Path:
    text = _string(value, label)
    parts = text.split("/")
    if (
        any(character in text for character in '\\:<>"|?*\x00')
        or any(ord(character) < 32 for character in text)
        or any(
            part in ("", ".", "..") or part.endswith((".", " "))
            or part.split(".")[0].lower() in RESERVED_NAMES for part in parts
        )
    ):
        raise EnvironmentError(f"Template {label} must be a portable relative file path: {text!r}")
    return Path(*parts)


def _paths(value: Any, workspace: WorkspaceConfig, template: Path) -> dict[str, Path]:
    if not isinstance(value, dict):
        raise EnvironmentError("Template paths must be an object.")
    anchors = {
        "workspace": workspace.root,
        "utils": workspace.recovery_project.parents[2],
        "tools": workspace.recovery_project.parents[2] / "mosaico-tools",
        "engine": workspace.raylib_path or workspace.root / "submodule/raylib-lite-engine",
        "template": template.parent,
        "bsp": workspace.bsp_path,
        "esp_iris": workspace.esp_iris_path,
    }
    result = {}
    for name, raw in value.items():
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in {"project_name", "project_path"}
        ):
            raise EnvironmentError(f"Invalid or reserved template path variable: {name}")
        entry = _object(raw, {"base", "path", "required"}, {"base", "path"}, f"path {name}")
        base = _string(entry["base"], f"path {name}.base")
        if base not in anchors:
            raise EnvironmentError(f"Unknown template path base: {base}")
        path_text = _string(entry["path"], f"path {name}.path")
        relative = Path(".") if path_text == "." else _file_path(path_text, f"path {name}.path")
        path = (anchors[base] / relative).resolve()
        required = entry.get("required")
        if "required" in entry and required not in ("file", "directory"):
            raise EnvironmentError(f"Template path {name}.required must be 'file' or 'directory'.")
        if (
            (required == "file" and not path.is_file())
            or (required == "directory" and not path.is_dir())
        ):
            raise EnvironmentError(f"Required template resource ({required}) is missing: {path}")
        result[name] = path
    return result


def _expand(text: str, values: dict[str, Any], destination: Path) -> str:
    def substitute(match: re.Match) -> str:
        parts = [part.strip() for part in match.group(1).split("|")]
        if len(parts) > 2 or parts[0] not in values:
            raise EnvironmentError(f"Unknown template variable: {match.group(0)}")
        value = values[parts[0]]
        if isinstance(value, Path):
            try:
                value = Path(os.path.relpath(value, destination.parent)).as_posix()
            except ValueError as error:
                raise EnvironmentError(
                    f"Cannot create a relative reference from {destination.parent} to {value}; "
                    "keep the project and referenced dependencies on the same drive."
                ) from error
        format_name = parts[1] if len(parts) == 2 else "raw"
        if format_name == "json":
            return json.dumps(value, ensure_ascii=False)
        if format_name == "shell":
            return shell_quote(value)
        if format_name == "cmake":
            for character in ("\\", '"', "$", ";"):
                value = value.replace(character, "\\" + character)
            return value
        if format_name != "raw":
            raise EnvironmentError(f"Unknown template variable format: {format_name}")
        return value

    # Expand only descriptor replacement/append strings, not arbitrary source.
    return _TOKEN.sub(substitute, text)


def render_template(
    workspace: WorkspaceConfig, template: Path, project: Path, name: str
) -> dict[str, bytes]:
    """Validate a v1 description and render every file before any output is written."""
    try:
        description = _object(
            json.loads(template.read_text(encoding="utf-8")),
            {"schema_version", "paths", "files"}, {"schema_version", "files"}, "description",
        )
        if type(description["schema_version"]) is not int or description["schema_version"] != 1:
            raise EnvironmentError("Unsupported template schema_version; expected 1.")
        entries = description["files"]
        if not isinstance(entries, list) or not entries:
            raise EnvironmentError("Template files must be a non-empty array.")
        values = {
            "project_name": name,
            "project_path": project.relative_to(workspace.root).as_posix(),
            **_paths(description.get("paths", {}), workspace, template),
        }
        files = {}
        destinations = set()
        for index, raw in enumerate(entries):
            label = f"files[{index}]"
            entry = _object(
                raw, {"source", "destination", "replacements", "append"}, {"source"}, label
            )
            source = template.parent / _file_path(entry["source"], f"{label}.source")
            relative = _file_path(entry.get("destination", entry["source"]), f"{label}.destination")
            key = relative.as_posix().casefold()
            if any(
                key == other or key.startswith(other + "/") or other.startswith(key + "/")
                for other in destinations
            ):
                raise EnvironmentError(f"Conflicting template destination: {relative}")
            destinations.add(key)
            if (
                not source.is_file() or source.is_symlink()
                or template.parent not in source.resolve().parents
            ):
                raise EnvironmentError(f"Template source is missing or not a regular local file: {source}")
            content = source.read_bytes()
            replacements = entry.get("replacements", [])
            if not isinstance(replacements, list):
                raise EnvironmentError(f"Template {label}.replacements must be an array.")
            if replacements or "append" in entry:
                text = content.decode("utf-8").replace("\r\n", "\n")
                destination = project / relative
                for rule_index, raw_rule in enumerate(replacements):
                    rule_label = f"{label}.replacements[{rule_index}]"
                    rule = _object(
                        raw_rule, {"pattern", "replacement", "count"},
                        {"pattern", "replacement"}, rule_label,
                    )
                    pattern = _string(rule["pattern"], f"{rule_label}.pattern")
                    replacement = _expand(
                        _string(rule["replacement"], f"{rule_label}.replacement", empty=True),
                        values, destination,
                    )
                    expected = rule.get("count", 1)
                    if type(expected) is not int or expected < 1:
                        raise EnvironmentError(f"Template {rule_label}.count must be a positive integer.")
                    text, count = re.subn(
                        pattern, lambda match: replacement, text, flags=re.MULTILINE
                    )
                    if count != expected:
                        raise EnvironmentError(
                            f"Template {rule_label}: expected {expected} match(es) in {source}, found {count}."
                        )
                if "append" in entry:
                    text += _expand(
                        _string(entry["append"], f"{label}.append", empty=True), values, destination
                    )
                content = text.encode("utf-8")
            files[relative.as_posix()] = content
        return files
    except (OSError, ValueError, re.error) as error:
        raise EnvironmentError(f"Could not render template {template}: {error}") from error
