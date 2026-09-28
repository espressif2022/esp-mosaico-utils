"""Workspace application creation and preview using public dependency entry points."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

from .errors import EnvironmentError, SelectionError
from .project import resolve_project
from .template import RESERVED_NAMES

IRIS_PROJECT_CMAKE = """cmake_minimum_required(VERSION 3.16)
set(RAYLIB_LITE_GAME {game})
include("{module}")
project({game} VERSION 1.0.0)
include("${{MOSAICO_SYSTEM_UPDATE_CMAKE}}")
"""


def _engine(workspace, purpose):
    engine = workspace.raylib_path
    if engine is None or not (engine / "tools/game_cli.py").is_file():
        raise EnvironmentError(f"Initialize the configured Raylib Lite Engine dependency before {purpose}.")
    return engine


def _engine_games(engine, target):
    completed = subprocess.run(
        [sys.executable, str(engine / "tools/game_cli.py"), "list", "--json", "--target", target],
        capture_output=True, text=True,
    )
    if completed.returncode:
        raise EnvironmentError(completed.stderr.strip() or "Engine game listing failed.")
    return {game["name"]: Path(game["path"]) for game in json.loads(completed.stdout)["games"]}


def _iris_project(workspace, selected):
    """Generate the ESP-Iris wrapper project for one native engine game."""
    engine = _engine(workspace, "an Iris build")
    games = _engine_games(engine, "native")
    name = Path(selected).name
    if name not in games:
        raise SelectionError(f"Not a native engine game: {selected}; choose one of {', '.join(sorted(games))}.")
    template = workspace.tool_root / "templates" / "raylib_lite_iris"
    project = workspace.run_dir / "raylib-iris" / name
    project.mkdir(parents=True, exist_ok=True)
    (project / "CMakeLists.txt").write_text(IRIS_PROJECT_CMAKE.format(
        game=name, module=(workspace.tool_root / "cmake/raylib_lite_iris_app.cmake").as_posix()))
    (project / "partitions.csv").write_bytes((template / "partitions.csv").read_bytes())
    return project


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
    game = commands.add_parser("game", help="Create and run Raylib Lite Engine games")
    actions = game.add_subparsers(dest="game_action", required=True)
    for name in ("create", "new"):
        create = actions.add_parser(name, help="Create a game in the configured engine checkout")
        create.set_defaults(command="game", public_command="game " + name)
        create.add_argument("name")
        create.add_argument("--template", default="shooter",
                            help="Engine template alias or game name (see the engine's game_cli.py list)")
        create.add_argument("--dry-run", action="store_true")
    for name in ("sim", "run", "build"):
        child = actions.add_parser(name)
        child.set_defaults(command="game", public_command="game " + name)
        child.add_argument("project_path", nargs="?")
        child.add_argument("--project")
        if name == "build":
            child.add_argument("--idf-path")
            child.add_argument("--target", choices=("native", "iris"), default="native",
                               help="native: build the project as is; iris: wrap an engine game as an ESP-Iris app")
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
        engine = _engine(workspace, "creating a game")
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", arguments.name)
                or arguments.name.lower() in RESERVED_NAMES
                or len(arguments.name) > 31):
            raise SelectionError("Game name must be at most 31 ASCII letters, digits or underscores, starting with a letter.")
        project = engine / "examples" / arguments.name
        if project.exists():
            raise SelectionError(f"Game already exists: {project}")
        completed = subprocess.run(
            [sys.executable, str(engine / "tools/game_cli.py"), "create", arguments.name,
             "--json", "--template", arguments.template] + (["--dry-run"] if arguments.dry_run else []),
            capture_output=True, text=True,
        )
        if completed.returncode:
            raise SelectionError(completed.stderr.strip().splitlines()[-1] if completed.stderr.strip()
                                 else completed.stdout.strip())
        result = json.loads(completed.stdout)
        if arguments.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"game: {result['status']}\nProject: {result['project']}")
        return 0

    if arguments.project and arguments.project_path:
        raise SelectionError("Use either the positional project or --project.")
    if not (arguments.project or arguments.project_path):
        raise SelectionError("Specify an engine game with --project PATH.")
    iris = arguments.game_action == "build" and arguments.target == "iris"
    selected = Path(arguments.project or arguments.project_path).expanduser()
    candidates = ([selected.resolve()] if selected.is_absolute() else [
        (Path.cwd() / selected).resolve(),
        (workspace.root / selected).resolve(),
        (workspace.raylib_path / selected).resolve(),
    ])
    required = "CMakeLists.txt" if arguments.game_action == "build" else "game.sim.json"
    project = (_iris_project(workspace, selected) if iris else
               next((path for path in candidates if (path / required).is_file()), None))
    if project is None:
        raise SelectionError(f"Game project must contain {required}: {selected}")
    if arguments.game_action == "build":
        import os
        from .runtime import RunContext, build_application
        if arguments.idf_path:
            os.environ["IDF_PATH"] = str(Path(arguments.idf_path).expanduser().resolve())
        os.environ["MOSAICO_BSP_ROOT"] = str(workspace.bsp_path)
        os.environ["MOSAICO_UTILS_ROOT"] = str(workspace.tool_root.parent)
        os.environ["RAYLIB_LITE_ENGINE_ROOT"] = str(workspace.raylib_path)
        context = RunContext(workspace, "game-build", arguments.verbose, arguments.json)
        build_application(context, project)
        return 0
    engine = _engine(workspace, "simulation")
    command = [sys.executable, str(engine / "host/run_game.py"), "--project", str(project),
               "--listen", arguments.listen, "--port", str(arguments.port)]
    if arguments.headless:
        command.extend(("--headless", "--frames", str(arguments.frames)))
    for name in ("replay", "state_output"):
        value = getattr(arguments, name)
        if value:
            command.extend(("--" + name.replace("_", "-"), value))
    return subprocess.call(command)
