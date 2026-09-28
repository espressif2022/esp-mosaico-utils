from __future__ import annotations

import abc
import asyncio
import contextlib
import hashlib
import os
import pathlib
import sys
import tempfile
from typing import Protocol

from .compat import to_thread


class SerialPort(Protocol):
    @property
    def is_open(self) -> bool: ...

    @property
    def in_waiting(self) -> int: ...

    def read(self, size: int) -> bytes: ...

    def write(self, data: bytes) -> int | None: ...

    def close(self) -> None: ...


class Link(abc.ABC):
    endpoint: str

    @abc.abstractmethod
    async def read(self, size: int = 4096) -> bytes: ...

    @abc.abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class TcpLink(Link):
    def __init__(
        self,
        host: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.host = host
        self.port = port
        self.endpoint = f"tcp:{host}:{port}"
        self._reader = reader
        self._writer = writer

    @classmethod
    async def open(cls, host: str, port: int) -> TcpLink:
        reader, writer = await asyncio.open_connection(host, port)
        return cls(host, port, reader, writer)

    async def read(self, size: int = 4096) -> bytes:
        return await self._reader.read(size)

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        await self._writer.wait_closed()


class SerialLink(Link):
    def __init__(
        self, port: str, serial_port: SerialPort, *, endpoint: str | None = None
    ) -> None:
        self.port = port
        # Discovery prefers a stable by-path identity. Keep that identity
        # instead of resolving it to a transient tty/COM endpoint so the same
        # supervisor survives firmware re-enumeration.
        self.endpoint = endpoint or f"usb:{port}"
        self._serial = serial_port
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False

    @classmethod
    async def open(
        cls,
        port: str,
        *,
        hupcl: bool | None = None,
        endpoint: str | None = None,
    ) -> SerialLink:
        import serial

        def open_port() -> SerialPort:
            serial_port = serial.Serial(
                port=port,
                baudrate=115200,
                timeout=0.2,
                write_timeout=2,
                exclusive=True if os.name == "posix" else None,
            )
            try:
                if hupcl is not None and sys.platform != "win32":
                    import termios

                    attributes = termios.tcgetattr(serial_port.fileno())
                    if hupcl:
                        attributes[2] |= termios.HUPCL
                    else:
                        attributes[2] &= ~termios.HUPCL
                    termios.tcsetattr(
                        serial_port.fileno(), termios.TCSANOW, attributes
                    )
            except BaseException:
                serial_port.close()
                raise
            return serial_port

        serial_port = await to_thread(open_port)
        return cls(port, serial_port, endpoint=endpoint)

    def _read_batch(self, size: int) -> bytes:
        if size <= 0:
            return b""
        data = self._serial.read(1)
        if not data or size == 1:
            return data
        waiting = int(getattr(self._serial, "in_waiting", 0) or 0)
        if waiting <= 0:
            return data
        return data + self._serial.read(min(waiting, size - len(data)))

    async def read(self, size: int = 65536) -> bytes:
        while True:
            async with self._read_lock:
                if self._closing or not self._serial.is_open:
                    return b""
                read_task = asyncio.create_task(
                    to_thread(self._read_batch, size)
                )
                try:
                    data = await asyncio.shield(read_task)
                except asyncio.CancelledError:
                    cancel_read = getattr(self._serial, "cancel_read", None)
                    if cancel_read is not None:
                        await to_thread(cancel_read)
                    with contextlib.suppress(Exception):
                        await read_task
                    raise
            if data:
                return data
            if self._closing:
                return b""
            await asyncio.sleep(0)

    async def write(self, data: bytes) -> None:
        async with self._write_lock:
            if self._closing or not self._serial.is_open:
                raise ConnectionError("ESP-Iris serial link is closed")
            written = await to_thread(self._serial.write, data)
        if written != len(data):
            raise OSError(f"short serial write: {written}/{len(data)}")

    async def close(self) -> None:
        async with self._close_lock:
            if not self._serial.is_open:
                return
            self._closing = True
            cancel_read = getattr(self._serial, "cancel_read", None)
            if cancel_read is not None:
                with contextlib.suppress(Exception):
                    await to_thread(cancel_read)
            async with self._read_lock, self._write_lock:
                if self._serial.is_open:
                    await to_thread(self._serial.close)


class EndpointLock:
    """Cross-process advisory lock for one physical endpoint."""

    @staticmethod
    def held(endpoint: str, root: pathlib.Path) -> bool:
        """Probe an existing lock without creating files or changing owner metadata."""
        path = root / f"{hashlib.sha256(endpoint.encode()).hexdigest()}.lock"
        try:
            stream = path.open("r+b")
        except FileNotFoundError:
            return False
        with stream:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return False

    def __init__(self, endpoint: str, *, root: pathlib.Path | None = None) -> None:
        digest = hashlib.sha256(endpoint.encode()).hexdigest()
        root = root or pathlib.Path(tempfile.gettempdir()) / "esp-iris-locks"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / f"{digest}.lock"
        # Windows supports locking ranges beyond EOF. Do not initialize a byte
        # here: another owner may hold the lock while replacing its metadata.
        self._file = self.path.open("a+b")

    def _owner_message(self) -> str:
        try:
            self._file.seek(0)
            owner = self._file.read(512).decode("utf-8", errors="replace").strip()
        except OSError:
            # Windows byte-range locks can also prohibit reading this byte.
            owner = "owner metadata unavailable"
        return f"endpoint is owned by another ESP-Iris instance ({owner}; lock={self.path})"

    def acquire(self, *, blocking: bool = False) -> None:
        if sys.platform == "win32":
            import msvcrt

            self._file.seek(0)
            try:
                msvcrt.locking(  # type: ignore[attr-defined]
                    self._file.fileno(),
                    msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
                    1,
                )
            except OSError as exc:
                raise RuntimeError(
                    self._owner_message()
                ) from exc
        else:
            import fcntl

            try:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(self._file.fileno(), flags)
            except OSError as exc:
                raise RuntimeError(
                    self._owner_message()
                ) from exc
        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"pid={os.getpid()}\nendpoint={self.path.name}\n".encode())
        self._file.flush()

    def close(self) -> None:
        if self._file.closed:
            return
        if sys.platform == "win32":
            import msvcrt

            self._file.seek(0)
            try:
                msvcrt.locking(  # type: ignore[attr-defined]
                    self._file.fileno(), msvcrt.LK_UNLCK, 1  # type: ignore[attr-defined]
                )
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()

    def fileno(self) -> int:
        """Allow a supervised POSIX writer to retain this lock if its parent dies."""
        return self._file.fileno()
