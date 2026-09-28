#!/usr/bin/env python3
"""Exercise the File Service through a running Gateway and a real device."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


def request(
    base_url: str,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | bytes, dict[str, str]]:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        response = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload: dict[str, Any] | bytes = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        return exc.code, payload, dict(exc.headers)
    with response:
        raw = response.read()
        content_type = response.headers.get_content_type()
        payload = json.loads(raw) if content_type == "application/json" else raw
        return response.status, payload, dict(response.headers)


def query(endpoint: str, **values: str) -> str:
    return f"{endpoint}?{urllib.parse.urlencode(values)}"


def expect(status: int, wanted: int | tuple[int, ...], context: str) -> None:
    accepted = (wanted,) if isinstance(wanted, int) else wanted
    if status not in accepted:
        raise AssertionError(f"{context}: HTTP {status}, expected {accepted}")


def json_request(
    base_url: str,
    method: str,
    path: str,
    value: dict[str, Any],
    *,
    operation_id: str | None = None,
) -> tuple[int, dict[str, Any] | bytes, dict[str, str]]:
    headers = {"Content-Type": "application/json"}
    if operation_id is not None:
        headers["X-Operation-ID"] = operation_id
    return request(
        base_url,
        method,
        path,
        body=json.dumps(value).encode(),
        headers=headers,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://127.0.0.1:8879")
    parser.add_argument("--device-id")
    parser.add_argument("--volume", default="fs")
    args = parser.parse_args()

    status, payload, _ = request(args.gateway, "GET", "/v1/devices")
    expect(status, 200, "device discovery")
    assert isinstance(payload, dict)
    devices = [device for device in payload["devices"] if device["connected"]]
    if args.device_id is not None:
        devices = [device for device in devices if device["device_id"] == args.device_id]
    if len(devices) != 1:
        raise AssertionError(f"expected one target device, found {len(devices)}")
    device = devices[0]
    device_id = device["device_id"]
    assert "files" in device["capability_names"]
    root = f"/v1/devices/{device_id}"

    status, payload, _ = request(args.gateway, "GET", f"{root}/files/volumes")
    expect(status, 200, "volume discovery")
    assert isinstance(payload, dict)
    volume = next(item for item in payload["volumes"] if item["id"] == args.volume)
    required = {"read", "list", "write", "delete", "mkdir", "rename", "hash"}
    assert required <= set(volume["capability_names"])
    assert "atomic_replace" not in volume["capability_names"]

    suffix = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    directory = f"iris-e2e-{suffix}"
    original = f"{directory}/upload.bin"
    renamed = f"{directory}/renamed.bin"
    body = bytes((index * 37 + 11) & 0xFF for index in range(13_337))
    operation_prefix = f"hw-file-{suffix}"
    created_directory = False
    uploaded_path: str | None = None

    try:
        status, payload, _ = json_request(
            args.gateway,
            "POST",
            f"{root}/directories",
            {"volume": args.volume, "path": directory},
            operation_id=f"{operation_prefix}-mkdir",
        )
        expect(status, 201, "mkdir")
        created_directory = True

        upload_url = query(f"{root}/file", volume=args.volume, path=original)
        status, payload, _ = request(
            args.gateway,
            "PUT",
            upload_url,
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Operation-ID": f"{operation_prefix}-upload",
            },
        )
        expect(status, 201, "multi-chunk upload")
        uploaded_path = original
        assert isinstance(payload, dict)
        assert payload["file"]["size"] == len(body)
        assert payload["file"]["sha256"] == hashlib.sha256(body).hexdigest()

        stat_url = query(f"{root}/files/stat", volume=args.volume, path=original)
        status, metadata, _ = request(args.gateway, "GET", stat_url)
        expect(status, 200, "stat")
        assert isinstance(metadata, dict)
        assert metadata["size"] == len(body)

        download_url = query(f"{root}/file", volume=args.volume, path=original)
        status, downloaded, headers = request(args.gateway, "GET", download_url)
        expect(status, 200, "download")
        assert downloaded == body
        assert headers["Accept-Ranges"] == "bytes"

        status, ranged, headers = request(
            args.gateway,
            "GET",
            download_url,
            headers={"Range": "bytes=4093-6155"},
        )
        expect(status, 206, "range download")
        assert ranged == body[4093:6156]
        assert headers["Content-Range"] == f"bytes 4093-6155/{len(body)}"

        status, payload, _ = json_request(
            args.gateway,
            "POST",
            f"{root}/file-rename",
            {
                "volume": args.volume,
                "source": original,
                "destination": renamed,
            },
            operation_id=f"{operation_prefix}-rename",
        )
        expect(status, 200, "rename")
        uploaded_path = renamed

        list_url = query(f"{root}/files", volume=args.volume, path=directory)
        status, listing, _ = request(args.gateway, "GET", list_url)
        expect(status, 200, "directory list")
        assert isinstance(listing, dict)
        assert [entry["name"] for entry in listing["entries"]] == ["renamed.bin"]

        delete_dir_url = query(f"{root}/file", volume=args.volume, path=directory)
        status, _, _ = request(
            args.gateway,
            "DELETE",
            delete_dir_url,
            headers={"X-Operation-ID": f"{operation_prefix}-nonempty-delete"},
        )
        expect(status, 409, "non-empty directory rejection")

        status, _, _ = request(
            args.gateway,
            "PUT",
            query(
                f"{root}/file",
                volume=args.volume,
                path=renamed,
                overwrite="true",
            ),
            body=b"must not replace the original",
            headers={
                "Content-Type": "application/octet-stream",
                "If-Match": str(metadata["etag"]),
                "X-Operation-ID": f"{operation_prefix}-overwrite",
            },
        )
        expect(status, 501, "unsupported atomic overwrite")
        status, downloaded, _ = request(args.gateway, "GET", query(
            f"{root}/file", volume=args.volume, path=renamed
        ))
        expect(status, 200, "post-overwrite download")
        assert downloaded == body

        status, _, _ = request(
            args.gateway,
            "PUT",
            query(f"{root}/file", volume=args.volume, path="../escape.bin"),
            body=b"escape",
            headers={"Content-Type": "application/octet-stream"},
        )
        expect(status, 400, "path traversal rejection")

        status, payload, _ = request(args.gateway, "GET", "/v1/operations")
        expect(status, 200, "operation audit")
        assert isinstance(payload, dict)
        kinds = {
            operation["action"]
            for operation in payload["operations"]
            if operation["operation_id"].startswith(operation_prefix)
        }
        assert {"file.mkdir", "file.upload", "file.rename", "file.delete"} <= kinds
    finally:
        if uploaded_path is not None:
            request(
                args.gateway,
                "DELETE",
                query(f"{root}/file", volume=args.volume, path=uploaded_path),
                headers={"X-Operation-ID": f"{operation_prefix}-cleanup-file"},
            )
        if created_directory:
            request(
                args.gateway,
                "DELETE",
                query(f"{root}/file", volume=args.volume, path=directory),
                headers={"X-Operation-ID": f"{operation_prefix}-cleanup-directory"},
            )

    print(
        json.dumps(
            {
                "result": "PASS",
                "device_id": device_id,
                "transport": device["transport_name"],
                "volume": args.volume,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "operations": ["upload", "download", "mkdir", "rename", "delete"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
