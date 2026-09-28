"""Bounded Mosaico Ideas HTTP transport and private per-server state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .errors import OperationError, SelectionError
from .host import state_root


class PlatformError(OperationError):
    category = "platform_error"

    def __init__(self, code: str, status: int = 0, request_id: str = "") -> None:
        # Server messages and transport exceptions may contain credentials/URLs.
        super().__init__(
            "Mosaico Ideas request failed: "
            + code
            + (" (request_id: " + request_id + ")" if request_id else ""),
            details={"code": code, "http_status": status, "request_id": request_id},
        )
        self.code = code
        self.status = status


def server_url(value: str | None) -> str:
    value = value or os.environ.get("MAKER_SPARK_SERVER", "")
    parsed = urllib.parse.urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local)) or (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SelectionError(
            "Use --server with an HTTPS origin (HTTP is allowed for localhost)."
        )
    return value.rstrip("/")


def private_root(server: str) -> Path:
    state = state_root("esp-mosaico")
    server_key = hashlib.sha256(server.encode()).hexdigest()
    parent = state / "mosaico-ideas"
    root = parent / server_key
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        parent.chmod(0o700)
    # One-time per-server migration of credentials and resumable uploads.
    # New state wins; never merge it with potentially stale credentials.
    legacy = state / "maker-spark" / server_key
    if not root.exists() and legacy.exists():
        if legacy.is_symlink() or not legacy.is_dir():
            raise OperationError("Invalid legacy Mosaico Ideas state directory.")
        try:
            legacy.rename(root)
        except OSError as error:
            if not root.is_dir():
                raise OperationError("Cannot migrate private Mosaico Ideas state.") from error
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise OperationError("Cannot read private Mosaico Ideas state.") from error
    if not isinstance(value, dict):
        raise OperationError("Invalid private Mosaico Ideas state.")
    return value


def write_state(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def credential(server: str) -> str:
    token = os.environ.get("MAKER_SPARK_TOKEN")
    if token is None:
        token = read_state(private_root(server) / "credential.json").get("access_token")
    if not isinstance(token, str) or not token or any(c.isspace() for c in token):
        raise SelectionError("Log in with account login or set MAKER_SPARK_TOKEN.")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward bearer credentials or signed storage grants to a redirect.
        return None


class PlatformClient:
    def __init__(self, server: str, token: str | None = None) -> None:
        self.server = server_url(server)
        self.token = token
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _send(self, request: urllib.request.Request, *, storage: bool = False) -> Any:
        try:
            with self.opener.open(request, timeout=60) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise PlatformError("INVALID_RESPONSE")
                if storage or not raw:
                    return {}
                result = json.loads(raw.decode("utf-8"))
                if not isinstance(result, (dict, list)):
                    raise PlatformError("INVALID_RESPONSE")
                return result
        except urllib.error.HTTPError as error:
            if storage and error.code == 412:
                # Immutable object already exists; complete verifies its digest.
                return {}
            code, request_id = "HTTP_" + str(error.code), ""
            if not storage:
                try:
                    body = json.loads(error.read(16 * 1024))
                    candidate = body.get("code", "")
                    identifier = body.get("request_id", "")
                    if (
                        isinstance(candidate, str)
                        and candidate.replace("_", "").isalnum()
                        and len(candidate) < 80
                    ):
                        code = candidate
                    if (
                        isinstance(identifier, str)
                        and len(identifier) < 128
                        and all(c.isalnum() or c in "-_.:" for c in identifier)
                    ):
                        request_id = identifier
                except (ValueError, TypeError, AttributeError):
                    pass
            raise PlatformError(code, error.code, request_id) from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise PlatformError("NETWORK_ERROR") from None
        except (UnicodeDecodeError, ValueError):
            raise PlatformError("INVALID_RESPONSE") from None

    def request(
        self, method: str, path: str, body: Any = None, *, key: str | None = None
    ) -> Any:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if key:
            headers["Idempotency-Key"] = key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
        request = urllib.request.Request(
            self.server + "/api/v1" + path, data=data, headers=headers, method=method
        )
        for attempt in range(3):
            try:
                return self._send(request)
            except PlatformError as error:
                retryable = error.code in {
                    "NETWORK_ERROR",
                    "UPLOAD_BUSY",
                } or error.status in {502, 503, 504}
                safe = method == "GET" or key is not None or path.endswith("/complete")
                if not retryable or not safe or attempt == 2:
                    raise
                time.sleep(attempt + 1)

    def put_object(self, grant: dict[str, Any], data: bytes) -> None:
        url = grant["upload_url"]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise PlatformError("INVALID_UPLOAD_GRANT")
        if parsed.username or parsed.password or parsed.fragment:
            raise PlatformError("INVALID_UPLOAD_GRANT")
        headers = grant["headers"]
        if any(
            name.lower() in {"authorization", "cookie", "proxy-authorization"}
            for name in headers
        ):
            raise PlatformError("INVALID_UPLOAD_GRANT")
        request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
        self._send(request, storage=True)

    def multipart(
        self, path: str, filename: str, media_type: str, data: bytes, *, key: str
    ) -> Any:
        boundary = "mosaico-" + hashlib.sha256(os.urandom(32)).hexdigest()
        safe_name = filename.replace('"', "_")
        prefix = (
            "--"
            + boundary
            + '\r\nContent-Disposition: form-data; name="file"; filename="'
            + safe_name
            + '"\r\nContent-Type: '
            + media_type
            + "\r\n\r\n"
        ).encode()
        headers = {
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Idempotency-Key": key,
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            self.server + "/api/v1" + path,
            data=prefix + data + ("\r\n--" + boundary + "--\r\n").encode(),
            headers=headers,
            method="POST",
        )
        return self._send(request)
