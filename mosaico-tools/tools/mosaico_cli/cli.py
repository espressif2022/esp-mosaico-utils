"""Argument parsing and stable output for the public mosaico.py command."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NoReturn

from . import __version__
from .commands import (
    configure_recovery_network,
    enter_recovery,
    inspect_crash,
    install,
    invoke_rpc,
    list_devices,
    monitor,
    monitor_memory,
    read_bridge_code,
    recover,
    start_system_update,
)
from .doctor import diagnose_host, print_diagnosis
from .errors import MosaicoError, SelectionError
from .runtime import RunContext
from .scaffold import initialize_project
from .workspace import load_workspace

TOOL_ROOT = Path(__file__).resolve().parents[2]


class MosaicoArgumentParser(argparse.ArgumentParser):
    json_errors = False

    def parse_args(self, args=None, namespace=None):
        if self.prog == "mosaico.py":
            args = _canonical_argv(_normalize_globals(list(sys.argv[1:] if args is None else args)))
        return super().parse_args(args, namespace)

    def error(self, message: str) -> NoReturn:
        if self.json_errors:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "selection_error",
                        "message": message,
                        "exit_code": 2,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            raise SystemExit(2)
        super().error(message)


def uuid_argument(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("takeover ID must be a UUID") from error


def positive_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return result


def hardware_mac(value: str) -> str:
    compact = value.replace(":", "").replace("-", "").lower()
    if len(compact) != 12 or any(
        character not in "0123456789abcdef" for character in compact
    ):
        raise argparse.ArgumentTypeError(
            "hardware MAC must contain exactly 12 hexadecimal digits"
        )
    return ":".join(compact[index:index + 2] for index in range(0, 12, 2))


def monitor_timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if result < 0:
        raise argparse.ArgumentTypeError("must not be less than 0")
    return result


def nand_manifest_path(value: str) -> str:
    if not value.startswith("/nand/") or value.endswith("/") or "\\" in value:
        raise argparse.ArgumentTypeError("must be an absolute file below /nand")
    segments = value[len("/nand/") :].split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise argparse.ArgumentTypeError("must not contain empty, '.' or '..' segments")
    if len(value.encode("utf-8")) >= 256:
        raise argparse.ArgumentTypeError("must be shorter than 256 UTF-8 bytes")
    return value


# Legacy spellings remain accepted but are omitted from the public command tree.
COMMAND_PATHS = {
    "init": ("project", "init"),
    "install": ("iris", "app-update"),
    "system-update": ("iris", "system-update"),
    "list": ("iris", "list"),
    "monitor": ("iris", "logs"),
    "memory": ("iris", "memory"),
    "crash": ("iris", "crash"),
    "rpc": ("iris", "rpc"),
    "enter-recovery": ("iris", "test", "enter-recovery"),
    "recovery-wifi": ("iris", "test", "recovery-wifi"),
    "bridge-code": ("iris", "test", "bridge-code"),
}


def _canonical_argv(argv: Sequence[str]) -> list[str]:
    result = list(argv)
    index = 0
    while index < len(result):
        token = result[index]
        if token == "--workspace":
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        if token in COMMAND_PATHS:
            result[index:index + 1] = COMMAND_PATHS[token]
        elif token == "session":
            result[index] = "iris"
        elif token == "device":
            result[index] = "iris"
        break
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = MosaicoArgumentParser(
        prog="mosaico.py",
        description="Unified ESP-Mosaico project and device command-line tool",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit stable JSON; iris logs emits NDJSON"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show internal stages and full log paths"
    )
    parser.add_argument(
        "--workspace",
        help="Workspace directory or .mosaico.json path; discovered from cwd by default",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="group", required=True)
    project_parser = commands.add_parser("project", help="Create application projects")
    project_commands = project_parser.add_subparsers(dest="project_action", required=True)
    from .app_commands import add_commands
    add_commands(commands, project_commands)

    upload_parser = project_commands.add_parser("upload", help="Upload an unsigned Iris application/version as a Mosaico Ideas draft")
    upload_parser.set_defaults(command="project-upload")
    upload_parser.add_argument("--project", help="Application directory with mosaico-ideas.json")
    upload_parser.add_argument("--skip-build", action="store_true")
    upload_parser.add_argument("--bundle", help="Existing .irisfw bundle; no build is performed")
    upload_parser.add_argument("--version", dest="release_version", help="Exact release version; must match other version sources")
    upload_target = upload_parser.add_mutually_exclusive_group()
    upload_target.add_argument("--create", action="store_true")
    upload_target.add_argument("--update", metavar="APPLICATION_ID")
    upload_parser.add_argument("--yes", action="store_true", help="Confirm creation/update and replacement of any existing draft")
    upload_parser.add_argument("--server", help="Mosaico Ideas HTTPS origin (or MAKER_SPARK_SERVER)")
    account_parser = commands.add_parser("account", help="Manage browser-approved Mosaico Ideas login")
    account_commands = account_parser.add_subparsers(dest="account_action", required=True)
    for account_action in ("login", "status", "logout"):
        account_child = account_commands.add_parser(account_action)
        account_child.set_defaults(command="account")
        account_child.add_argument("--server", help="Mosaico Ideas HTTPS origin (or MAKER_SPARK_SERVER)")
        if account_action == "login":
            account_child.add_argument("--open-browser", action="store_true", help="Open the verification page in the system browser")
    iris_parser = commands.add_parser("iris", help="Project Gateway and ESP-Iris device operations")
    iris_commands = iris_parser.add_subparsers(dest="iris_action", required=True)
    test_parser = iris_commands.add_parser("test", help="Test Recovery transitions, Wi-Fi and Bridge pairing")
    test_commands = test_parser.add_subparsers(dest="test_action", required=True)
    leaves = []

    def command(name, **kwargs):
        # Keep operation identifiers stable for evidence records and handlers.
        path = COMMAND_PATHS.get(name, (name,))
        if path[0] == "project":
            parent = project_commands
        elif path[:2] == ("iris", "test"):
            parent = test_commands
        elif path[0] == "iris":
            parent = iris_commands
        else:
            parent = commands
        child = parent.add_parser(path[-1], **kwargs)
        child.set_defaults(command=name, public_command=" ".join(path))
        leaves.append(child)
        return child

    init_parser = command(
        "init",
        help="Create an application from the workspace's template description",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    init_parser.add_argument(
        "name",
        help="New project name: 1-31 letters, digits or underscores, starting with a letter",
    )
    init_parser.add_argument(
        "--dry-run", action="store_true", help="Validate and list files without writing"
    )

    command(
        "doctor",
        help="Check the host environment without building or writing a device",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    install_parser = command(
        "install",
        help="Install code-only firmware; requires an identical device partition table",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    install_parser.add_argument(
        "--project", help="ESP-IDF application path; selected automatically by default"
    )
    install_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    install_parser.add_argument(
        "--gateway-profile", help="External ESP-Iris profile; use this project's local Gateway by default"
    )
    install_parser.add_argument(
        "--skip-build", action="store_true", help="Reuse a complete existing build"
    )
    install_parser.add_argument(
        "--validation",
        choices=("elf-sha256", "version"),
        default="elf-sha256",
        help="Firmware identity validation method after installation",
    )
    install_parser.add_argument(
        "--timeout", type=positive_timeout, default=600.0, help="Installation timeout in seconds"
    )

    system_update_parser = command(
        "system-update",
        help="Install application layout/resources together (preferred for new projects) or a Recovery bundle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    system_update_source = system_update_parser.add_mutually_exclusive_group()
    system_update_source.add_argument(
        "--bundle",
        type=Path,
        help="Reuse an existing local .irisfw bundle instead of building one",
    )
    system_update_source.add_argument(
        "--manifest-path",
        type=nand_manifest_path,
        help="Absolute NAND LittleFS path of the exploded bundle manifest.json",
    )
    system_update_parser.add_argument(
        "--device-id",
        help="Target Device ID; omit for the sole connected/owned device or available USB device",
    )
    system_update_parser.add_argument(
        "--gateway-profile", help="External ESP-Iris profile; use this project's local Gateway by default"
    )
    system_update_parser.add_argument(
        "--project", help="ESP-IDF application path; selected automatically by default"
    )
    system_update_parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse the default bundle from a complete existing build",
    )
    system_update_parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=900.0,
        help="System Update timeout in seconds",
    )

    recover_parser = command(
        "recover",
        help="Restore the device base firmware",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    recover_parser.add_argument(
        "--model", help="Device model; selected automatically when only one is supported"
    )
    recover_parser.add_argument(
        "--source",
        choices=("reviewed", "current"),
        default="reviewed",
        help="Reviewed base bundle or current-source candidate bundle",
    )
    recover_identity = recover_parser.add_mutually_exclusive_group()
    recover_identity.add_argument(
        "--device-id", help="Device ID used to correlate identity before and after recovery"
    )
    recover_identity.add_argument(
        "--hardware-mac", type=hardware_mac,
        help="Factory eFuse Base MAC used to select a managed or ROM-mode device",
    )
    recover_parser.add_argument(
        "--recovery-port", help="Explicit independent USB Serial/JTAG 303A:1001 port; requires a live managed device"
    )
    recover_parser.add_argument(
        "--timeout", type=positive_timeout, default=180.0,
        help="Recovery and validation timeout in seconds",
    )
    recover_parser.add_argument(
        "--dry-run", action="store_true", help="Check only; do not build or write firmware"
    )

    enter_recovery_parser = command(
        "enter-recovery",
        help="Enter retained Recovery without installing firmware",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    enter_recovery_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    enter_recovery_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    enter_recovery_parser.add_argument(
        "--timeout", type=positive_timeout, default=30.0,
        help="Recovery transition timeout in seconds",
    )

    recovery_wifi_parser = command(
        "recovery-wifi",
        help="Configure Recovery Wi-Fi through the active USB session",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    recovery_wifi_parser.add_argument(
        "--ssid", required=True, help="Wi-Fi SSID; password is read without echo"
    )
    recovery_wifi_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    recovery_wifi_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    recovery_wifi_parser.add_argument(
        "--timeout", type=positive_timeout, default=30.0,
        help="Wi-Fi connection timeout in seconds",
    )

    update_code_parser = command(
        "bridge-code",
        help="Open Recovery's Bridge download page and wait for its pairing code over USB",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    update_code_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    update_code_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )

    update_code_parser.add_argument(
        "--timeout", type=positive_timeout, default=60.0,
        help="Maximum wait for Wi-Fi and Bridge pairing code in seconds",
    )

    monitor_parser = command(
        "monitor",
        help="View retained ESP-Iris logs and follow new logs until interrupted",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    monitor_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    monitor_parser.add_argument(
        "--gateway-profile", help="External ESP-Iris profile; use this project's local Gateway by default"
    )
    monitor_parser.add_argument(
        "--timeout", type=monitor_timeout, default=0.0,
        help="Follow duration in seconds; 0 means no limit",
    )
    monitor_parser.add_argument(
        "--snapshot", action="store_true", help="Print retained logs and exit"
    )
    monitor_parser.add_argument("--grep", help="Client-side text filter")
    monitor_colors = monitor_parser.add_mutually_exclusive_group()
    monitor_colors.add_argument(
        "--force-color", action="store_true", help="Always emit ANSI log colors"
    )
    monitor_colors.add_argument(
        "--disable-auto-color",
        action="store_true",
        help="Disable automatic log coloring",
    )

    list_parser = command(
        "list",
        help="List devices visible through ESP-Iris",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    list_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    list_parser.add_argument(
        "--details",
        action="store_true",
        help="Show endpoint, ESP-IDF version, session, and capabilities",
    )

    memory_parser = command(
        "memory",
        help="Read internal RAM, SPIRAM and each task's stack high-water mark",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    memory_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    memory_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    memory_parser.add_argument(
        "--follow", action="store_true", help="Poll until interrupted"
    )
    memory_parser.add_argument(
        "--interval", type=positive_timeout, default=5.0,
        help="Seconds between polls when --follow is set",
    )

    rpc_parser = command(
        "rpc",
        help="Invoke a raw application RPC through ESP-Iris",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    rpc_parser.add_argument("service_id", help="Numeric RPC service ID")
    rpc_parser.add_argument("method_id", help="Numeric RPC method ID")
    rpc_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    rpc_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    rpc_payload = rpc_parser.add_mutually_exclusive_group()
    rpc_payload.add_argument("--payload", default="", help="UTF-8 request payload")
    rpc_payload.add_argument("--payload-hex", help="Binary request payload as hexadecimal bytes")
    rpc_parser.add_argument(
        "--deadline-ms", type=int, default=3000, help="RPC deadline in milliseconds"
    )

    crash_parser = command(
        "crash",
        help="Inspect and preserve retained crash evidence",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    crash_parser.add_argument(
        "--device-id", help="Target Device ID; omit for the sole connected/owned device or available USB device"
    )
    crash_parser.add_argument(
        "--gateway-profile", help="ESP-Iris profile; use the local Gateway by default"
    )
    crash_parser.add_argument(
        "--archive", action="store_true", help="Archive and decode the retained Core Dump"
    )
    crash_parser.add_argument(
        "--save-core", type=Path, help="Also save the raw Core Dump at this path"
    )
    run_parser = iris_commands.add_parser("run", help="Hold a shared project Gateway client and try connecting its sole device; Ctrl-C releases this client")
    run_parser.set_defaults(command="session", session_action="run", public_command="iris run")
    status_parser = iris_commands.add_parser("status", help="Inspect this project's Gateway without starting it")
    status_parser.set_defaults(command="session", session_action="status", public_command="iris status")
    status_parser.add_argument("--all", dest="all_projects", action="store_true",
                               help="Passively list same-user Gateways, clients and device ownership across workspaces")
    takeover_parser = iris_commands.add_parser("takeover", help="Take a device into this project or recover an interrupted handoff")
    takeover_actions = takeover_parser.add_subparsers(dest="takeover_action", required=True)
    device_parsers = []
    actions = {
        "claim": "Claim the selected or sole available device for this project's Gateway",
        "release": "Release an idle device or endpoint; defaults to the sole owned device",
        "reconcile": "Clear ordinary ownership left by a stopped session",
        "takeover-start": "Request a device from its current Gateway into this project",
        "takeover-status": "Inspect an existing device takeover without starting a Gateway",
        "takeover-resume": "Continue identity validation in the receiving project",
        "takeover-abort": "Roll back an incomplete takeover from its original owning project",
        "takeover-reconcile": "Resolve ownership after both original sessions have stopped",
    }
    for action, help_text in actions.items():
        is_takeover = action.startswith("takeover-")
        verb = action[len("takeover-"):] if is_takeover else action
        path = ("iris", "takeover", verb) if is_takeover else ("iris", verb)
        action_parser = (takeover_actions if is_takeover else iris_commands).add_parser(verb, help=help_text)
        action_parser.set_defaults(command="device", device_action=action, public_command=" ".join(path))
        device_parsers.append(action_parser)
        if action == "claim":
            action_parser.add_argument("--device-id")
            action_parser.add_argument("--endpoint")
        elif action in {"takeover-start", "release", "reconcile"}:
            selection = action_parser.add_mutually_exclusive_group(required=action != "release")
            selection.add_argument("--device-id")
            selection.add_argument("--endpoint")
        if action == "takeover-start":
            action_parser.add_argument("--force", action="store_true", help="Stop mirrors and jobs; wait safely for active writes")
            action_parser.add_argument("--timeout", type=float, default=120, help="Maximum seconds to drain active work (default: 120)")
        if is_takeover:
            action_parser.add_argument("--takeover-id", type=uuid_argument, required=action != "takeover-start",
                                       help="Takeover record ID; reuse this ID after a lost response")
        if action in {"claim", "takeover-start", "takeover-resume"}:
            action_parser.add_argument("--pairing-token-file", type=Path, help="Private TCP pairing token file")
    run_parser.add_argument("--device-id")
    run_parser.add_argument("--endpoint")
    run_parser.add_argument("--pairing-token-file", type=Path, help="Private file containing the TCP pairing token")
    for child in [*leaves, run_parser, status_parser, *device_parsers]:
        if child is init_parser or child.prog.endswith(" doctor"):
            continue
        if "--project" not in child._option_string_actions:
            child.add_argument("--project", help="Project whose development session to use")
        if child in device_parsers:
            continue
        if child is not recover_parser and "--device-id" in child._option_string_actions and "--endpoint" not in child._option_string_actions:
            child.add_argument("--endpoint", help="Explicit discovered endpoint for first identity handshake")
        if "--device-id" in child._option_string_actions and "--pairing-token-file" not in child._option_string_actions:
            child.add_argument("--pairing-token-file", type=Path, help="Private TCP pairing token file")
    return parser


def _normalize_globals(argv: Sequence[str]) -> list[str]:
    globals_found: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        upload_version_flag = value == "--version" and "project" in argv[:index] and "upload" in argv[:index]
        if value in {"--json", "--verbose", "--version"} and not upload_version_flag:
            globals_found.append(value)
        elif value == "--workspace":
            globals_found.append(value)
            if index + 1 < len(argv):
                index += 1
                globals_found.append(argv[index])
        elif value.startswith("--workspace="):
            globals_found.append(value)
        else:
            rest.append(value)
        index += 1
    return [*globals_found, *rest]


def _print_device_table(result: dict[str, Any], details: bool) -> None:
    fields = [
        "device_id",
        "hardware_mac",
        "online",
        "connection",
        "alias",
        "project_name",
        "app_version",
        "firmware_mode",
        "boot_id",
    ]
    if details:
        fields.extend(["endpoint", "idf_version", "session_id", "capability_names"])
    headers = [field.upper() for field in fields]

    def cell(item: dict[str, Any], field: str) -> str:
        if field == "online":
            return "yes" if item.get(field) else "no"
        if field == "alias":
            return str(item.get("alias") or item.get("suggested_alias") or "")
        if field == "capability_names":
            values = item.get(field, [])
            return ",".join(str(value) for value in values) if isinstance(values, list) else ""
        return str(item.get(field, ""))

    rows = [
        [cell(item, field) for field in fields]
        for item in result["devices"]
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(fields))
    ]
    print("  ".join(headers[index].ljust(widths[index]) for index in range(len(fields))))
    for row in rows:
        print("  ".join(row[index].ljust(widths[index]) for index in range(len(fields))))
    for endpoint in result.get("endpoints", []):
        owner = (endpoint.get("ownership") or {}).get("owner", "unclaimed")
        print(f"Endpoint: {endpoint['endpoint']}  state={endpoint.get('state', 'discovered')}  owner={owner}")


def _emit_error(error: MosaicoError, json_output: bool, verbose: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": error.category,
                    "message": str(error),
                    "exit_code": error.exit_code,
                    "details": error.details,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
    else:
        print(f"mosaico: {error}", file=sys.stderr)
        candidates = error.details.get("candidates")
        if isinstance(candidates, list) and candidates:
            print("Available device targets:", file=sys.stderr)
            for candidate in candidates:
                print(f"  {candidate}", file=sys.stderr)
        diagnostic = error.details.get("diagnostic")
        if diagnostic:
            print(diagnostic, file=sys.stderr)
        build_log_dir = error.details.get("build_log_dir")
        if build_log_dir:
            print(f"Build logs: {build_log_dir}", file=sys.stderr)
        log = error.details.get("log")
        if log and not build_log_dir:
            print(f"Log: {log}", file=sys.stderr)
        if verbose and error.details:
            print(json.dumps(error.details, ensure_ascii=False, indent=2), file=sys.stderr)


def _main(
    argv: Sequence[str] | None = None,
    *,
    tool_root: Path | None = None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    MosaicoArgumentParser.json_errors = "--json" in raw
    arguments = build_parser().parse_args(_normalize_globals(raw))
    from .session_runtime import CURRENT_SCOPE
    scope = CURRENT_SCOPE.get()
    if scope is not None:
        scope.arguments = arguments
    if sys.version_info < (3, 8):  # noqa: UP036 -- diagnose unsupported host interpreters
        message = "mosaico.py requires Python 3.8 or newer."
        if arguments.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "environment_error",
                        "message": message,
                        "exit_code": 3,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        else:
            print(f"mosaico: {message}", file=sys.stderr)
        return 3
    if arguments.command == "account":
        from .platform_account import account
        try:
            result = account(arguments)
        except MosaicoError as error:
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code
        if arguments.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    try:
        workspace = load_workspace(
            (tool_root or TOOL_ROOT).resolve(), explicit=arguments.workspace
        )
    except MosaicoError as error:
        _emit_error(error, arguments.json, arguments.verbose)
        return error.exit_code

    if arguments.command in {"game", "sim"}:
        from .app_commands import run
        try:
            return run(arguments, workspace)
        except MosaicoError as error:
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code

    if arguments.command == "project-upload":
        from .platform_upload import project_upload
        try:
            result = project_upload(arguments, RunContext(workspace, arguments.command, arguments.verbose, arguments.json))
        except MosaicoError as error:
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code
        if arguments.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        else:
            print("Draft saved: {} ({})".format(result["application_id"], result["version"]))
            print("Draft URL: " + result["draft_url"])
        return 0

    if arguments.command in {"session", "device"}:
        try:
            return _project_command(arguments, RunContext(workspace, arguments.command, arguments.verbose, arguments.json))
        except MosaicoError as error:
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code

    if arguments.command == "init":
        try:
            result = initialize_project(
                workspace, arguments.name, dry_run=arguments.dry_run
            )
        except MosaicoError as error:
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code
        if arguments.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"init: {result['status']}")
            print(f"Project: {result['project']}")
            if arguments.dry_run or arguments.verbose:
                print(f"Template: {result['template']}")
                for filename in result["files"]:
                    print(f"  {filename}")
            if arguments.dry_run:
                print("Checks passed; no files were written.")
            print(f"From workspace: {workspace.root}")
            print(result["install_command"])
        return 0

    if arguments.command == "list":
        context = RunContext(
            workspace,
            arguments.command,
            arguments.verbose,
            arguments.json,
        )
        try:
            result = list_devices(context, arguments.gateway_profile)
        except MosaicoError as error:
            error.details.setdefault("log", str(context.log_path))
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code
        if arguments.json:
            print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        else:
            _print_device_table(result, arguments.details)
        return 0

    if arguments.command == "doctor":
        try:
            result = diagnose_host(workspace)
        except MosaicoError as error:
            _emit_error(error, arguments.json, arguments.verbose)
            return error.exit_code
        if arguments.json:
            print(
                json.dumps(
                    {"ok": result["exit_code"] == 0, **result},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print_diagnosis(result)
        return int(result["exit_code"])

    context = RunContext(
        workspace,
        arguments.command,
        arguments.verbose,
        arguments.json,
    )
    if arguments.verbose and not arguments.json:
        print(f"Run log: {context.log_path}", file=sys.stderr)
    try:
        if arguments.command == "install":
            result = install(arguments, context)
        elif arguments.command == "system-update":
            result = start_system_update(arguments, context)
        elif arguments.command == "recover":
            result = recover(arguments, context)
        elif arguments.command == "enter-recovery":
            result = enter_recovery(arguments, context)
        elif arguments.command == "recovery-wifi":
            result = configure_recovery_network(arguments, context)
        elif arguments.command == "rpc":
            result = invoke_rpc(arguments, context)
        elif arguments.command == "crash":
            result = inspect_crash(arguments, context)
        elif arguments.command == "memory":
            return monitor_memory(arguments, context, arguments.json)
        elif arguments.command == "bridge-code":
            result = read_bridge_code(arguments, context)
        else:
            return monitor(arguments, context, arguments.json)
    except MosaicoError as error:
        error.details.setdefault("log", str(context.log_path))
        _emit_error(error, arguments.json, arguments.verbose)
        return error.exit_code
    except KeyboardInterrupt:
        return 0 if arguments.command in {"monitor", "memory"} else 5
    if arguments.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    else:
        status = result.get("status", "succeeded")
        print(f"{arguments.public_command}: {status}")
        if arguments.command == "bridge-code":
            authorization = result.get("bridge", {})
            print(f"Bridge pairing code: {authorization.get('code', '')}")
            print(
                "Expires in: "
                f"{int(authorization.get('expires_in_ms', 0)) // 1000}s"
            )
            print(f"Bridge website: {result.get('server_url', '')}")
        if arguments.command == "recover" and status == "dry_run":
            print("Checks passed; no firmware was built or written.")
        if arguments.verbose:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _project_command(arguments: Any, context: RunContext) -> int:
    from .errors import DeviceError
    from .gateway import ensure_gateway
    from .session_runtime import (
        CURRENT_SCOPE,
        acquire_device,
        read_pairing_token,
        request,
    )

    if arguments.command == "session" and arguments.session_action == "status":
        from .gateway_status import print_status, status
        if arguments.all_projects and arguments.project:
            raise SelectionError("--all and --project cannot be combined")
        result = status(context.workspace, arguments.project, all_projects=arguments.all_projects)
        if arguments.json:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        else:
            print_status(result)
        return 0
    start = arguments.command == "session" or getattr(arguments, "device_action", None) != "takeover-status"
    session = ensure_gateway(context, None, start=start)
    url = session.connection_args[1]
    if arguments.command == "session":
        if start and session.started_local and not (arguments.device_id or arguments.endpoint):
            try:
                acquire_device(url, arguments, context, allow_none=True)
            except MosaicoError as error:
                # Keep the foreground session available for explicit selection
                # and diagnosis. Do not retry admission after a user releases it.
                print(f"Gateway ready; automatic connection: {error}", file=sys.stderr)
                for candidate in error.details.get("candidates", []):
                    print(f"  {candidate}", file=sys.stderr)
        result = request(url, "/v1/project")
        result["running"] = True
        if arguments.json:
            print(json.dumps(result, ensure_ascii=False), flush=True)
        else:
            print(f"Project: {result['session']['project_path']}")
            print("Shared client retained; Ctrl+C releases this client only.", flush=True)
        if arguments.session_action == "run":
            print(f"Project Gateway: {url}  session={result['session']['session_id']}", file=sys.stderr)
            try:
                while True:
                    time.sleep(0.5)
                    scope = CURRENT_SCOPE.get()
                    if scope is not None:
                        for lease in scope.leases.values():
                            lease.check()
                    if scope is not None and any(process.poll() is not None for process, _, _ in scope.processes):
                        raise DeviceError("Project Gateway exited; inspect the project Gateway log")
            except KeyboardInterrupt:
                pass
        return 0
    action = arguments.device_action
    if action == "claim":
        pairing = read_pairing_token(arguments.pairing_token_file) if arguments.pairing_token_file else None
        result = request(url, "/v1/project/acquire", {
            "device_id": arguments.device_id, "endpoint": arguments.endpoint,
            "pairing_token": pairing, "auto": not (arguments.device_id or arguments.endpoint),
        }, timeout=35)
    elif action == "release":
        result = request(url, "/v1/project/release", {
            "device_id": arguments.device_id, "endpoint": arguments.endpoint,
            "auto": not (arguments.device_id or arguments.endpoint),
        })
    elif action == "reconcile":
        result = request(url, "/v1/project/reconcile", {"device_id": arguments.device_id, "endpoint": arguments.endpoint})
    elif action == "takeover-start":
        import math

        if not math.isfinite(arguments.timeout) or not 0 < arguments.timeout <= 3600:
            raise SelectionError("--timeout must be between zero and 3600 seconds")
        takeover_id = arguments.takeover_id or str(uuid.uuid4())
        context.status(f"takeover: {takeover_id}")
        try:
            result = request(url, "/v1/project/takeovers", {
                "device_id": arguments.device_id, "endpoint": arguments.endpoint,
                "takeover_id": takeover_id, "force": arguments.force, "timeout": arguments.timeout,
            }, timeout=arguments.timeout + 45)
        except DeviceError as error:
            error.details["takeover_id"] = takeover_id
            error.details["hint"] = "Query 'iris takeover status' before retrying with the same ID or using 'iris takeover resume'."
            raise
    elif action == "takeover-status":
        result = request(url, "/v1/project/takeovers/" + arguments.takeover_id)
    else:
        verb = {"takeover-abort": "abort", "takeover-resume": "resume", "takeover-reconcile": "reconcile"}[action]
        result = request(url, f"/v1/project/takeovers/{arguments.takeover_id}/{verb}", {}, timeout=35)
    print(json.dumps(result, ensure_ascii=False, indent=None if arguments.json else 2))
    return 0


def main(argv: Sequence[str] | None = None, *, tool_root: Path | None = None) -> int:
    from .session_runtime import CURRENT_SCOPE, SessionScope
    scope = SessionScope()
    token = CURRENT_SCOPE.set(scope)
    try:
        result = _main(argv, tool_root=tool_root)
    except KeyboardInterrupt:
        result = 130
    finally:
        try:
            scope.close()
        except MosaicoError as error:
            _emit_error(error, bool(getattr(scope.arguments, "json", False)),
                        bool(getattr(scope.arguments, "verbose", False)))
            result = error.exit_code
        finally:
            CURRENT_SCOPE.reset(token)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
