"""Browser-approved device login; secrets never pass through RunContext."""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
import webbrowser
from typing import Any

from .errors import SelectionError
from .platform_client import (
    PlatformClient,
    PlatformError,
    credential,
    private_root,
    read_state,
    server_url,
    write_state,
)


def account(arguments: Any) -> dict[str, Any]:
    server = server_url(arguments.server)
    path = private_root(server) / "credential.json"
    action = arguments.account_action
    if action == "login":
        client = PlatformClient(server)
        device = client.request("POST", "/auth/cli/device", {"name": "ESP-Mosaico CLI"})
        if (
            not isinstance(device, dict)
            or any(
                not isinstance(device.get(field), str) or not device[field]
                for field in ("verification_uri", "user_code", "device_code")
            )
            or not isinstance(device.get("expires_in"), int)
            or not 1 <= device["expires_in"] <= 600
            or not isinstance(device.get("interval", 5), int)
            or not 1 <= device.get("interval", 5) <= 30
            or len(device["user_code"]) > 32
            or not all(c.isalnum() or c == "-" for c in device["user_code"])
        ):
            raise PlatformError("INVALID_RESPONSE")
        uri = device["verification_uri"]
        parsed = urllib.parse.urlsplit(uri)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            (parsed.scheme != "https" and not (parsed.scheme == "http" and local))
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise PlatformError("INVALID_VERIFICATION_URI")
        # User code is deliberately separate from URLs and JSON output.
        print(
            "Open " + uri + " and enter code " + device["user_code"],
            file=sys.stderr,
            flush=True,
        )
        if arguments.open_browser:
            webbrowser.open(uri)
        deadline = time.monotonic() + min(int(device["expires_in"]), 600)
        interval = max(1, min(int(device.get("interval", 5)), 30))
        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                token = client.request(
                    "POST", "/auth/cli/token", {"device_code": device["device_code"]}
                )
            except PlatformError as error:
                if error.code == "AUTHORIZATION_PENDING":
                    continue
                raise
            if not isinstance(token, dict):
                raise PlatformError("INVALID_RESPONSE")
            access = token.get("access_token")
            if (
                not isinstance(access, str)
                or not access
                or any(c.isspace() for c in access)
                or not isinstance(token.get("expires_in"), int)
                or token["expires_in"] <= 0
            ):
                raise PlatformError("INVALID_RESPONSE")
            write_state(
                path,
                {
                    "access_token": access,
                    "expires_at": time.time() + token["expires_in"],
                },
            )
            return {
                "command": "account login",
                "status": "authenticated",
                "server": server,
            }
        raise PlatformError("EXPIRED_TOKEN")
    if action == "status":
        client = PlatformClient(server, credential(server))
        value = client.request("GET", "/auth/cli/token")
        user = value.get("user", {})
        return {
            "command": "account status",
            "status": "authenticated",
            "server": server,
            "user_id": user.get("id"),
            "name": value.get("name"),
            "scopes": value.get("scopes"),
            "expires_at": value.get("expires_at"),
            "credential_source": "environment"
            if "MAKER_SPARK_TOKEN" in os.environ
            else "private_state",
        }
    if action == "logout":
        # An environment token belongs to the caller; explicitly revoke it too.
        stored = read_state(path)
        if not os.environ.get("MAKER_SPARK_TOKEN") and not stored.get("access_token"):
            return {
                "command": "account logout",
                "status": "logged_out",
                "server": server,
            }
        client = PlatformClient(server, credential(server))
        try:
            client.request("DELETE", "/auth/cli/token")
        except PlatformError as error:
            if error.status != 401:
                raise
        if "MAKER_SPARK_TOKEN" not in os.environ and path.exists():
            path.unlink()
        return {"command": "account logout", "status": "logged_out", "server": server}
    raise SelectionError("Unknown account command.")
