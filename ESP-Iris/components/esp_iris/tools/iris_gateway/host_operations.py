"""Local ROM work participates in the normal operation queue and history."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import sys
from typing import Any

from aiohttp import web

from .device_activity import stop_session_work
from .host_worker import active_workers, consume_request
from .http_support import json_body, request_actor, request_is_loopback
from .operations import OperationOutcomeUnknown


class HostOperations:
    def __init__(self, service: Any) -> None:
        self.service = service

    async def submit(self, request_id: str, actor: Any) -> dict:
        service = self.service
        existing = service.store.operation(request_id)
        if existing is not None:
            return existing
        spec = consume_request(request_id)
        action = spec.get("action")
        if action not in {"host.recovery", "host.probe"}:
            raise ValueError("unsupported host operation")
        commands = spec.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ValueError("host operation requires foreground commands")
        for command in commands:
            if not isinstance(command, dict) or not command.get("argv"):
                raise ValueError("host command requires argv")
            if not all(isinstance(part, str) and part for part in command["argv"]):
                raise ValueError("host argv must contain nonempty strings")
        timeout = float(spec.get("timeout", 60))
        if not 0 < timeout <= 3600:
            raise ValueError("timeout must be between zero and 3600 seconds")
        hub = service.device_hub
        device_id = spec.get("device_id")
        if device_id:
            device_id = service.resolve_device(device_id)
            target = hub.get(device_id)
            identifier = target.link.endpoint
        else:
            identifier = str(spec.get("endpoint") or "")
            if not identifier:
                raise ValueError("device_id or endpoint is required")
        primary = hub.host_endpoint(identifier)
        endpoints = [primary]
        if spec.get("write_endpoint"):
            writer = hub.host_endpoint(spec["write_endpoint"])
            if writer["endpoint"] == primary["endpoint"]:
                raise ValueError("an independent write endpoint must differ from the managed endpoint")
            if writer["endpoint"] != primary["endpoint"]:
                if writer.get("device_id") and writer["device_id"] != device_id:
                    raise ValueError("write endpoint belongs to a different device")
                endpoints.append(writer)
        else:
            writer = primary
        device_id = device_id or primary.get("device_id")
        resources = sorted({str(item.get("device_id") or "endpoint::" + item["endpoint"])
                            for item in endpoints} | {item["endpoint"] for item in endpoints})
        key = device_id or "endpoint::" + primary["endpoint"]
        if service.project is not None and (
            service.project.closing or any(item in service.project.blocked for item in resources)
        ):
            raise RuntimeError("device or project is draining")
        params = {"endpoints": endpoints, "expected_version": spec.get("expected_version"),
                  "expected_hardware_mac": spec.get("expected_hardware_mac"),
                  "request_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()}
        operation, _ = await service.operations.submit(
            key, actor, action, params,
            lambda: self.run(request_id, spec, device_id, endpoints, writer, resources, timeout),
            operation_id=request_id, exclusive_resources=tuple(resources),
        )
        return operation

    async def run(self, operation_id: str, spec: dict, device_id: str | None,
                  endpoints: list[dict], writer: dict, resources: list[str], timeout: float) -> dict:
        service, hub = self.service, self.service.device_hub
        directory = service.store.artifacts_dir / "host-operations" / operation_id
        directory.mkdir(parents=True, exist_ok=True)
        evidence = {}
        await service.operations.progress(operation_id, stage="preserving_evidence", progress_permille=0)
        if device_id:
            await stop_session_work(service, device_id, timeout)
            before = await hub.status(device_id)
            if before.get("device_id") != device_id or not before.get("boot_id"):
                raise RuntimeError("host operation requires verified device and boot identity")
            evidence["device_before"] = before
            evidence["crash_index"] = await hub.crash_report(device_id)
            evidence["coredump"] = await service.preserve_coredump(device_id)
        (directory / "before.json").write_text(json.dumps(evidence), encoding="utf-8")
        detached = []
        process = None
        writer_finished = False
        try:
            for endpoint in endpoints:
                detached.append(await hub.detach_for_host(endpoint["endpoint"]))
            for endpoint in detached:
                hub.yield_host_lock(endpoint["endpoint"])
            await service.operations.progress(operation_id, stage="transferring", progress_permille=100)
            worker_spec = {
                "operation_id": operation_id, "resources": resources, "endpoints": endpoints,
                "locks": [item.get("lock_endpoint") or item["endpoint"] for item in detached],
                "write_endpoint": writer.get("lock_endpoint") or writer["endpoint"],
                "commands": spec["commands"], "log_path": str(directory / "output.log"),
                "result_path": str(directory / "result.json"),
            }
            options = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000200}
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "iris_gateway.host_worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(pathlib.Path(__file__).resolve().parent.parent), **options,
            )
            assert process.stdin is not None
            process.stdin.write(json.dumps(worker_spec).encode())
            await process.stdin.drain()
            process.stdin.close()
            # Cancellation of this coroutine never signals the independent
            # worker. Its physical locks protect any still-running writer.
            await process.wait()
            writer_finished = True
            result_path = directory / "result.json"
            if not result_path.is_file():
                raise OperationOutcomeUnknown("host worker exited without a result; inspect its raw log")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("returncode"):
                raise RuntimeError(result.get("error", "host operation failed"))
            if spec["action"] == "host.recovery":
                await service.operations.progress(operation_id, stage="verifying", progress_permille=900)
                await hub.resume_after_host(endpoints[0]["endpoint"], connect=True)
                verification = await self.verify(endpoints[0]["endpoint"], device_id,
                                                 evidence.get("device_before", {}), spec, timeout)
                evidence["verification"] = verification
            (directory / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
            return {"evidence": evidence, "log_path": str(directory / "output.log"),
                    "returncode": 0, "stdout": result.get("stdout", "")[-65536:]}
        except Exception:  # Command delivery can fail after the process has started.
            if process is not None and not writer_finished:
                if process.stdin is not None:
                    process.stdin.close()
                await process.wait()
                writer_finished = True
            raise
        finally:
            # Spawn failure is safe to unwind; a disconnected Gateway must not
            # reconnect while its independent worker is still writing.
            if process is None or writer_finished:
                for endpoint in reversed(detached):
                    await hub.resume_after_host(endpoint["endpoint"])

    async def verify(self, endpoint: str, device_id: str | None, before: dict,
                     spec: dict, timeout: float) -> dict:
        hub = self.service.device_hub
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for candidate in hub.list_devices():
                if candidate.get("endpoint") != endpoint:
                    continue
                if device_id and candidate["device_id"] != device_id:
                    continue
                try:
                    status = await asyncio.wait_for(hub.status(candidate["device_id"]),
                                                    max(0.001, deadline - asyncio.get_running_loop().time()))
                except (OSError, LookupError, asyncio.TimeoutError):
                    continue
                if (status.get("device_id") == candidate["device_id"]
                        and status.get("boot_id") not in (None, "")
                        and str(status["boot_id"]) != str(before.get("boot_id"))
                        and status.get("firmware_mode") == "recovery"
                        and "ota" in status.get("capability_names", [])
                        and (not spec.get("expected_version") or status.get("app_version") == spec["expected_version"])
                        and (not spec.get("expected_hardware_mac") or status.get("hardware_mac") == spec["expected_hardware_mac"])):
                    return status
            await asyncio.sleep(0.05)
        raise OperationOutcomeUnknown("ROM write finished but Recovery identity verification did not complete")


def register_routes(app: web.Application, service: Any) -> None:
    async def submit(request: web.Request) -> web.Response:
        if not request_is_loopback(request) or request.headers.get("Origin"):
            raise PermissionError("host operations require a local CLI and a private request file")
        blocked = service.require_develop()
        if blocked is not None:
            return blocked
        if service.demo:
            raise ValueError("host operations require a physical device")
        body = await json_body(request)
        if set(body) != {"request_id"}:
            raise ValueError("host operations accept only a private request_id")
        operation = await service.host_operations.submit(str(body["request_id"]), request_actor(request))
        return web.json_response({"operation": operation,
                                  "status_url": "/v1/operations/" + operation["operation_id"]}, status=202)

    app.router.add_post("/v1/host-operations", submit)


__all__ = ["HostOperations", "active_workers", "register_routes"]
