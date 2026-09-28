from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import ssl
import struct
import urllib.parse

import pytest
from aiohttp import ClientSession, WSMsgType

from .contracts import LOG_BURST_METHOD, LOG_BURST_V1, TEST_SERVICE_ID
from .gateway import GatewayApi, GatewayProcess

pytestmark = [
    pytest.mark.iris_e2e,
    pytest.mark.iris_stage(8),
    pytest.mark.firmware_profile("services_usb"),
]


async def _websocket_log_event(
    base_url: str,
    cookies: dict[str, str],
    device_id: str,
    trigger,
) -> dict:
    websocket_url = base_url.replace("https://", "wss://", 1)
    async with ClientSession(cookies=cookies) as session, session.ws_connect(
        f"{websocket_url}/v1/events/ws?device_id={device_id}",
        ssl=False,
    ) as websocket:
        trigger()
        for _ in range(200):
            message = await websocket.receive(timeout=5)
            if message.type is WSMsgType.TEXT:
                event = json.loads(message.data)
                if event.get("category") == "log":
                    return event
    raise TimeoutError("no log event arrived over the real WebSocket")


def test_cli_tls_auth_websocket_workbench_and_final_smoke(
    iris_board,
    iris_artifacts,
    iris_cli,
    iris_playwright,
    firmware_profile,
) -> None:
    assert firmware_profile == "services_usb"
    endpoint = iris_board.discover_application_port()
    doctor = iris_cli.run_top(
        ["doctor", "--json"], log_name="cli-doctor.log"
    )
    doctor_json = json.loads(doctor.stdout)
    assert doctor.returncode == 0 and doctor_json["python_supported"] is True

    with GatewayProcess(
        iris_artifacts,
        endpoint_kind="usb",
        endpoint=endpoint,
        tls=True,
        require_local_auth=True,
        name="final-tls",
    ) as gateway:
        api = gateway.start()
        status, _, _ = api.request("GET", "/v1/devices")
        assert status == 401
        status, login, _ = api.request(
            "POST",
            "/v1/auth/login",
            json_body={"password": "iris-e2e-developer-password"},
        )
        assert status == 200 and login["authenticated"] is True
        device = api.wait_device()
        device_id = device["device_id"]

        certificate = gateway.state_dir / "tls" / "gateway.crt"
        private_key = gateway.state_dir / "tls" / "gateway.key"
        assert certificate.is_file() and private_key.is_file()
        der = ssl.PEM_cert_to_DER_cert(certificate.read_text(encoding="ascii"))
        fingerprint = hashlib.sha256(der).hexdigest()
        assert len(fingerprint) == 64
        iris_artifacts.write_json(
            "tls.json", {"certificate": str(certificate), "fingerprint": fingerprint}
        )

        status, created, _ = api.request(
            "POST",
            "/v1/auth/tokens",
            json_body={
                "name": "iris-e2e-agent",
                "scopes": ["files.read", "files.write", "files.delete"],
            },
        )
        assert status == 201
        agent_token = created["token"]
        agent = GatewayApi(
            gateway.base_url,
            iris_artifacts,
            ssl_context=api.ssl_context,
            bearer_token=agent_token,
        )
        status, volumes, _ = agent.request(
            "GET", f"/v1/devices/{device_id}/files/volumes"
        )
        assert status == 200 and len(volumes["volumes"]) == 3

        status, reader, _ = api.request(
            "POST",
            "/v1/auth/tokens",
            json_body={"name": "iris-e2e-reader", "scopes": ["files.read"]},
        )
        assert status == 201
        read_only_agent = GatewayApi(
            gateway.base_url,
            iris_artifacts,
            ssl_context=api.ssl_context,
            bearer_token=reader["token"],
        )
        query = urllib.parse.urlencode({"volume": "fs", "path": "denied.txt"})
        status, denied, _ = read_only_agent.request(
            "PUT",
            f"/v1/devices/{device_id}/file?{query}",
            body=b"denied",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert status == 403 and denied["error"]["code"] == "insufficient_scope"

        devices = iris_cli.run(
            gateway.base_url,
            ["devices"],
            log_name="cli-devices.log",
            insecure=True,
            agent_token=agent_token,
        )
        assert devices["devices"][0]["device_id"] == device_id
        cli_status = iris_cli.run(
            gateway.base_url,
            ["status", device_id],
            log_name="cli-status.log",
            insecure=True,
            agent_token=agent_token,
        )
        assert cli_status["device_id"] == device_id
        rpc = iris_cli.run(
            gateway.base_url,
            ["rpc-raw", device_id, "1", "1", "--payload", "cli-echo"],
            log_name="cli-rpc-raw.log",
            insecure=True,
            agent_token=agent_token,
        )
        assert base64.b64decode(rpc["payload_base64"]) == b"cli-echo"

        status, started, _ = agent.request(
            "POST",
            f"/v1/devices/{device_id}/rpc/raw",
            json_body={
                "service_id": 1,
                "method_id": 2,
                "payload_hex": "00008813",
            },
        )
        assert status == 200
        job_id = struct.unpack(
            "<I", base64.b64decode(started["payload_base64"])
        )[0]
        job = iris_cli.run(
            gateway.base_url,
            ["jobs", device_id, str(job_id)],
            log_name="cli-job.log",
            insecure=True,
            agent_token=agent_token,
        )
        assert job["job"]["job_id"] == job_id
        cancelled = iris_cli.run(
            gateway.base_url,
            ["cancel", device_id, str(job_id)],
            log_name="cli-job-cancel.log",
            insecure=True,
            agent_token=agent_token,
        )
        assert cancelled["job"]["cancel_requested"] is True

        screenshot = iris_artifacts.root / "cli-screenshot.png"
        captured = iris_cli.run(
            gateway.base_url,
            ["screenshot", device_id, str(screenshot)],
            log_name="cli-screenshot.log",
            insecure=True,
            agent_token=agent_token,
        )
        assert captured["bytes"] > 0 and screenshot.read_bytes().startswith(b"\x89PNG")
        iris_cli.run(
            gateway.base_url,
            ["mirror", device_id, "start", "--channel", "image", "--fps", "10"],
            log_name="cli-mirror-start.log",
            insecure=True,
            agent_token=agent_token,
        )
        iris_cli.run(
            gateway.base_url,
            ["mirror", device_id, "stop", "--channel", "image"],
            log_name="cli-mirror-stop.log",
            insecure=True,
            agent_token=agent_token,
        )

        def trigger_log() -> None:
            status, _, _ = api.request(
                "POST",
                f"/v1/devices/{device_id}/rpc/raw",
                json_body={
                    "service_id": TEST_SERVICE_ID,
                    "method_id": LOG_BURST_METHOD,
                    "payload_hex": LOG_BURST_V1.pack(1, 0, 16, 0).hex(),
                },
            )
            assert status == 200

        event = asyncio.run(
            _websocket_log_event(
                gateway.base_url, api.cookies, device_id, trigger_log
            )
        )
        assert event["device_id"] == device_id

        missing_operation = iris_cli.run_top(
            [
                "ctl",
                "--url",
                gateway.base_url,
                "--insecure",
                "--json",
                "operation-status",
                "missing-operation",
            ],
            log_name="cli-operation-status-error.log",
            check=False,
            env={"ESP_IRIS_AGENT_TOKEN": agent_token},
        )
        assert missing_operation.returncode != 0
        missing_core = iris_cli.run_top(
            [
                "ctl",
                "--url",
                gateway.base_url,
                "--insecure",
                "--json",
                "coredump",
                device_id,
                str(iris_artifacts.root / "unexpected-coredump.bin"),
            ],
            log_name="cli-coredump-error.log",
            check=False,
            env={"ESP_IRIS_AGENT_TOKEN": agent_token},
        )
        assert missing_core.returncode != 0

        restarted = iris_cli.run(
            gateway.base_url,
            ["restart", device_id, "--delay-ms", "100"],
            log_name="cli-restart.log",
            insecure=True,
            agent_token=agent_token,
        )
        assert restarted["restart"]["reconnected"] is True

        iris_playwright.run_hardware(gateway.base_url, device_id)

        status, final, _ = api.request("GET", f"/v1/devices/{device_id}")
        assert status == 200
        assert final["invalid_frames"] == 0
        assert final["log_dropped_bytes"] == 0
        status, image, _ = api.request(
            "POST", f"/v1/devices/{device_id}/screenshot", json_body={}
        )
        assert status == 200 and image.startswith(b"\x89PNG")
        query = urllib.parse.urlencode({"volume": "fs", "path": "final-smoke.txt"})
        status, uploaded, _ = api.request(
            "PUT",
            f"/v1/devices/{device_id}/file?{query}",
            body=b"final smoke\n",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert status in {200, 201} and uploaded["file"]["size"] == 12
        status, _, _ = api.request(
            "DELETE", f"/v1/devices/{device_id}/file?{query}"
        )
        assert status == 200
