"""Workspace application creation and preview using public dependency entry points."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

from .errors import EnvironmentError, SelectionError
from .project import resolve_project
from .scaffold import initialize_project

GAME_TEMPLATES = {"shooter": "raylib_shooter", "sky-hop": "sky_hop", "tower-defense": "tower_defense"}


def add_commands(commands, project_commands):
    preview = project_commands.add_parser("sim", help="Preview a GSP application with its native PC backend")
    preview.set_defaults(command="sim", public_command="project sim")
    preview.add_argument("--project")
    preview.add_argument("--scene-only", action="store_true")
    mode = preview.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true")
    mode.add_argument("--interactive", action="store_true")
    preview.add_argument("--duration", type=float)
    preview.add_argument("--frames", type=int)
    preview.add_argument("--fps", type=int)
    preview.add_argument("--dump-ppm")
    game = commands.add_parser("game", help="Create BSP game examples or use the Raylib Host simulator")
    actions = game.add_subparsers(dest="game_action", required=True)
    for name in ("create", "new"):
        create = actions.add_parser(name, help="Create a game from BSP examples")
        create.set_defaults(command="game", public_command="game " + name)
        create.add_argument("name")
        create.add_argument("--template", choices=tuple(GAME_TEMPLATES), default="shooter")
        create.add_argument("--dry-run", action="store_true")
    for name in ("sim", "run", "build"):
        child = actions.add_parser(name)
        child.set_defaults(command="game", public_command="game " + name)
        child.add_argument("project_path", nargs="?")
        child.add_argument("--project")
        if name == "build":
            child.add_argument("--idf-path")
        else:
            child.add_argument("--headless", action="store_true")
            child.add_argument("--frames", type=int, default=300)
            child.add_argument("--listen", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
            child.add_argument("--port", type=int, default=8460)
            child.add_argument("--scenario", "--replay", dest="replay")
            child.add_argument("--state-output")


def run(arguments, workspace):
    tools_root = workspace.recovery_project.parents[2] / "mosaico-tools"
    if arguments.command == "sim":
        project = resolve_project(workspace, arguments.project, Path.cwd())
        command = [sys.executable, str(tools_root / "tools/gsp-sim/run.py"),
                   "--project", str(project)]
        for flag in ("headless", "interactive", "scene_only"):
            if getattr(arguments, flag):
                command.append("--" + flag.replace("_", "-"))
        for flag in ("duration", "frames", "fps", "dump_ppm"):
            value = getattr(arguments, flag)
            if value is not None:
                command.extend(("--" + flag.replace("_", "-"), str(value)))
        return subprocess.call(command)

    if arguments.game_action in {"create", "new"}:
        template = workspace.bsp_path / "examples" / GAME_TEMPLATES[arguments.template] / "mosaico-template.json"
        if not template.is_file():
            raise EnvironmentError("Initialize the BSP submodule containing the selected game template: " + str(template))
        engine = workspace.raylib_path
        if engine is None or not (engine / "cmake/mosaico_game_sdk.cmake").is_file():
            raise EnvironmentError("Initialize the configured Raylib Lite Engine dependency before creating a game.")
        result = initialize_project(replace(workspace, init_template=template), arguments.name, dry_run=arguments.dry_run)
        if arguments.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"game: {result['status']}\nProject: {result['project']}\n{result['install_command']}")
        return 0

    if arguments.project and arguments.project_path:
        raise SelectionError("Use either the positional project or --project.")
    project = resolve_project(workspace, arguments.project or arguments.project_path, Path.cwd())
    if arguments.game_action == "build":
        import os
        from .runtime import RunContext, build_application
        if arguments.idf_path:
            os.environ["IDF_PATH"] = str(Path(arguments.idf_path).expanduser().resolve())
        context = RunContext(workspace, "game-build", arguments.verbose, arguments.json)
        build_application(context, project)
        return 0
    engine = workspace.raylib_path
    if engine is None or not (engine / "host/run_game.py").is_file():
        raise EnvironmentError("Initialize the configured Raylib Lite Engine dependency before simulation.")
    command = [sys.executable, str(engine / "host/run_game.py"), "--project", str(project),
               "--listen", arguments.listen, "--port", str(arguments.port)]
    if arguments.headless:
        command.extend(("--headless", "--frames", str(arguments.frames)))
    for name in ("replay", "state_output"):
        value = getattr(arguments, name)
        if value:
            command.extend(("--" + name.replace("_", "-"), value))
    return subprocess.call(command)
