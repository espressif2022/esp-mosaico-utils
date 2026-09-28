from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mosaico_cli.cli import build_parser, main
from mosaico_cli.errors import SelectionError
from mosaico_cli.platform_account import account
from mosaico_cli.platform_client import (
    PlatformClient,
    PlatformError,
    credential,
    private_root,
    server_url,
    write_state,
)
from mosaico_cli.platform_manifest import (
    file_info,
    load_manifest,
    local_path,
    rewritten_readme,
    upload_version,
    validate_bundle,
)
from mosaico_cli.platform_upload import project_upload, select_target, upload_file


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Windows temporary paths may use 8.3 aliases; production resolves paths.
        self.root = Path(self.temporary.name).resolve()
        patch = mock.patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": str(self.root / "state"),
                "LOCALAPPDATA": str(self.root / "state"),
            },
            clear=True,
        )
        patch.start()
        self.addCleanup(patch.stop)
        # Keep home lookup valid after clearing the environment, including on
        # Windows, and isolate macOS state (which uses the home directory).
        home_patch = mock.patch("pathlib.Path.home", return_value=self.root)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.manifest = {
            "schema": "mosaico-ideas/project-upload/v1",
            "project": {"title": "Demo", "categories": ["sensors"], "license": "MIT"},
            "content": {
                "readme": "README.md",
                "covers": ["cover.png"],
                "attachments": ["circuit.pdf"],
            },
            "release": {"changelog": "Fixed reconnect"},
        }
        (self.root / "README.md").write_text(
            "![cover](cover.png)\n[doc][c]\n[c]: circuit.pdf\n[site](https://example.com)\n`![code](missing.png)`\n",
            encoding="utf-8",
        )
        (self.root / "cover.png").write_bytes(b"cover")
        (self.root / "circuit.pdf").write_bytes(b"pdf")
        self.write_manifest()

    def write_manifest(self):
        (self.root / "mosaico-ideas.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )

    def args(self, **updates):
        values = {
            "server": "https://ideas.test",
            "json": True,
            "create": True,
            "update": None,
            "yes": True,
            "bundle": str(self.root / "app.irisfw"),
            "skip_build": False,
            "project": str(self.root),
            "release_version": None,
        }
        values.update(updates)
        return SimpleNamespace(**values)

    def plan(self):
        return {
            "release": "1.2.3",
            "chip_id": 32,
            "flash_size": 16 * 1024 * 1024,
            "signature_verified": False,
            "key_id": None,
            "components": [
                {
                    "kind": "application",
                    "flags": 0,
                    "target_offset": 0x10000,
                    "size": 32,
                }
            ],
        }

    def test_parser_release_version_not_global(self):
        parsed = build_parser().parse_args(
            ["project", "upload", "--version", "1.2.3", "--create", "--json"]
        )
        self.assertEqual(parsed.release_version, "1.2.3")
        self.assertTrue(parsed.json)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            build_parser().parse_args(
                ["project", "upload", "--create", "--update", "abc"]
            )

    def test_account_without_workspace(self):
        with ExitStack() as stack:
            load = stack.enter_context(mock.patch("mosaico_cli.cli.load_workspace"))
            stack.enter_context(
                mock.patch(
                    "mosaico_cli.platform_account.account",
                    return_value={"status": "ok"},
                )
            )
            stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(
                main(["account", "status", "--server", "https://ideas.test", "--json"]),
                0,
            )
        load.assert_not_called()

    def test_manifest_resources_and_readme_rewrite(self):
        value = load_manifest(self.root)
        uploaded = {path: {"url": "/files/" + path.name} for path in value["files"]}
        result = rewritten_readme(value, uploaded)
        for expected in (
            "![cover](/files/cover.png)",
            "[c]: /files/circuit.pdf",
            "https://example.com",
            "`![code](missing.png)`",
        ):
            self.assertIn(expected, result)
        self.assertEqual(len(value["files"]), 2)

    def test_manifest_rejects_platform_identity(self):
        self.manifest["project"]["application_id"] = "private"
        self.write_manifest()
        with self.assertRaises(SelectionError):
            load_manifest(self.root)

    def test_manifest_rejects_duplicates(self):
        self.manifest["content"]["resources"] = ["cover.png"]
        self.write_manifest()
        with self.assertRaises(SelectionError):
            load_manifest(self.root)

    def test_resource_paths(self):
        for path in ("../secret", "/etc/passwd", "C:\\secret", "a/../cover.png"):
            with self.subTest(path=path), self.assertRaises(SelectionError):
                local_path(self.root, path)
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "secret.txt"
            target.write_text("outside project", encoding="utf-8")
            (self.root / "escape").symlink_to(target)
            with self.assertRaises(SelectionError):
                local_path(self.root, "escape")

    def test_readme_path_escape(self):
        for destination in ("../secret", "/etc/passwd", "%2e%2e/secret"):
            (self.root / "README.md").write_text("[x](" + destination + ")")
            with ExitStack() as stack:
                stack.enter_context(self.subTest(destination=destination))
                stack.enter_context(self.assertRaises(SelectionError))
                load_manifest(self.root)

    def test_bad_manifest_category(self):
        self.manifest["project"]["categories"] = ["unknown"]
        self.write_manifest()
        with self.assertRaises(SelectionError):
            load_manifest(self.root)

    def test_versions_are_exact(self):
        self.assertEqual(
            upload_version({"release": "1.2-beta+3"}, "1.2-beta+3", "1.2-beta+3"),
            "1.2-beta+3",
        )
        for plan, explicit, build in (
            ({}, None, None),
            ({"release": "1"}, "2", None),
            ({}, " 1", None),
            ({}, "a/b", None),
        ):
            with ExitStack() as stack:
                stack.enter_context(self.subTest(plan=plan, explicit=explicit))
                stack.enter_context(self.assertRaises(SelectionError))
                upload_version(plan, explicit, build)

    def test_bundle_policy(self):
        validate_bundle(self.plan())
        for change in (
            {"key_id": "signed"},
            {"components": []},
            {"flash_size": 1},
            {"chip_id": 9},
            {"flash_size": 64 * 1024 * 1024},
        ):
            with self.subTest(change=change), self.assertRaises(SelectionError):
                validate_bundle({**self.plan(), **change})

    def test_explicit_target_required(self):
        with self.assertRaises(SelectionError):
            select_target(self.args(create=False), mock.Mock(), "1")

    def test_update_ownership_and_blocked_state(self):
        client = mock.Mock()
        client.request.return_value = [
            {"id": "mine", "can_update": False, "blocked_reason": "pending_review"}
        ]
        for identifier in ("other", "mine"):
            with self.subTest(identifier=identifier), self.assertRaises(SelectionError):
                select_target(self.args(create=False, update=identifier), client, "1")

    def test_existing_draft_requires_confirmation(self):
        client = mock.Mock()
        client.request.return_value = [
            {
                "id": "mine",
                "title": "App",
                "current_version": "1",
                "can_update": True,
                "draft_revision_id": "r",
            }
        ]
        with self.assertRaises(SelectionError):
            select_target(
                self.args(create=False, update="mine", yes=False), client, "2"
            )
        self.assertEqual(
            select_target(self.args(create=False, update="mine"), client, "2")["id"],
            "mine",
        )

    def test_interactive_create_and_update(self):
        client = mock.Mock()
        client.request.return_value = [
            {
                "id": "mine",
                "title": "App",
                "current_version": "1",
                "visibility": "draft",
                "can_update": True,
                "draft_revision_id": "r",
            }
        ]
        with ExitStack() as stack:
            stack.enter_context(mock.patch("sys.stdin.isatty", return_value=True))
            stack.enter_context(mock.patch("builtins.input", side_effect=["c", "y"]))
            self.assertIsNone(
                select_target(
                    self.args(create=False, json=False, yes=False), client, "2"
                )
            )
        with ExitStack() as stack:
            stack.enter_context(mock.patch("sys.stdin.isatty", return_value=True))
            stack.enter_context(
                mock.patch("builtins.input", side_effect=["u", "1", "y"])
            )
            stack.enter_context(redirect_stderr(io.StringIO()))
            self.assertEqual(
                select_target(
                    self.args(create=False, json=False, yes=False), client, "2"
                )["id"],
                "mine",
            )

    def test_private_state_is_isolated_on_all_host_platforms(self):
        for platform in ("windows", "macos", "linux"):
            with self.subTest(platform=platform), mock.patch(
                "mosaico_cli.host.host_platform", return_value=platform
            ):
                path = private_root("https://ideas.test")
                self.assertTrue(path.is_dir())
                path.resolve().relative_to(self.root)

    def test_private_credentials_and_environment_priority(self):
        path = private_root("https://ideas.test") / "credential.json"
        write_state(path, {"access_token": "stored"})
        self.assertEqual(credential("https://ideas.test"), "stored")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with mock.patch.dict(os.environ, {"MAKER_SPARK_TOKEN": "environment"}):
            self.assertEqual(credential("https://ideas.test"), "environment")

    def test_private_state_moves_credentials_and_upload_ledger_once(self):
        server = "https://ideas.test"
        state = self.root / "private"
        legacy = state / "maker-spark" / hashlib.sha256(server.encode()).hexdigest()
        legacy.mkdir(parents=True)
        write_state(legacy / "credential.json", {"access_token": "existing"})
        write_state(legacy / "upload-retry.json", {"key": "original-idempotency-key"})
        with mock.patch("mosaico_cli.platform_client.state_root", return_value=state):
            self.assertEqual(credential(server), "existing")
            current = private_root(server)
            self.assertEqual(current.parent.name, "mosaico-ideas")
            self.assertFalse(legacy.exists())
            self.assertEqual(json.loads((current / "upload-retry.json").read_text()),
                             {"key": "original-idempotency-key"})
            self.assertEqual(private_root(server), current)
            if os.name != "nt":
                self.assertEqual(current.stat().st_mode & 0o777, 0o700)
                self.assertEqual((current / "credential.json").stat().st_mode & 0o777, 0o600)

    def test_existing_private_state_is_not_overwritten_by_legacy_state(self):
        server = "https://ideas.test"
        state = self.root / "private"
        legacy = state / "maker-spark" / hashlib.sha256(server.encode()).hexdigest()
        with mock.patch("mosaico_cli.platform_client.state_root", return_value=state):
            current = private_root(server)
            write_state(current / "credential.json", {"access_token": "current"})
            legacy.mkdir(parents=True)
            write_state(legacy / "credential.json", {"access_token": "stale"})
            self.assertEqual(credential(server), "current")
            self.assertTrue((legacy / "credential.json").exists())

    def test_server_environment_name_is_unchanged(self):
        with mock.patch.dict(os.environ, {"MAKER_SPARK_SERVER": "https://ideas.test"}):
            self.assertEqual(server_url(None), "https://ideas.test")

    def test_public_upload_help_uses_new_manifest_name(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as error:
            build_parser().parse_args(["project", "upload", "--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertIn("mosaico-ideas.json", output.getvalue())

    def test_unknown_manifest_schema_is_rejected(self):
        self.manifest["schema"] = "unsupported/project-upload/v1"
        self.write_manifest()
        with self.assertRaisesRegex(SelectionError, "Unsupported mosaico-ideas.json schema"):
            load_manifest(self.root)

    def test_server_validation(self):
        for value in (
            "http://example.com",
            "https://user:pass@example.com",
            "https://example.com/?token=secret",
            "https://example.com/path",
        ):
            with self.subTest(value=value), self.assertRaises(SelectionError):
                server_url(value)
        self.assertEqual(server_url("http://localhost:3000/"), "http://localhost:3000")

    def test_transport_sanitizes_errors(self):
        client = PlatformClient("https://ideas.test", "secret-token")
        client.opener.open = mock.Mock(
            side_effect=urllib.error.HTTPError(
                "https://signed?secret",
                403,
                "secret",
                {},
                io.BytesIO(
                    json.dumps(
                        {
                            "code": "DENIED",
                            "message": "secret-token",
                            "request_id": "req-123",
                        }
                    ).encode()
                ),
            )
        )
        with self.assertRaises(PlatformError) as raised:
            client.request("POST", "/something")
        self.assertNotIn("secret", str(raised.exception))
        self.assertIn("req-123", str(raised.exception))

    def test_storage_put_does_not_leak_token_and_412_completes(self):
        client = PlatformClient("https://ideas.test", "private-token")
        client.opener.open = mock.Mock(
            side_effect=urllib.error.HTTPError(
                "https://store.test?secret", 412, "exists", {}, io.BytesIO(b"")
            )
        )
        client.put_object(
            {
                "upload_url": "https://store.test/key?secret",
                "headers": {"If-None-Match": "*"},
            },
            b"data",
        )
        request = client.opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))

    def test_login_poll_and_json_do_not_disclose_token(self):
        client = mock.Mock()
        client.request.side_effect = [
            {
                "verification_uri": "https://ideas.test/auth/device",
                "user_code": "1234",
                "device_code": "device-secret",
                "expires_in": 600,
                "interval": 1,
            },
            PlatformError("AUTHORIZATION_PENDING", 400),
            {"access_token": "token-secret", "expires_in": 3600},
        ]
        stderr = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch(
                    "mosaico_cli.platform_account.PlatformClient", return_value=client
                )
            )
            stack.enter_context(mock.patch("mosaico_cli.platform_account.time.sleep"))
            stack.enter_context(redirect_stderr(stderr))
            result = account(self.args(account_action="login", open_browser=False))
        self.assertNotIn("secret", json.dumps(result) + stderr.getvalue())
        self.assertIn("1234", stderr.getvalue())
        self.assertEqual(credential("https://ideas.test"), "token-secret")

    def test_upload_orchestration_and_finalize_resume(self):
        (self.root / "app.irisfw").write_bytes(b"bundle")
        client, calls, lost = mock.Mock(), [], [True]

        def request(method, path, body=None, **kwargs):
            calls.append((method, path, body, kwargs))
            if path == "/firmware-projects":
                return {"id": "project-id", "version": 1}
            if path.endswith("/draft"):
                return {
                    "id": "project-id",
                    "article_id": "article-id",
                    "revision_id": "revision-id",
                    "version": 2,
                    "revision_version": 3,
                }
            if path.endswith(("/iris-uploads", "/uploads")):
                return {
                    "id": body["filename"],
                    "upload_url": "https://store.test",
                    "headers": {},
                }
            if path.endswith("/complete"):
                name = path.split("/")[-2]
                return {
                    "id": "asset-" + name,
                    "filename": name,
                    "size": 3,
                    "media_type": "image/png",
                    "url": "/files/" + name,
                }
            if path.endswith("/iris-version"):
                if lost[0]:
                    lost[0] = False
                    raise PlatformError("NETWORK_ERROR")
                return {
                    "application_id": "project-id",
                    "revision_id": "revision-id",
                    "version": "1.2.3",
                    "status": "draft",
                    "draft_url": "/firmware/project-id/edit",
                }
            raise AssertionError(path)

        client.request.side_effect = request
        context = SimpleNamespace(workspace=SimpleNamespace())
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(os.environ, {"MAKER_SPARK_TOKEN": "secret"})
            )
            stack.enter_context(
                mock.patch(
                    "mosaico_cli.platform_upload.PlatformClient", return_value=client
                )
            )
            stack.enter_context(
                mock.patch(
                    "mosaico_cli.platform_upload.resolve_project",
                    return_value=self.root,
                )
            )
            stack.enter_context(
                mock.patch(
                    "mosaico_cli.platform_upload.inspect_bundle_plan",
                    return_value=self.plan(),
                )
            )
            build = stack.enter_context(
                mock.patch("mosaico_cli.platform_upload.run_idf_target")
            )
            with self.assertRaises(PlatformError):
                project_upload(self.args(), context)
            result = project_upload(self.args(), context)
        self.assertEqual(result["status"], "draft")
        build.assert_not_called()
        self.assertEqual(
            sum(path == "/firmware-projects" for _, path, _, _ in calls), 1
        )
        finals = [call for call in calls if call[1].endswith("/iris-version")]
        self.assertEqual(finals[0][3]["key"], finals[1][3]["key"])
        self.assertEqual(
            finals[-1][2]["attachments"][0]["upload_id"], "asset-circuit.pdf"
        )
        self.assertIn("/files/cover.png", finals[-1][2]["description"])
        self.assertFalse(
            any("submit" in path or "publish" in path for _, path, _, _ in calls)
        )

    def test_upload_expiry_renews_key(self):
        client = mock.Mock()
        client.request.side_effect = [
            PlatformError("UPLOAD_EXPIRED", 410),
            {"id": "session", "upload_url": "https://store.test", "headers": {}},
            {"id": "asset"},
        ]
        result = upload_file(
            client,
            self.root / "cover.png",
            file_info(self.root / "cover.png"),
            "/uploads",
            "/files",
            "unique",
            {},
            self.root / "ledger.json",
        )
        self.assertEqual(result["id"], "asset")
        self.assertEqual(client.request.call_args_list[1].kwargs["key"], "unique:1")

    def test_python38_syntax(self):
        tools = Path(__file__).resolve().parents[1] / "tools/mosaico_cli"
        for path in tools.glob("platform_*.py"):
            ast.parse(path.read_text(), feature_version=(3, 8))

    def test_completed_session_replay_skips_storage(self):
        client = mock.Mock()
        client.request.return_value = {"id": "done", "completed_asset": {"id": "asset"}}
        result = upload_file(
            client,
            self.root / "cover.png",
            file_info(self.root / "cover.png"),
            "/uploads",
            "/files",
            "unique",
            {},
            self.root / "ledger.json",
        )
        self.assertEqual(result["id"], "asset")
        client.put_object.assert_not_called()
        self.assertEqual(client.request.call_count, 1)

    def test_multipart_fallback_uses_idempotency(self):
        client = mock.Mock()
        client.request.side_effect = PlatformError("DIRECT_UPLOAD_UNAVAILABLE", 503)
        client.multipart.return_value = {"id": "asset"}
        upload_file(
            client,
            self.root / "cover.png",
            file_info(self.root / "cover.png"),
            "/uploads",
            "/files",
            "unique",
            {},
            self.root / "ledger.json",
        )
        self.assertEqual(client.multipart.call_args.kwargs["key"], "unique:multipart")

    def test_login_rejects_malformed_response(self):
        client = mock.Mock()
        for response in (
            {},
            {
                "verification_uri": "http://remote.test/device",
                "device_code": "secret",
                "user_code": "1234",
                "expires_in": 600,
            },
        ):
            client.request.return_value = response
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch(
                        "mosaico_cli.platform_account.PlatformClient",
                        return_value=client,
                    )
                )
                stack.enter_context(self.assertRaises(PlatformError))
                account(self.args(account_action="login", open_browser=False))

    def test_unreferenced_resource_rejected(self):
        (self.root / "orphan.txt").write_text("resource")
        self.manifest["content"]["resources"] = ["orphan.txt"]
        self.write_manifest()
        with self.assertRaisesRegex(SelectionError, "linked from README"):
            load_manifest(self.root)

    def test_schema_fixtures(self):
        fixture_root = Path(__file__).parent
        self.manifest = json.loads(
            (fixture_root / "mosaico-ideas.valid.json").read_text()
        )
        self.write_manifest()
        self.assertEqual(
            load_manifest(self.root)["project"]["title"], "Temperature sensor"
        )
        self.manifest = json.loads(
            (fixture_root / "mosaico-ideas.invalid.json").read_text()
        )
        self.write_manifest()
        with self.assertRaises(SelectionError):
            load_manifest(self.root)
        schema = json.loads(
            (fixture_root.parent / "mosaico-ideas.schema.json").read_text()
        )
        self.assertEqual(
            schema["properties"]["schema"]["const"], "mosaico-ideas/project-upload/v1"
        )

    def test_build_and_skip_build_version_source(self):
        (self.root / "build").mkdir()
        (self.root / "build/project_description.json").write_text(
            json.dumps({"project_name": "demo", "project_version": "1.2.3"})
        )
        (self.root / "build/demo-system-update.irisfw").write_bytes(b"bundle")
        for skip_build in (False, True):
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch.dict(os.environ, {"MAKER_SPARK_TOKEN": "secret"})
                )
                stack.enter_context(
                    mock.patch(
                        "mosaico_cli.platform_upload.resolve_project",
                        return_value=self.root,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "mosaico_cli.platform_upload.inspect_bundle_plan",
                        return_value=self.plan(),
                    )
                )
                build = stack.enter_context(
                    mock.patch("mosaico_cli.platform_upload.run_idf_target")
                )
                gateway = stack.enter_context(
                    mock.patch("mosaico_cli.gateway.ensure_gateway")
                )
                stack.enter_context(
                    mock.patch(
                        "mosaico_cli.platform_upload.ensure_iris_tools",
                        return_value=(Path("python"), Path("iris")),
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "mosaico_cli.platform_upload.resolve_idf_path",
                        return_value=self.root,
                    )
                )
                select = stack.enter_context(
                    mock.patch(
                        "mosaico_cli.platform_upload.select_target",
                        side_effect=SelectionError("stop"),
                    )
                )
                with self.assertRaisesRegex(SelectionError, "stop"):
                    project_upload(
                        self.args(bundle=None, skip_build=skip_build),
                        SimpleNamespace(workspace=SimpleNamespace()),
                    )
                self.assertEqual(build.call_count, 0 if skip_build else 1)
                if not skip_build:
                    self.assertEqual(
                        build.call_args.kwargs["target"], "system-update-bundle"
                    )
                    self.assertEqual(build.call_args.kwargs["project"], self.root)
                    self.assertEqual(
                        build.call_args.kwargs["build_dir"], self.root / "build"
                    )
                gateway.assert_not_called()
                self.assertEqual(select.call_args.args[2], "1.2.3")

    def test_skip_build_version_mismatch_fails_before_network(self):
        (self.root / "build").mkdir()
        (self.root / "build/project_description.json").write_text(
            json.dumps({"project_name": "demo", "project_version": "9.9.9"})
        )
        (self.root / "build/demo-system-update.irisfw").write_bytes(b"bundle")
        with mock.patch.dict(os.environ, {"MAKER_SPARK_TOKEN": "secret"}), mock.patch(
            "mosaico_cli.platform_upload.resolve_project", return_value=self.root
        ), mock.patch(
            "mosaico_cli.platform_upload.inspect_bundle_plan", return_value=self.plan()
        ), mock.patch(
            "mosaico_cli.platform_upload.PlatformClient"
        ) as client, mock.patch(
            "mosaico_cli.platform_upload.run_idf_target"
        ) as build, mock.patch("mosaico_cli.gateway.ensure_gateway") as gateway:
            with self.assertRaisesRegex(SelectionError, "must match exactly"):
                project_upload(
                    self.args(bundle=None, skip_build=True),
                    SimpleNamespace(workspace=SimpleNamespace()),
                )
            build.assert_not_called()
            gateway.assert_not_called()
            client.return_value.request.assert_not_called()

    def test_json_error_retains_request_id_without_credentials(self):
        stderr = io.StringIO()
        with mock.patch(
            "mosaico_cli.platform_account.account",
            side_effect=PlatformError("DENIED", 403, "request-123"),
        ), redirect_stderr(stderr):
            status = main(
                ["account", "status", "--server", "https://ideas.test", "--json"]
            )
        result = json.loads(stderr.getvalue())
        self.assertEqual(status, 5)
        self.assertEqual(result["error"], "platform_error")
        self.assertEqual(result["details"]["request_id"], "request-123")
        self.assertNotIn("Authorization", stderr.getvalue())
        self.assertNotIn("access_token", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
