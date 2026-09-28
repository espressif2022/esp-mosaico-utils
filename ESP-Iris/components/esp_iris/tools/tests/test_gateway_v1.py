from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import iris_gateway.cli as cli_module
import iris_gateway.crashes as crashes_module
from iris_gateway import __version__
from iris_gateway.cli import _client_ssl, _listen_is_loopback, build_parser
from iris_gateway.demo import DemoHub
from iris_gateway.gateway import GATEWAY_CLIENT_MAX_SIZE, GatewayService, create_app
from iris_gateway.security import Actor
from iris_gateway.store import GatewayStore
from iris_gateway.system_update import SYSTEM_UPDATE_SCHEMA, build_system_update_bundle


def test_cli_reports_product_version(capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"esp_iris.py {__version__}"


def _firmware_bundle() -> tuple[bytes, bytes, bytes]:
    elf = b"test-application-elf"
    image = bytearray(256)
    image[0] = 0xE9
    image[1] = 1
    struct.pack_into("<H", image, 12, 0x20)
    descriptor = 32
    struct.pack_into("<I", image, descriptor, 0xABCD5432)
    image[descriptor + 16 : descriptor + 21] = b"5.0.0"
    image[descriptor + 48 : descriptor + 65] = b"esp-iris-template"
    image[descriptor + 144 : descriptor + 176] = hashlib.sha256(elf).digest()
    return bytes(image), elf, b"memory map"


def test_gateway_client_max_size_is_one_gibibyte(tmp_path) -> None:
    service = GatewayService(GatewayStore(tmp_path), instance_id="test", demo=True)
    app = create_app(service)

    assert GATEWAY_CLIENT_MAX_SIZE == 1024**3
    assert app._client_max_size == GATEWAY_CLIENT_MAX_SIZE


def test_system_inventory_endpoint_returns_live_layout_hash(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            response = await client.get(
                "/v1/devices/demo-a1b2c3d4/system-inventory"
            )
            assert response.status == 200
            body = await response.json()
            assert body["device_id"] == "demo-a1b2c3d4"
            assert body["inventory"]["partition_table_sha256"] == "00" * 32

            spec = await client.get("/v1/openapi.json")
            assert (
                "/v1/devices/{device_id}/system-inventory"
                in (await spec.json())["paths"]
            )
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_system_inventory_ctl_command_parses_device() -> None:
    args = build_parser().parse_args(
        ["ctl", "system-inventory", "device-a"]
    )
    assert args.ctl_command == "system-inventory"
    assert args.device == "device-a"


def test_ota_archives_complete_bundle_and_returns_queryable_operation(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            binary, elf, map_data = _firmware_bundle()
            form = FormData()
            form.add_field("bin", binary, filename="app.bin")
            form.add_field("elf", elf, filename="app.elf")
            form.add_field("map", map_data, filename="app.map")
            uploaded = await client.post("/v1/firmware-artifacts", data=form)
            assert uploaded.status == 201
            artifact = (await uploaded.json())["artifact"]
            assert artifact["artifact_id"] == hashlib.sha256(elf).hexdigest()

            preconditions = {"recovery_version": (await hub.status("demo-a1b2c3d4"))["app_version"],
                             "partition_table_sha256": "00" * 32}
            accepted = await client.post(
                "/v1/devices/demo-a1b2c3d4/ota",
                json={
                    "artifact_id": artifact["artifact_id"],
                    "execution_mode": "recovery",
                    "compatibility": {"chip_target": "esp32s31"},
                    "preconditions": preconditions,
                },
                headers={"X-Operation-ID": "ota-background"},
            )
            assert accepted.status == 202
            assert (await accepted.json())["status_url"].endswith("ota-background")
            operation = None
            for _ in range(100):
                response = await client.get("/v1/operations/ota-background")
                operation = await response.json()
                if operation["status"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            assert operation is not None
            assert operation["status"] == "succeeded", json.dumps(operation)
            assert operation["params"]["compatibility"] == {"chip_target": "esp32s31"}
            assert operation["params"]["preconditions"] == preconditions
            evidence = operation["result"]["recovery"]
            assert evidence["device_id"] == "demo-a1b2c3d4"
            assert evidence["recovery_version"] == preconditions["recovery_version"]
            assert evidence["partition_table_sha256"] == preconditions["partition_table_sha256"]
            changed_precondition = await client.post(
                "/v1/devices/demo-a1b2c3d4/ota",
                json={"artifact_id": artifact["artifact_id"], "execution_mode": "recovery",
                      "compatibility": {"chip_target": "esp32s31"},
                      "preconditions": {**preconditions, "partition_table_sha256": "ff" * 32}},
                headers={"X-Operation-ID": "ota-background"},
            )
            assert changed_precondition.status == 409
            conflict = await client.post(
                "/v1/devices/demo-a1b2c3d4/ota",
                json={"artifact_id": artifact["artifact_id"], "execution_mode": "recovery",
                      "compatibility": {"chip_target": "esp32s31", "board_id": "another"}},
                headers={"X-Operation-ID": "ota-background"},
            )
            assert conflict.status == 409
            assert operation["result"]["execution_mode"] == "recovery"
            assert operation["result"]["validation"]["mode"] == "elf_sha256"
            assert "preserved_coredump" not in operation["result"]

            invalid_validation = await client.post(
                "/v1/devices/demo-a1b2c3d4/ota",
                json={
                    "artifact_id": artifact["artifact_id"],
                    "validation_mode": "unsupported",
                },
            )
            assert invalid_validation.status == 400
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())








def test_unsigned_system_update_closes_actual_inventory_loop(tmp_path) -> None:
    async def scenario() -> None:
        components = tmp_path / "components"
        components.mkdir()
        (components / "partition-table.bin").write_bytes(b"table-v2")
        manifest = {
            "schema": SYSTEM_UPDATE_SCHEMA,
            "target": {"chip_id": 0x20, "flash_size": 16 * 1024 * 1024},
            "target_layout_sha256": "00" * 32,
            "components": [
                {
                    "id": 1,
                    "kind": "partition_table",
                    "target_offset": 0x8000,
                    "file": "partition-table.bin",
                }
            ],
        }
        archive_path = build_system_update_bundle(
            tmp_path / "release.irisfw",
            manifest,
            components,
        )
        store = GatewayStore(tmp_path / "state")
        service = GatewayService(
            store,
            instance_id="test",
            demo=True,
        )
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            operation_id = str(uuid.uuid4())
            accepted = await client.post(
                "/v1/devices/demo-a1b2c3d4/system-update",
                data=archive_path.read_bytes(),
                headers={
                    "Content-Type": "application/vnd.esp-iris.system-update+zip",
                    "X-Operation-ID": operation_id,
                    "X-Iris-Compatibility": json.dumps({"chip_target": "esp32s31"}),
                },
            )
            assert accepted.status == 202
            assert (await accepted.json())["bundle"]["signature_verified"] is False
            operation = None
            for _ in range(200):
                response = await client.get(f"/v1/operations/{operation_id}")
                operation = await response.json()
                if operation["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert operation is not None
            assert operation["status"] == "succeeded", json.dumps(operation)
            assert operation["params"]["compatibility"] == {"chip_target": "esp32s31"}
            assert operation["result"]["validated"] is True
            assert (
                operation["result"]["target_inventory"][
                    "partition_table_sha256"
                ]
                == operation["result"]["target_layout_sha256"]
            )
            saved = list((store.artifacts_dir / "demo-a1b2c3d4").glob("*.irisfw"))
            assert len(saved) == 1
            repeated = await client.post(
                "/v1/devices/demo-a1b2c3d4/system-update",
                data=archive_path.read_bytes(),
                headers={"Content-Type": "application/vnd.esp-iris.system-update+zip",
                         "X-Operation-ID": operation_id,
                         "X-Iris-Compatibility": json.dumps({"chip_target": "esp32s31"})},
            )
            assert repeated.status == 202
            assert (await repeated.json())["accepted"] is False
            assert len(store.operations()) == 1
            conflict = await client.post(
                "/v1/devices/demo-a1b2c3d4/system-update",
                data=archive_path.read_bytes(),
                headers={"Content-Type": "application/vnd.esp-iris.system-update+zip",
                         "X-Operation-ID": operation_id,
                         "X-Iris-Compatibility": json.dumps({"board_id": "different"})},
            )
            assert conflict.status == 409
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_recovery_self_update_closes_recovery_identity_loop(tmp_path) -> None:
    async def scenario() -> None:
        components = tmp_path / "recovery-components"
        components.mkdir()
        firmware, _, _ = _firmware_bundle()
        (components / "factory.bin").write_bytes(firmware)
        manifest = {
            "schema": SYSTEM_UPDATE_SCHEMA,
            "minimum_recovery_version": "1.0.0",
            "target": {"chip_id": 0x20, "flash_size": 16 * 1024 * 1024},
            "target_layout_sha256": "11" * 32,
            "components": [
                {
                    "id": 1,
                    "kind": "recovery",
                    "target_offset": 0x20000,
                    "size": 0x2000,
                    "file": "factory.bin",
                },
            ],
        }
        archive_path = build_system_update_bundle(
            tmp_path / "recovery.irisfw", manifest, components
        )
        store = GatewayStore(tmp_path / "state")
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        hub.get("demo-a1b2c3d4")["partition_table_sha256"] = "11" * 32
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            operation_id = str(uuid.uuid4())
            accepted = await client.post(
                "/v1/devices/demo-a1b2c3d4/system-update",
                data=archive_path.read_bytes(),
                headers={
                    "Content-Type": "application/vnd.esp-iris.system-update+zip",
                    "X-Operation-ID": operation_id,
                    "X-Iris-Compatibility": json.dumps(
                        {"chip_target": "esp32s31"}
                    ),
                },
            )
            assert accepted.status == 202
            operation = None
            for _ in range(200):
                response = await client.get(f"/v1/operations/{operation_id}")
                operation = await response.json()
                if operation["status"] in {"succeeded", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert operation is not None
            assert operation["status"] == "succeeded", json.dumps(operation)
            assert operation["result"]["recovery_validation"]["mode"] == (
                "elf_sha256"
            )
            status = await hub.status("demo-a1b2c3d4")
            assert status["firmware_mode"] == "recovery"
            assert status["project_name"] == "esp-iris-template"
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_authenticated_gateway_mode_and_idempotent_operations(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(
            store, instance_id="test", demo=True, require_local_auth=True
        )
        service.auth.set_initial_password("dev-password")
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            unauthorized = await client.get("/v1/devices")
            assert unauthorized.status == 401
            login = await client.post("/v1/auth/login", json={"password": "dev-password"})
            assert login.status == 200
            session = login.cookies["esp_iris_session"].value
            headers = {"Cookie": f"esp_iris_session={session}"}

            devices = await client.get("/v1/devices", headers=headers)
            body = await devices.json()
            assert len(body["devices"]) == 3
            assert body["demo"] is True

            status = await client.get("/v1/devices/demo-a1b2c3d4", headers=headers)
            assert (await status.json())["stale"] is False

            console = await client.post(
                "/v1/devices/demo-a1b2c3d4/console",
                json={"line": "help"},
                headers=headers,
            )
            console_body = await console.json()
            assert console.status == 200
            assert console_body["console"] == {"job_id": 1, "accepted": True}
            assert console_body["operation"]["params"]["command"] == "help"
            assert "line" not in console_body["operation"]["params"]

            invalid_console = await client.post(
                "/v1/devices/demo-a1b2c3d4/console",
                json={"line": "help\nrestart"},
                headers=headers,
            )
            assert invalid_console.status == 400

            operation_headers = {**headers, "X-Operation-ID": "stable-op"}
            payload = {"service_id": 1, "method_id": 1, "payload_text": "hi"}
            first = await client.post(
                "/v1/devices/demo-a1b2c3d4/rpc/raw",
                json=payload,
                headers=operation_headers,
            )
            assert (await first.json())["operation"]["status"] == "succeeded"
            second = await client.post(
                "/v1/devices/demo-a1b2c3d4/rpc/raw",
                json=payload,
                headers=operation_headers,
            )
            assert (await second.json())["idempotent_reuse"] is True

            screenshot = await client.post(
                "/v1/devices/demo-a1b2c3d4/screenshot",
                json={"width": 160, "height": 90},
                headers=headers,
            )
            assert screenshot.content_type == "image/png"
            assert (await screenshot.read()).startswith(b"\x89PNG")

            mirror = await client.post(
                "/v1/devices/demo-a1b2c3d4/mirror/start",
                json={"channel": "screen", "fps": 5, "description": {}},
                headers=headers,
            )
            assert mirror.status == 200
            repeated_mirror = await client.post(
                "/v1/devices/demo-a1b2c3d4/mirror/start",
                json={"channel": "screen", "fps": 5, "description": {}},
                headers=headers,
            )
            assert repeated_mirror.status == 200
            assert (await repeated_mirror.json())["mirror"]["reused"] is True
            mirrored_screenshot = await client.post(
                "/v1/devices/demo-a1b2c3d4/screenshot",
                json={},
                headers=headers,
            )
            media = json.loads(mirrored_screenshot.headers["X-ESP-Iris-Media"])
            assert media["mirror_reused"] == 1
            stopped = await client.post(
                "/v1/devices/demo-a1b2c3d4/mirror/stop",
                json={"channel": "screen"},
                headers=headers,
            )
            assert stopped.status == 200

            gesture = await client.post(
                "/v1/devices/demo-a1b2c3d4/input",
                json={
                    "begin": {"x": 0, "y": 0},
                    "moves": [{"x": 5000, "y": 5000}],
                    "end": {"x": 10000, "y": 10000},
                },
                headers=headers,
            )
            assert (await gesture.json())["input"]["points"] == 3

            restart = await client.post(
                "/v1/devices/demo-a1b2c3d4/restart",
                json={"delay_ms": 250},
                headers=headers,
            )
            assert (await restart.json())["restart"]["reconnected"] is True

            factory = await client.post(
                "/v1/devices/demo-a1b2c3d4/factory-recovery",
                headers=headers,
            )
            assert (await factory.json())["recovery"]["target"] == "factory_recovery"

            observe = await client.put("/v1/mode", json={"mode": "observe"}, headers=headers)
            assert (await observe.json())["mode"] == "observe"
            cached = await client.get("/v1/devices/demo-a1b2c3d4", headers=headers)
            assert (await cached.json())["stale"] is True
            blocked = await client.post(
                "/v1/devices/demo-a1b2c3d4/restart",
                json={"delay_ms": 250},
                headers=headers,
            )
            assert blocked.status == 423
            assert (await blocked.json())["error"]["code"] == "observe_mode"

            spec = await client.get("/v1/openapi.json", headers=headers)
            spec_body = await spec.json()
            assert spec_body["openapi"] == "3.1.0"
            assert "security" in spec_body
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_offline_device_can_be_removed_without_deleting_history(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        store.remember_device(
            {
                "device_id": "offline-device",
                "suggested_alias": "Retired Bench",
                "firmware_mode": "normal",
            }
        )
        store.set_setting("status.offline-device", {"device_id": "offline-device"})
        store.append_event(
            "log",
            {"kind": "log", "text": "I (1) iris: preserved\n"},
            "offline-device",
        )
        store.create_operation(
            {
                "operation_id": "preserved-operation",
                "device_id": "offline-device",
                "actor_type": "developer",
                "actor_name": "Developer",
                "action": "system.info",
                "params": {},
                "status": "succeeded",
                "created_ns": 1,
            }
        )
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            removed = await client.delete("/v1/devices/offline-device")
            assert removed.status == 200
            assert await removed.json() == {
                "device_id": "offline-device",
                "removed": True,
                "history_preserved": True,
            }
            devices = await client.get("/v1/devices")
            assert "offline-device" not in {
                item["device_id"] for item in (await devices.json())["devices"]
            }
            assert store.get_setting("status.offline-device") is None
            assert len(store.latest_events(device_id="offline-device")) == 1
            assert store.operations("offline-device")[0]["operation_id"] == "preserved-operation"
            assert store.audits()[0]["action"] == "device.removed"

            connected = await client.delete("/v1/devices/demo-a1b2c3d4")
            assert connected.status == 409
            assert (await connected.json())["error"]["code"] == "device_connected"
            devices = await client.get("/v1/devices")
            assert "demo-a1b2c3d4" in {
                item["device_id"] for item in (await devices.json())["devices"]
            }
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_named_agent_token_can_switch_mode_without_approval(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(
            store, instance_id="test", demo=True, require_local_auth=True
        )
        service.auth.set_initial_password("dev-password")
        agent = service.auth.create_agent_token(
            "codex-bench", Actor("developer", "Developer")
        )
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            headers = {"Authorization": f"Bearer {agent['token']}"}
            response = await client.put("/v1/mode", json={"mode": "observe"}, headers=headers)
            assert response.status == 200
            audit = service.store.audits()[0]
            assert audit["actor_type"] == "agent"
            assert audit["actor_name"] == "codex-bench"
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_crash_report_matches_retained_elf_to_archived_candidate(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        artifact = store.save_firmware_artifact(
            binary=b"candidate-bin",
            elf=b"candidate-elf",
            map_data=b"candidate-map",
            metadata={
                "project_name": "candidate",
                "version": "1.0.0",
                "chip_id": 999,
            },
        )
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        original = hub.crash_report

        async def report(device_id: str) -> dict[str, object]:
            value = await original(device_id)
            return {
                **value,
                "core_dump_valid": True,
                "core_dump_elf_sha256": artifact["elf_sha256"],
                "core_dump_elf_sha256_complete": True,
                "decode_eligible": False,
            }

        hub.crash_report = report
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            response = await client.get(
                "/v1/devices/demo-a1b2c3d4/crashes"
            )
            assert response.status == 200
            crash = (await response.json())["reports"][0]
            assert crash["decode_eligible"] is True
            assert crash["candidate_artifact_id"] == artifact["artifact_id"]
            assert crash["candidate_elf_sha256"] == artifact["elf_sha256"]
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_crash_report_matches_truncated_core_sha_via_failed_image(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        artifact = store.save_firmware_artifact(
            binary=b"candidate-bin",
            elf=b"candidate-elf",
            map_data=b"candidate-map",
            metadata={
                "project_name": "candidate",
                "version": "1.0.0",
                "chip_id": 999,
            },
        )
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        original = hub.crash_report

        async def report(device_id: str) -> dict[str, object]:
            value = await original(device_id)
            elf_sha = str(artifact["elf_sha256"])
            return {
                **value,
                "core_dump_valid": True,
                "core_dump_elf_sha256": elf_sha[:9],
                "core_dump_elf_sha256_complete": False,
                "crash_failed_firmware_sha256": elf_sha,
            }

        hub.crash_report = report
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            response = await client.get(
                "/v1/devices/demo-a1b2c3d4/crashes"
            )
            assert response.status == 200
            crash = (await response.json())["reports"][0]
            assert crash["decode_eligible"] is True
            assert crash["candidate_artifact_id"] == artifact["artifact_id"]
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_crash_archive_preserves_and_decodes_exact_incident(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        artifact = store.save_firmware_artifact(
            binary=b"candidate-bin",
            elf=b"candidate-elf",
            map_data=b"candidate-map",
            metadata={
                "project_name": "candidate",
                "version": "1.0.0",
                "chip_id": 999,
            },
        )
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        original = hub.crash_report

        async def report(device_id: str) -> dict[str, object]:
            value = await original(device_id)
            return {
                **value,
                "core_dump_elf_sha256": artifact["elf_sha256"],
                "core_dump_elf_sha256_complete": True,
                "crash_failed_boot_id": 77,
            }

        hub.crash_report = report

        def decode(core_path, firmware_artifact, *, expected_failed_boot_id):
            assert core_path.is_file()
            assert firmware_artifact["artifact_id"] == artifact["artifact_id"]
            assert expected_failed_boot_id == 77
            return (
                {
                    "status": "succeeded",
                    "source_location_confirmed": True,
                    "incident_identity_confirmed": True,
                },
                b"decoded output",
            )

        monkeypatch.setattr(crashes_module, "decode_coredump", decode)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            response = await client.post(
                "/v1/devices/demo-e5f6a7b8/crashes/archive"
            )
            assert response.status == 200
            archived = await response.json()
            assert archived["status"] == "preserved"
            assert archived["failed_boot_id"] == 77
            assert archived["core_dump"]["bytes"] == 2048
            assert archived["diagnosis"]["source_location_confirmed"] is True
            assert archived["diagnosis"]["incident_identity_confirmed"] is True
            assert Path(archived["decoder_output"]).read_bytes() == b"decoded output"

            repeated = await client.post(
                "/v1/devices/demo-e5f6a7b8/crashes/archive"
            )
            assert await repeated.json() == archived
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_file_mutations_require_explicit_agent_scopes(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(
            store, instance_id="test", demo=True, require_local_auth=True
        )
        service.auth.set_initial_password("dev-password")
        reader = service.auth.create_agent_token(
            "file-reader", Actor("developer", "Developer")
        )
        manager = service.auth.create_agent_token(
            "file-manager",
            Actor("developer", "Developer"),
            {"files.read", "files.write", "files.delete"},
        )
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            reader_headers = {"Authorization": f"Bearer {reader['token']}"}
            readable = await client.get(
                "/v1/devices/demo-a1b2c3d4/files/volumes",
                headers=reader_headers,
            )
            assert readable.status == 200
            forbidden = await client.put(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "agent.bin"},
                data=b"reader-cannot-write",
                headers=reader_headers,
            )
            assert forbidden.status == 403
            assert (await forbidden.json())["error"] == {
                "code": "insufficient_scope",
                "message": "file operation requires the files.write scope",
                "details": {"required_scope": "files.write"},
            }

            manager_headers = {"Authorization": f"Bearer {manager['token']}"}
            uploaded = await client.put(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "agent.bin"},
                data=b"manager-can-write",
                headers=manager_headers,
            )
            assert uploaded.status == 201
            deleted = await client.delete(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "agent.bin"},
                headers=manager_headers,
            )
            assert deleted.status == 200
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_gateway_restart_always_defaults_to_develop(tmp_path) -> None:
    store = GatewayStore(tmp_path)
    store.set_setting("mode", "observe")
    service = GatewayService(store, instance_id="test")
    assert service.mode == "develop"
    assert store.get_setting("mode") == "develop"
    store.close()


def test_event_socket_sends_recent_history_then_live_logs(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="test")
        store.append_event(
            "log", {"kind": "log", "text": "I (1) app: stored\n"}, "device-a"
        )
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            websocket = await client.ws_connect(
                "/v1/events/ws?cursor=0&device_id=device-a"
            )
            assert (await websocket.receive_json())["text"].endswith("stored\n")
            await service.on_device_event(
                {"kind": "log", "device_id": "device-a", "text": "I (2) app: live\n"}
            )
            assert (await websocket.receive_json())["text"].endswith("live\n")
            await websocket.close()
        finally:
            await client.close()
            store.close()

    asyncio.run(scenario())


def test_frontend_entry_revalidates_while_hashed_assets_are_immutable(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path / "state")
        frontend = tmp_path / "frontend"
        assets = frontend / "assets"
        assets.mkdir(parents=True)
        (frontend / "index.html").write_text("<main>current workbench</main>")
        (assets / "index-current123.js").write_text("console.log('current')")
        service = GatewayService(store, instance_id="test", frontend_dist=frontend)
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            entry = await client.get("/")
            assert entry.status == 200
            assert entry.headers["Cache-Control"] == "no-store, no-cache, must-revalidate"
            assert entry.headers["Pragma"] == "no-cache"

            fallback = await client.get("/workspace")
            assert fallback.status == 200
            assert fallback.headers["Cache-Control"] == "no-store, no-cache, must-revalidate"

            asset = await client.get("/assets/index-current123.js")
            assert asset.status == 200
            assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        finally:
            await client.close()
            store.close()

    asyncio.run(scenario())


def test_frontend_static_path_rejects_parent_traversal(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path / "state")
        frontend = tmp_path / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("safe fallback")
        (tmp_path / "secret.txt").write_text("must not be served")
        service = GatewayService(store, instance_id="test", frontend_dist=frontend)
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            response = await client.get("/%2E%2E%2Fsecret.txt")
            assert response.status == 200
            assert await response.text() == "safe fallback"
        finally:
            await client.close()
            store.close()

    asyncio.run(scenario())


def test_loopback_is_unauthenticated_by_default_and_forwarded_header_is_ignored(
    tmp_path,
) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            health = await client.get("/v1/health")
            health_body = await health.json()
            assert health_body["authenticated_api"] is False
            assert health_body["local_authentication_required"] is False

            state = await client.get(
                "/v1/auth/state", headers={"X-Forwarded-For": "203.0.113.7"}
            )
            state_body = await state.json()
            assert state_body["required"] is False
            assert state_body["authenticated"] is True
            assert state_body["actor"]["type"] == "local"

            spec = await client.get("/v1/openapi.json")
            assert "security" not in await spec.json()

            devices = await client.get(
                "/v1/devices", headers={"X-Forwarded-For": "203.0.113.7"}
            )
            assert devices.status == 200
            assert len((await devices.json())["devices"]) == 3
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())


def test_non_loopback_peer_requires_authentication_by_default(tmp_path) -> None:
    store = GatewayStore(tmp_path)
    service = GatewayService(store, instance_id="test")
    transport = Mock()
    transport.get_extra_info.return_value = ("192.0.2.10", 42000)
    request = make_mocked_request("GET", "/v1/devices", transport=transport)
    assert service.authentication_required(request) is True
    store.close()


def test_cli_rejects_python_below_38(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_module.sys, "version_info", (3, 7, 17))
    assert cli_module.main(["doctor", "--json"]) == 2
    assert "Python 3.8 or newer" in capsys.readouterr().err


def test_local_auth_cli_is_opt_in() -> None:
    parser = build_parser()
    default_web = parser.parse_args(["web"])
    assert default_web.require_local_auth is False
    assert default_web.tls is False
    assert default_web.no_tls is False
    assert default_web.discover_usb_serial_jtag is False
    assert default_web.discover_mdns is None  # Auto: enabled when no endpoints are explicit.
    assert parser.parse_args(["web", "--no-discover-mdns"]).discover_mdns is False
    usj_web = parser.parse_args(
        [
            "web",
            "--usb-serial-jtag",
            "/dev/ttyACM0",
            "--discover-usb-serial-jtag",
        ]
    )
    assert usj_web.usb_serial_jtag == ["/dev/ttyACM0"]
    assert usj_web.discover_usb_serial_jtag is True
    assert parser.parse_args(["web", "--require-local-auth"]).require_local_auth is True
    assert parser.parse_args(["web", "--tls"]).tls is True
    assert _client_ssl({"url": "http://192.0.2.10:8443"}, insecure=False) is False
    assert _listen_is_loopback("127.0.0.2") is True
    assert _listen_is_loopback("::ffff:127.0.0.1") is True
    assert _listen_is_loopback("0.0.0.0") is False
    screenshot = parser.parse_args(
        ["ctl", "--json", "screenshot", "device-a", "/tmp/device.png"]
    )
    assert screenshot.ctl_command == "screenshot"
    assert screenshot.output == "/tmp/device.png"
    crash = parser.parse_args(["ctl", "--json", "crash", "device-a"])
    assert crash.ctl_command == "crash"
    assert crash.device == "device-a"
    coredump = parser.parse_args(
        ["ctl", "--json", "coredump", "device-a", "/tmp/device.core"]
    )
    assert coredump.ctl_command == "coredump"
    assert coredump.output == "/tmp/device.core"
    rpc_raw = parser.parse_args(
        ["ctl", "--json", "rpc-raw", "device-a", "0x1200", "1", "--payload", "{}"]
    )
    assert not hasattr(rpc_raw, "params")
    assert rpc_raw.payload == "{}"
    rpc_raw_stdin = parser.parse_args(
        [
            "ctl",
            "--json",
            "rpc-raw",
            "device-a",
            "0x1202",
            "1",
            "--payload-base64-stdin",
        ]
    )
    assert rpc_raw_stdin.payload_base64_stdin is True
    assert rpc_raw_stdin.payload == ""
    pointer_input = parser.parse_args(
        [
            "ctl",
            "--json",
            "input",
            "device-a",
            "--gesture",
            '{"begin":{"x":1,"y":2},"moves":[],"end":{"x":1,"y":2}}',
        ]
    )
    assert pointer_input.ctl_command == "input"
    assert pointer_input.device == "device-a"
    mirror = parser.parse_args(
        ["ctl", "--json", "mirror", "device-a", "start", "--fps", "7"]
    )
    assert mirror.ctl_command == "mirror"
    assert mirror.action == "start"
    assert mirror.fps == 7
    console = parser.parse_args(
        ["ctl", "--json", "console", "device-a", "iris_info"]
    )
    assert console.ctl_command == "console"
    assert console.line == ["iris_info"]
    ota_default = parser.parse_args(["ctl", "ota", "device-a", "app.bin"])
    assert ota_default.validation_mode == "elf_sha256"
    ota_version = parser.parse_args(
        [
            "ctl",
            "ota",
            "device-a",
            "app.bin",
            "--validation-mode",
            "version",
        ]
    )
    assert ota_version.validation_mode == "version"


def test_file_api_lists_and_streams_demo_volume(tmp_path) -> None:
    async def scenario() -> None:
        store = GatewayStore(tmp_path)
        service = GatewayService(store, instance_id="test", demo=True)
        hub = DemoHub(service.on_device_event)
        service.attach_hub(hub)
        await hub.start()
        client = TestClient(TestServer(create_app(service)))
        await client.start_server()
        try:
            volumes = await client.get("/v1/devices/demo-a1b2c3d4/files/volumes")
            assert volumes.status == 200
            assert (await volumes.json())["volumes"][0]["id"] == "cfg"

            listing = await client.get(
                "/v1/devices/demo-a1b2c3d4/files",
                params={"volume": "cfg", "path": "", "limit": "2"},
            )
            assert listing.status == 200
            page = await listing.json()
            assert len(page["entries"]) == 2
            assert page["next_cursor"] == 2
            assert page["snapshot"] is False

            downloaded = await client.get(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "app.json"},
            )
            content = await downloaded.read()
            assert downloaded.status == 200
            assert content == b'{"device":"esp-iris","mode":"demo"}\n'
            assert downloaded.headers["Accept-Ranges"] == "bytes"
            assert downloaded.headers["Content-Length"] == str(len(content))

            partial = await client.get(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "app.json"},
                headers={"Range": "bytes=1-5"},
            )
            assert partial.status == 206
            assert await partial.read() == content[1:6]
            assert partial.headers["Content-Range"] == f"bytes 1-5/{len(content)}"

            invalid_range = await client.get(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "app.json"},
                headers={"Range": "bytes=999-"},
            )
            assert invalid_range.status == 416

            uploaded = await client.put(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "new.bin"},
                data=b"streamed-upload",
                headers={"X-Operation-ID": "file-upload-new"},
            )
            assert uploaded.status == 201
            upload_body = await uploaded.json()
            assert upload_body["file"]["size"] == len(b"streamed-upload")
            assert upload_body["operation"]["action"] == "file.upload"
            assert "content" not in upload_body["operation"]["params"]

            app_stat = await client.get(
                "/v1/devices/demo-a1b2c3d4/files/stat",
                params={"volume": "cfg", "path": "app.json"},
            )
            app_etag = (await app_stat.json())["etag"]
            replaced = await client.put(
                "/v1/devices/demo-a1b2c3d4/file",
                params={
                    "volume": "cfg",
                    "path": "app.json",
                    "overwrite": "true",
                },
                data=b"replacement",
                headers={"If-Match": f'W/"{app_etag}"'},
            )
            assert replaced.status == 200
            assert (await replaced.json())["file"]["replaced"] is True

            created_directory = await client.post(
                "/v1/devices/demo-a1b2c3d4/directories",
                json={"volume": "cfg", "path": "uploads"},
            )
            assert created_directory.status == 201
            assert (await created_directory.json())["directory"]["kind"] == "directory"

            renamed = await client.post(
                "/v1/devices/demo-a1b2c3d4/file-rename",
                json={
                    "volume": "cfg",
                    "source": "new.bin",
                    "destination": "uploads/new.bin",
                },
            )
            assert renamed.status == 200
            assert (await renamed.json())["file"]["path"] == "uploads/new.bin"

            nonempty = await client.delete(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "uploads"},
            )
            assert nonempty.status == 400
            deleted_file = await client.delete(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "uploads/new.bin"},
            )
            assert deleted_file.status == 200
            deleted_directory = await client.delete(
                "/v1/devices/demo-a1b2c3d4/file",
                params={"volume": "cfg", "path": "uploads"},
            )
            assert deleted_directory.status == 200

            await client.put("/v1/mode", json={"mode": "observe"})
            blocked = await client.get(
                "/v1/devices/demo-a1b2c3d4/files/volumes"
            )
            assert blocked.status == 423
        finally:
            await client.close()
            await hub.close()
            store.close()

    asyncio.run(scenario())
