"""ESP-Iris multi-image system-update bundles.

The manifest is the update plan for both the Gateway and the product backend.
Bundles may be signed or explicitly unsigned. Archive metadata, member names
and component bytes are treated as untrusted input and are fully bounded
before any device operation begins.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import pathlib
import tempfile
import zipfile
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .firmware import inspect_firmware_image

SYSTEM_UPDATE_SCHEMA = "esp-iris-system-update/v1"
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 16
MAX_MANIFEST_BYTES = 2048
MAX_SIGNATURE_BYTES = 256
MAX_COMPONENTS = 8
MAX_COMPONENT_BYTES = 16 * 1024 * 1024
PARTITION_TABLE_REGION_BYTES = 0x1000
MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "manifest.sig"


class SystemUpdateComponentKind(enum.IntEnum):
    BOOTLOADER = 1
    PARTITION_TABLE = 2
    APPLICATION = 3
    RECOVERY = 4
    DATA = 5


_KIND_NAMES = {
    "bootloader": SystemUpdateComponentKind.BOOTLOADER,
    "partition_table": SystemUpdateComponentKind.PARTITION_TABLE,
    "application": SystemUpdateComponentKind.APPLICATION,
    "recovery": SystemUpdateComponentKind.RECOVERY,
    "data": SystemUpdateComponentKind.DATA,
}


@dataclasses.dataclass(frozen=True)
class SystemUpdateComponent:
    id: int
    kind: SystemUpdateComponentKind
    flags: int
    target_offset: int
    size: int
    sha256: bytes
    filename: str
    data: bytes

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.name.lower(),
            "flags": self.flags,
            "target_offset": self.target_offset,
            "size": self.size,
            "sha256": self.sha256.hex(),
            "file": self.filename,
        }


@dataclasses.dataclass(frozen=True)
class SystemUpdateBundle:
    manifest: Mapping[str, Any]
    manifest_bytes: bytes
    signature: bytes
    manifest_sha256: bytes
    key_id: str | None
    signature_verified: bool
    chip_id: int
    flash_size: int
    target_layout_sha256: str
    components: tuple[SystemUpdateComponent, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SYSTEM_UPDATE_SCHEMA,
            "manifest_sha256": self.manifest_sha256.hex(),
            "key_id": self.key_id,
            "chip_id": self.chip_id,
            "flash_size": self.flash_size,
            "target_layout_sha256": self.target_layout_sha256,
            "components": [item.as_dict() for item in self.components],
            "signature_verified": self.signature_verified,
            "release": self.manifest.get("release"),
            "minimum_recovery_version": self.manifest.get("minimum_recovery_version"),
        }


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest field {key!r}")
        result[key] = value
    return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def _require_fields(
    value: Mapping[str, Any],
    name: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    missing = required - value.keys()
    unknown = value.keys() - allowed
    if missing:
        raise ValueError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")


def _bounded_uint(value: Any, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not 0 <= value <= maximum:
        raise ValueError(f"{name} is outside 0..{maximum}")
    return value


def _sha256_hex(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must contain 64 lowercase hex characters")
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not hexadecimal") from exc
    if value != value.lower() or len(raw) != 32:
        raise ValueError(f"{name} must contain canonical lowercase SHA-256")
    return value


def _safe_member_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{name} must be a non-empty archive member name")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ValueError(f"{name} must be a root-level archive filename")
    return value


def _load_public_key(data: bytes) -> ec.EllipticCurvePublicKey:
    try:
        key = serialization.load_pem_public_key(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("system-update trust key is not valid PEM") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise TypeError("system-update trust key must be ECDSA P-256")
    return key


def _load_private_key(
    data: bytes, password: bytes | None = None
) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(data, password=password)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "system-update signing key or password is not valid PEM"
        ) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise TypeError("system-update signing key must be ECDSA P-256")
    return key


def _archive_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if not 1 <= len(infos) <= MAX_ARCHIVE_MEMBERS:
        raise ValueError("system-update archive has an invalid member count")
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in infos:
        name = _safe_member_name(info.filename, "archive member")
        if name in result:
            raise ValueError(f"duplicate archive member {name!r}")
        if info.is_dir() or info.flag_bits & 0x1:
            raise ValueError("directories and encrypted archive members are not allowed")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError("unsupported system-update archive compression")
        if info.file_size > MAX_COMPONENT_BYTES:
            raise ValueError(f"archive member {name!r} is too large")
        total += info.file_size
        if total > MAX_BUNDLE_BYTES:
            raise ValueError("system-update archive expands beyond the size limit")
        result[name] = info
    return result


def load_system_update_bundle(
    source: str | pathlib.Path | bytes,
    *,
    trusted_public_key: bytes | None = None,
) -> SystemUpdateBundle:
    """Load a bounded signed or explicitly unsigned ``.irisfw`` archive.

    Supplying ``trusted_public_key`` requires a signed bundle and verifies it.
    Without a trust key, only unsigned bundles are accepted.
    """

    if isinstance(source, bytes):
        import io

        if len(source) > MAX_BUNDLE_BYTES:
            raise ValueError("system-update archive exceeds the size limit")
        stream: Any = io.BytesIO(source)
    else:
        path = pathlib.Path(source)
        if not path.is_file() or path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError("system-update archive is missing or too large")
        stream = path
    public_key = (
        _load_public_key(trusted_public_key)
        if trusted_public_key is not None
        else None
    )
    try:
        archive_context = zipfile.ZipFile(stream)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("system-update bundle is not a valid ZIP archive") from exc
    with archive_context as archive:
        members = _archive_members(archive)
        if MANIFEST_NAME not in members:
            raise ValueError("system-update bundle requires manifest.json")
        manifest_bytes = archive.read(members[MANIFEST_NAME])
        signature = (
            archive.read(members[SIGNATURE_NAME])
            if SIGNATURE_NAME in members
            else b""
        )
        if not 1 <= len(manifest_bytes) <= MAX_MANIFEST_BYTES:
            raise ValueError("system-update manifest exceeds the device bound")
        if len(signature) > MAX_SIGNATURE_BYTES:
            raise ValueError("system-update signature has an invalid size")
        if signature and public_key is None:
            raise ValueError("signed system-update bundle requires a trust key")
        if not signature and public_key is not None:
            raise ValueError("unsigned system-update bundle rejected by trust policy")
        if signature:
            try:
                assert public_key is not None
                public_key.verify(
                    signature, manifest_bytes, ec.ECDSA(hashes.SHA256())
                )
            except InvalidSignature as exc:
                raise ValueError(
                    "system-update manifest signature is invalid"
                ) from exc
        try:
            document = json.loads(
                manifest_bytes.decode("utf-8"), object_pairs_hook=_strict_object
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("system-update manifest is not valid UTF-8 JSON") from exc
        manifest = _mapping(document, "manifest")
        if manifest.get("schema") != SYSTEM_UPDATE_SCHEMA:
            raise ValueError("unsupported system-update manifest schema")
        _require_fields(
            manifest,
            "manifest",
            required={
                "schema",
                "target",
                "target_layout_sha256",
                "components",
            },
            optional={"release", "minimum_recovery_version", "signature"},
        )
        for optional_text in ("release", "minimum_recovery_version"):
            optional_value = manifest.get(optional_text)
            if optional_value is not None and (
                not isinstance(optional_value, str)
                or not 1 <= len(optional_value) <= 64
            ):
                raise TypeError(f"{optional_text} must be a bounded string")
        signature_value = manifest.get("signature")
        if signature:
            signature_info = _mapping(signature_value, "signature")
            _require_fields(
                signature_info,
                "signature",
                required={"algorithm", "key_id"},
            )
            if signature_info.get("algorithm") != "ecdsa-p256-sha256":
                raise ValueError("unsupported system-update signature algorithm")
            key_id_value = signature_info.get("key_id")
            if not isinstance(key_id_value, str) or not 1 <= len(key_id_value) <= 64:
                raise ValueError("system-update signature key_id is invalid")
            key_id: str | None = key_id_value
        else:
            if signature_value is not None:
                raise ValueError("unsigned system-update manifest must omit signature")
            key_id = None
        target = _mapping(manifest.get("target"), "target")
        _require_fields(
            target, "target", required={"chip_id", "flash_size"}
        )
        chip_id = _bounded_uint(target.get("chip_id"), "target.chip_id", 0xFFFF)
        flash_size = _bounded_uint(
            target.get("flash_size"), "target.flash_size", 0xFFFFFFFF
        )
        if flash_size == 0 or flash_size % 4096:
            raise ValueError("target.flash_size must be a non-zero 4 KiB multiple")
        target_layout = _sha256_hex(
            manifest.get("target_layout_sha256"), "target_layout_sha256"
        )
        component_values = manifest.get("components")
        if not isinstance(component_values, list) or not 1 <= len(
            component_values
        ) <= MAX_COMPONENTS:
            raise ValueError("components must contain 1..8 entries")
        components: list[SystemUpdateComponent] = []
        identifiers: set[int] = set()
        singleton_kinds: set[SystemUpdateComponentKind] = set()
        filenames = {MANIFEST_NAME}
        if signature:
            filenames.add(SIGNATURE_NAME)
        for index, value in enumerate(component_values):
            item = _mapping(value, f"components[{index}]")
            _require_fields(
                item,
                f"components[{index}]",
                required={
                    "id",
                    "kind",
                    "target_offset",
                    "size",
                    "sha256",
                    "file",
                },
                optional={"flags"},
            )
            identifier = _bounded_uint(item.get("id"), "component id", 0xFF)
            if identifier == 0 or identifier in identifiers:
                raise ValueError("component IDs must be unique and non-zero")
            identifiers.add(identifier)
            kind_name = item.get("kind")
            if kind_name not in _KIND_NAMES:
                raise ValueError(f"unsupported component kind {kind_name!r}")
            kind = _KIND_NAMES[str(kind_name)]
            if kind is not SystemUpdateComponentKind.DATA:
                if kind in singleton_kinds:
                    raise ValueError(f"component kind {kind_name!r} is duplicated")
                singleton_kinds.add(kind)
            flags = _bounded_uint(item.get("flags", 0), "component flags", 0xFFFF)
            offset = _bounded_uint(
                item.get("target_offset"), "component target_offset", 0xFFFFFFFF
            )
            size = _bounded_uint(item.get("size"), "component size", 0xFFFFFFFF)
            if size == 0 or size > MAX_COMPONENT_BYTES or offset + size > flash_size:
                raise ValueError("component is empty, too large or outside target Flash")
            alignment = (
                0x10000
                if kind is SystemUpdateComponentKind.APPLICATION
                else 0x1000
            )
            if offset % alignment:
                raise ValueError(
                    f"component {identifier} target offset is not 0x{alignment:x}-aligned"
                )
            sha_hex = _sha256_hex(item.get("sha256"), "component sha256")
            filename = _safe_member_name(item.get("file"), "component file")
            if filename in filenames or filename not in members:
                raise ValueError(f"component archive member {filename!r} is invalid")
            filenames.add(filename)
            data = archive.read(members[filename])
            if len(data) != size:
                raise ValueError(f"component {identifier} size does not match manifest")
            digest = hashlib.sha256(data).digest()
            if digest.hex() != sha_hex:
                raise ValueError(f"component {identifier} SHA-256 does not match manifest")
            components.append(
                SystemUpdateComponent(
                    id=identifier,
                    kind=kind,
                    flags=flags,
                    target_offset=offset,
                    size=size,
                    sha256=digest,
                    filename=filename,
                    data=data,
                )
            )
            if kind in {
                SystemUpdateComponentKind.APPLICATION,
                SystemUpdateComponentKind.RECOVERY,
            }:
                firmware = inspect_firmware_image(data)
                if firmware.chip_id != chip_id:
                    raise ValueError(
                        f"component {identifier} {kind.name.lower()} chip ID does not match target"
                    )
        unused = set(members) - filenames
        if unused:
            raise ValueError(
                "system-update bundle contains undeclared members: "
                + ", ".join(sorted(unused))
            )
        ordered_regions = sorted(
            (item.target_offset, item.target_offset + item.size, item.id)
            for item in components
        )
        # itertools.pairwise is unavailable on the supported Python 3.8/3.9.
        for previous, current in zip(
            ordered_regions, ordered_regions[1:]
        ):
            if current[0] < previous[1]:
                raise ValueError(
                    f"components {previous[2]} and {current[2]} overlap in target Flash"
                )
        partition_tables = [
            item
            for item in components
            if item.kind is SystemUpdateComponentKind.PARTITION_TABLE
        ]
        if partition_tables and partition_tables[0].size != PARTITION_TABLE_REGION_BYTES:
            raise ValueError("partition-table component must cover one 4 KiB sector")
        recoveries = [
            item
            for item in components
            if item.kind is SystemUpdateComponentKind.RECOVERY
        ]
        if recoveries and len(components) != 1:
            raise ValueError("recovery must be the bundle's only component")
        if recoveries and target_layout == "0" * 64:
            raise ValueError(
                "recovery bundle requires an explicit target layout SHA-256"
            )
        bootloaders = [
            item
            for item in components
            if item.kind is SystemUpdateComponentKind.BOOTLOADER
        ]
        if bootloaders:
            if not partition_tables:
                raise ValueError(
                    "bootloader component requires a partition-table component"
                )
            if (
                bootloaders[0].target_offset + bootloaders[0].size
                != partition_tables[0].target_offset
            ):
                raise ValueError(
                    "bootloader component must cover the full range before the partition table"
                )
        if partition_tables and partition_tables[0].sha256.hex() != target_layout:
            raise ValueError("target layout SHA-256 does not match partition table")
        return SystemUpdateBundle(
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            signature=signature,
            manifest_sha256=hashlib.sha256(manifest_bytes).digest(),
            key_id=key_id,
            signature_verified=bool(signature),
            chip_id=chip_id,
            flash_size=flash_size,
            target_layout_sha256=target_layout,
            components=tuple(components),
        )


def build_system_update_bundle(
    destination: str | pathlib.Path,
    manifest: Mapping[str, Any],
    component_root: str | pathlib.Path,
    *,
    signing_private_key: bytes | None = None,
    signing_key_password: bytes | None = None,
) -> pathlib.Path:
    """Create a deterministic signed or unsigned bundle from a template.

    Component ``size`` and ``sha256`` fields are replaced from their source
    files before the canonical JSON manifest is signed. Partition-table,
    bootloader, and Recovery inputs are padded with erased bytes to their
    complete protected Flash ranges so component digests match device
    readback. A Recovery bundle carries only that component; its explicit
    target-layout precondition is supplied separately in the manifest.
    """

    document = json.loads(json.dumps(manifest))
    mapping = _mapping(document, "manifest")
    values = mapping.get("components")
    if not isinstance(values, list) or not values:
        raise ValueError("manifest template requires components")
    root = pathlib.Path(component_root).resolve()
    component_data: dict[str, bytes] = {}
    partition_table_sha256: str | None = None
    partition_items = [
        item
        for item in values
        if isinstance(item, dict) and item.get("kind") == "partition_table"
    ]
    if len(partition_items) > 1:
        raise ValueError("manifest may contain only one partition table")
    partition_offset = (
        _bounded_uint(
            partition_items[0].get("target_offset"),
            "partition-table target_offset",
            0xFFFFFFFF,
        )
        if partition_items
        else None
    )
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise TypeError(f"components[{index}] must be an object")
        item = value
        filename = _safe_member_name(item.get("file"), "component file")
        if filename in component_data:
            raise ValueError(f"component file {filename!r} is duplicated")
        path = (root / filename).resolve()
        if path.parent != root or not path.is_file():
            raise ValueError(f"component source {filename!r} does not exist")
        data = path.read_bytes()
        if not data or len(data) > MAX_COMPONENT_BYTES:
            raise ValueError(f"component source {filename!r} has an invalid size")
        kind = item.get("kind")
        if kind == "partition_table":
            if len(data) > PARTITION_TABLE_REGION_BYTES:
                raise ValueError("partition-table image exceeds its 4 KiB sector")
            data = data.ljust(PARTITION_TABLE_REGION_BYTES, b"\xff")
        elif kind == "bootloader":
            if partition_offset is None:
                raise ValueError(
                    "bootloader component requires a partition-table component"
                )
            bootloader_offset = _bounded_uint(
                item.get("target_offset"), "bootloader target_offset", 0xFFFFFFFF
            )
            protected_size = partition_offset - bootloader_offset
            if protected_size <= 0 or protected_size > MAX_COMPONENT_BYTES:
                raise ValueError("bootloader protected range is invalid")
            if len(data) > protected_size:
                raise ValueError("bootloader image exceeds its protected range")
            data = data.ljust(protected_size, b"\xff")
        elif kind == "recovery":
            protected_size = _bounded_uint(
                item.get("size"), "recovery protected size", MAX_COMPONENT_BYTES
            )
            if protected_size == 0 or protected_size % 0x1000:
                raise ValueError(
                    "recovery protected size must be a non-zero 4 KiB multiple"
                )
            if len(data) > protected_size:
                raise ValueError("recovery image exceeds its protected range")
            data = data.ljust(protected_size, b"\xff")
        item["size"] = len(data)
        item["sha256"] = hashlib.sha256(data).hexdigest()
        if item.get("kind") == "partition_table":
            partition_table_sha256 = str(item["sha256"])
        component_data[filename] = data
    if partition_table_sha256 is not None:
        document["target_layout_sha256"] = partition_table_sha256
    recovery_items = [
        item
        for item in values
        if isinstance(item, dict) and item.get("kind") == "recovery"
    ]
    if recovery_items and len(values) != 1:
        raise ValueError("recovery must be the bundle's only component")
    if recovery_items:
        recovery_layout = _sha256_hex(
            document.get("target_layout_sha256"), "target_layout_sha256"
        )
        if recovery_layout == "0" * 64:
            raise ValueError(
                "recovery bundle requires an explicit target layout SHA-256"
            )
    manifest_bytes = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise ValueError("canonical system-update manifest is too large")
    signature_value = mapping.get("signature")
    if signing_private_key is None:
        if signing_key_password is not None:
            raise ValueError("signing key password requires a signing key")
        if signature_value is not None:
            raise ValueError("unsigned system-update manifest must omit signature")
        key = None
        signature = b""
    else:
        if signature_value is None:
            raise ValueError("signed system-update manifest requires signature metadata")
        key = _load_private_key(signing_private_key, signing_key_password)
        signature = key.sign(manifest_bytes, ec.ECDSA(hashes.SHA256()))
    output = pathlib.Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    timestamp = (1980, 1, 1, 0, 0, 0)
    with tempfile.NamedTemporaryFile(
        prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False
    ) as temporary_handle:
        temporary = pathlib.Path(temporary_handle.name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            members = [(MANIFEST_NAME, manifest_bytes)]
            if signature:
                members.append((SIGNATURE_NAME, signature))
            members.extend(sorted(component_data.items()))
            for name, data in members:
                info = zipfile.ZipInfo(name, timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        public_pem = (
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            if key is not None
            else None
        )
        load_system_update_bundle(temporary, trusted_public_key=public_pem)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


__all__ = [
    "SYSTEM_UPDATE_SCHEMA",
    "SystemUpdateBundle",
    "SystemUpdateComponent",
    "SystemUpdateComponentKind",
    "build_system_update_bundle",
    "load_system_update_bundle",
]
