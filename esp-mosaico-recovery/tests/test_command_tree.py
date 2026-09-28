"""Public command routing and compatibility with existing automation."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from mosaico_cli.cli import build_parser, main


@pytest.mark.parametrize("public,legacy,options", [
    ("project init", "init", ["my_app"]),
    ("iris run", "session run", []),
    ("iris status", "session status", []),
    ("iris list", "list", []),
    ("iris claim", "device claim", ["--endpoint", "usb:location=1-2"]),
    ("iris release", "device release", ["--device-id", "board"]),
    ("iris reconcile", "device reconcile", ["--device-id", "board"]),
    ("iris logs", "monitor", ["--snapshot"]),
    ("iris memory", "memory", ["--follow"]),
    ("iris crash", "crash", ["--archive"]),
    ("iris rpc", "rpc", ["1", "2", "--payload", "game"]),
    ("iris app-update", "install", ["--project", "projects/app", "--skip-build"]),
    ("iris system-update", "system-update", ["--bundle", "release.irisfw"]),
    ("iris test enter-recovery", "enter-recovery", []),
    ("iris test recovery-wifi", "recovery-wifi", ["--ssid", "test-network"]),
    ("iris test bridge-code", "bridge-code", []),
])
def test_public_command_preserves_operation_arguments(public, legacy, options):
    parser = build_parser()
    current = parser.parse_args([*public.split(), *options, "--json"])
    previous = parser.parse_args([*legacy.split(), *options, "--json"])
    assert vars(current) == vars(previous)
    assert current.public_command == public
    assert current.json is True


@pytest.mark.parametrize("action,options", [
    ("start", ["--device-id", "board", "--force", "--timeout", "30"]),
    ("start", ["--endpoint", "usb:location=1-2"]),
    *[(action, []) for action in ("status", "resume", "abort", "reconcile")],
])
def test_takeover_preserves_record_and_receiving_project_arguments(action, options):
    takeover_id = "34316aaf-5c53-49c0-9d71-44ad598f20ce"
    args = build_parser().parse_args([
        "iris", "takeover", action, *options, "--takeover-id", takeover_id,
        "--project", "projects/receiver", "--json",
    ])
    assert args.command == "device"
    assert args.device_action == "takeover-" + action
    assert args.public_command == "iris takeover " + action
    assert args.takeover_id == takeover_id
    assert args.project == "projects/receiver"
    assert args.json is True
    if action == "start":
        if "--device-id" in options:
            assert args.device_id == "board"
            assert args.force is True
            assert args.timeout == 30
        else:
            assert args.endpoint == "usb:location=1-2"
            assert args.force is False
            assert args.timeout == 120


def test_help_exposes_only_the_public_root_groups():
    help_text = build_parser().format_help()
    assert "{project,game,account,iris,doctor,recover}" in help_text
    assert "session" not in help_text
    assert "install" not in help_text
    assert "monitor" not in help_text


def test_command_words_in_values_are_not_rewritten():
    args = build_parser().parse_args([
        "--workspace", "install", "iris", "rpc", "1", "2",
        "--payload", "device transfer", "--project", "monitor",
    ])
    assert args.workspace == "install"
    assert args.project == "monitor"
    assert args.payload == "device transfer"
    assert args.command == "rpc"


@pytest.mark.parametrize("argv", [
    ["iris"], ["project"], ["iris", "takeover"], ["iris", "test"],
])
def test_incomplete_groups_fail_at_parse_time(argv):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2


def test_json_error_for_unknown_nested_command(capsys):
    import json
    from mosaico_cli.cli import MosaicoArgumentParser

    try:
        with pytest.raises(SystemExit) as error:
            main(["iris", "unknown", "--json"])
        assert error.value.code == 2
        result = json.loads(capsys.readouterr().err)
        assert result["error"] == "selection_error"
    finally:
        MosaicoArgumentParser.json_errors = False
