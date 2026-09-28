"""Process-owned USB exclusion for local host operations.

The worker survives its HTTP client and Gateway. It holds the physical locks
until the command exits; no timeout or disconnected parent kills a flash writer.
Only the local filesystem carries executable commands, never an HTTP body.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Any

from .discovery import resolve_usb_port, usb_endpoint
from .link import EndpointLock


def request_directory() -> pathlib.Path:
    root = pathlib.Path(tempfile.gettempdir()) / "esp-iris-host-requests"
    if hasattr(os, "getuid"):
        root = root.with_name(root.name + "-" + str(os.getuid()))
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or (
        hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise PermissionError("host request directory must be private to the current user")
    return root


def publish_request(spec: dict) -> str:
    request_id = str(uuid.uuid4())
    path = request_directory() / (request_id + ".json")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(spec, stream)
    return request_id


def consume_request(request_id: str) -> dict:
    if str(uuid.UUID(request_id)) != request_id:
        raise ValueError("invalid local request ID")
    path = request_directory() / (request_id + ".json")
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise KeyError("local host request does not exist or was already consumed") from error
    if not stat.S_ISREG(info.st_mode) or (
        hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise PermissionError("host request must be a private regular file")
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    path.unlink()
    if not isinstance(value, dict):
        raise TypeError("host request must be an object")
    return value


def active_workers() -> list[dict]:
    result = []
    for path in request_directory().glob("worker-*.json"):
        operation_id = path.stem[len("worker-"):]
        lock = EndpointLock("host-operation:" + operation_id)
        try:
            try:
                lock.acquire()
            except RuntimeError:
                try:
                    result.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
            else:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
        finally:
            lock.close()
    return result


def run(spec: dict) -> dict:
    operation_id = spec["operation_id"]
    record = request_directory() / ("worker-" + operation_id + ".json")
    outputs = []
    with contextlib.ExitStack() as stack:
        descriptors = []
        for key in ["host-operation:" + operation_id, *sorted(set(spec["locks"]))]:
            lock = EndpointLock(key)
            stack.callback(lock.close)
            lock.acquire()
            descriptors.append(lock.fileno())
        record.write_text(json.dumps({
            "operation_id": operation_id, "resources": spec["resources"],
            "endpoints": spec["endpoints"], "pid": os.getpid(),
        }), encoding="utf-8")
        stack.callback(lambda: record.unlink(missing_ok=True))
        write_endpoint = spec["write_endpoint"]
        current = resolve_usb_port(write_endpoint)
        if usb_endpoint(current) not in spec["locks"]:
            raise RuntimeError("USB endpoint identity changed before the host operation")
        port = str(current["device_path"])
        for step in spec["commands"]:
            argv = [part.replace("{port}", port) for part in step["argv"]]
            environment = os.environ.copy()
            environment.update({key: value.replace("{port}", port)
                                for key, value in step.get("env", {}).items()})
            # Commands are foreground executors. Their complete process lifetime
            # is protected, including nested idf.py/CMake/esptool invocations.
            with open(spec["log_path"], "ab") as log:
                offset = log.tell()
                inheritance: dict[str, Any] = {"pass_fds": tuple(descriptors)} if os.name != "nt" else {}
                completed = subprocess.run(argv, cwd=step.get("cwd"), env=environment,
                                           stdout=log, stderr=subprocess.STDOUT, check=False, **inheritance)
            with open(spec["log_path"], "rb") as log:
                log.seek(offset)
                output = log.read().decode("utf-8", errors="replace")
            if completed.returncode:
                raise RuntimeError(f"host command exited with code {completed.returncode}; see {spec['log_path']}")
            expected = step.get("expect")
            if expected:
                match = re.search(expected["pattern"], output)
                if match is None or match.group(1).lower() != expected["value"].lower():
                    raise RuntimeError("host command identity verification failed; no following command was run")
            outputs.append(output)
    return {"returncode": 0, "stdout": outputs[-1] if outputs else ""}


def main() -> None:
    # A terminal Ctrl-C targets the CLI, not a write in progress. The Gateway
    # starts us in a separate process group; ignore accidental soft signals too.
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_IGN)
    spec = json.load(sys.stdin)
    try:
        result = run(spec)
    except Exception as exc:  # noqa: BLE001 - persist any executor failure for the Gateway
        result = {"returncode": 1, "error": str(exc)}
    path = pathlib.Path(spec["result_path"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
