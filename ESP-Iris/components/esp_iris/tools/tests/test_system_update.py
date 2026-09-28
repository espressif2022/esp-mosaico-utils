from __future__ import annotations

import hashlib
import inspect
import json
import pathlib
import struct
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jsonschema import validate

from iris_gateway.protocol import Capability, Channel, Frame, SystemUpdateType
from iris_gateway.session import DeviceSession
from iris_gateway.system_update import (
    SYSTEM_UPDATE_SCHEMA,
    SystemUpdateBundle,
    SystemUpdateComponent,
    SystemUpdateComponentKind,
    build_system_update_bundle,
    load_system_update_bundle,
)
from iris_gateway.system_update_transport import SYSTEM_UPDATE_REQUEST_TIMEOUT

PROTOCOL_DIR = pathlib.Path(__file__).resolve().parents[2] / "protocol"


def test_system_update_timeout_covers_large_partition_erase() -> None:
    assert SYSTEM_UPDATE_REQUEST_TIMEOUT == 60.0
    assert (
        inspect.signature(DeviceSession.system_update)
        .parameters["timeout"]
        .default
        == SYSTEM_UPDATE_REQUEST_TIMEOUT
    )


def _keys() -> tuple[bytes, bytes]:
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public


def _manifest(partition_sha: str) -> dict[str, object]:
    return {
        "schema": SYSTEM_UPDATE_SCHEMA,
        "signature": {
            "algorithm": "ecdsa-p256-sha256",
            "key_id": "release-2026",
        },
        "target": {"chip_id": 0x20, "flash_size": 16 * 1024 * 1024},
        "target_layout_sha256": partition_sha,
        "components": [
            {
                "id": 1,
                "kind": "partition_table",
                "target_offset": 0x8000,
                "file": "partition-table.bin",
            },
            {
                "id": 2,
                "kind": "application",
                "target_offset": 0x1A0000,
                "file": "ota_0.bin",
            },
        ],
    }


def _application_image() -> bytes:
    image = bytearray(256)
    image[0] = 0xE9
    image[1] = 1
    struct.pack_into("<H", image, 12, 0x20)
    descriptor = 32
    struct.pack_into("<I", image, descriptor, 0xABCD5432)
    image[descriptor + 16 : descriptor + 21] = b"1.0.0"
    image[descriptor + 48 : descriptor + 59] = b"system-test"
    image[descriptor + 144 : descriptor + 176] = hashlib.sha256(b"elf").digest()
    return bytes(image)


def test_signed_bundle_round_trip_and_component_hashes(tmp_path) -> None:
    private, public = _keys()
    partition = b"partition" + b"\xff" * 55
    application = _application_image()
    (tmp_path / "partition-table.bin").write_bytes(partition)
    (tmp_path / "ota_0.bin").write_bytes(application)
    output = build_system_update_bundle(
        tmp_path / "release.irisfw",
        _manifest("00" * 32),
        tmp_path,
        signing_private_key=private,
    )
    bundle = load_system_update_bundle(output, trusted_public_key=public)
    with zipfile.ZipFile(output) as archive:
        signed_manifest = json.loads(archive.read("manifest.json"))
    schema = json.loads(
        (PROTOCOL_DIR / "system_update_manifest.schema.json").read_text()
    )
    validate(signed_manifest, schema)
    padded_partition = partition.ljust(0x1000, b"\xff")
    assert bundle.target_layout_sha256 == hashlib.sha256(padded_partition).hexdigest()
    assert bundle.components[0].data == padded_partition
    assert [item.kind for item in bundle.components] == [
        SystemUpdateComponentKind.PARTITION_TABLE,
        SystemUpdateComponentKind.APPLICATION,
    ]
    assert bundle.components[1].data == application
    assert bundle.as_dict()["signature_verified"] is True


def test_unsigned_bundle_round_trip_requires_no_key(tmp_path) -> None:
    partition = b"partition" + b"\xff" * 55
    application = _application_image()
    (tmp_path / "partition-table.bin").write_bytes(partition)
    (tmp_path / "ota_0.bin").write_bytes(application)
    manifest = _manifest("00" * 32)
    manifest.pop("signature")
    output = build_system_update_bundle(
        tmp_path / "unsigned.irisfw",
        manifest,
        tmp_path,
    )
    bundle = load_system_update_bundle(output)
    with zipfile.ZipFile(output) as archive:
        assert "manifest.sig" not in archive.namelist()
        unsigned_manifest = json.loads(archive.read("manifest.json"))
    schema = json.loads(
        (PROTOCOL_DIR / "system_update_manifest.schema.json").read_text()
    )
    validate(unsigned_manifest, schema)
    assert bundle.signature == b""
    assert bundle.key_id is None
    assert bundle.as_dict()["signature_verified"] is False
    assert "source_layout_sha256" not in bundle.as_dict()
    assert bundle.as_dict()["release"] is None
    assert bundle.as_dict()["minimum_recovery_version"] is None


@pytest.mark.parametrize("length", [1, 64])
def test_bundle_inspection_preserves_release_and_recovery_version(tmp_path, length) -> None:
    (tmp_path / "partition-table.bin").write_bytes(b"partition")
    (tmp_path / "ota_0.bin").write_bytes(_application_image())
    manifest = _manifest("00" * 32)
    manifest.pop("signature")
    manifest["release"] = "v" * length
    manifest["minimum_recovery_version"] = "r" * length
    output = build_system_update_bundle(tmp_path / "metadata.irisfw", manifest, tmp_path)
    bundle = load_system_update_bundle(output)
    metadata = bundle.as_dict()
    assert metadata["release"] == "v" * length
    assert metadata["minimum_recovery_version"] == "r" * length
    assert json.dumps(metadata, sort_keys=True) == json.dumps(bundle.as_dict(), sort_keys=True)


@pytest.mark.parametrize("field", ["release", "minimum_recovery_version"])
@pytest.mark.parametrize("value", ["", "x" * 65, 123])
def test_bundle_rejects_invalid_release_metadata(tmp_path, field, value) -> None:
    (tmp_path / "partition-table.bin").write_bytes(b"partition")
    (tmp_path / "ota_0.bin").write_bytes(_application_image())
    manifest = _manifest("00" * 32)
    manifest.pop("signature")
    manifest[field] = value
    with pytest.raises((TypeError, ValueError)):
        output = build_system_update_bundle(tmp_path / "metadata.irisfw", manifest, tmp_path)
        load_system_update_bundle(output)


def test_bundle_rejects_obsolete_source_layout_authorization(tmp_path) -> None:
    (tmp_path / "partition-table.bin").write_bytes(b"partition")
    (tmp_path / "ota_0.bin").write_bytes(_application_image())
    manifest = _manifest("00" * 32)
    manifest["source_layout_sha256"] = ["11" * 32]
    manifest.pop("signature")
    with pytest.raises(ValueError, match="unknown fields: source_layout_sha256"):
        build_system_update_bundle(tmp_path / "v1.irisfw", manifest, tmp_path)


def test_bundle_rejects_obsolete_v2_schema_name(tmp_path) -> None:
    (tmp_path / "partition-table.bin").write_bytes(b"partition")
    (tmp_path / "ota_0.bin").write_bytes(_application_image())
    manifest = _manifest("00" * 32)
    manifest["schema"] = "esp-iris-system-update/v2"
    manifest.pop("signature")
    with pytest.raises(ValueError, match="unsupported system-update manifest schema"):
        build_system_update_bundle(tmp_path / "v2.irisfw", manifest, tmp_path)


def test_unsigned_bundle_is_rejected_when_trust_key_is_configured(tmp_path) -> None:
    _, public = _keys()
    (tmp_path / "partition-table.bin").write_bytes(b"partition")
    (tmp_path / "ota_0.bin").write_bytes(_application_image())
    manifest = _manifest("00" * 32)
    manifest.pop("signature")
    output = build_system_update_bundle(
        tmp_path / "unsigned.irisfw",
        manifest,
        tmp_path,
    )
    with pytest.raises(ValueError, match="rejected by trust policy"):
        load_system_update_bundle(output, trusted_public_key=public)


def test_bundle_rejects_untrusted_signature(tmp_path) -> None:
    private, _ = _keys()
    _, other_public = _keys()
    (tmp_path / "partition-table.bin").write_bytes(b"partition")
    (tmp_path / "ota_0.bin").write_bytes(_application_image())
    output = build_system_update_bundle(
        tmp_path / "release.irisfw",
        _manifest("00" * 32),
        tmp_path,
        signing_private_key=private,
    )
    with pytest.raises(ValueError, match="signature is invalid"):
        load_system_update_bundle(output, trusted_public_key=other_public)


def test_bundle_supports_encrypted_offline_signing_key(tmp_path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    password = b"release-key-test-password"
    encrypted_private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (tmp_path / "partition-table.bin").write_bytes(b"partition")
    (tmp_path / "ota_0.bin").write_bytes(_application_image())
    output = build_system_update_bundle(
        tmp_path / "encrypted-key.irisfw",
        _manifest("00" * 32),
        tmp_path,
        signing_private_key=encrypted_private,
        signing_key_password=password,
    )
    assert load_system_update_bundle(output, trusted_public_key=public).key_id == (
        "release-2026"
    )


def test_builder_hashes_complete_bootloader_and_partition_regions(tmp_path) -> None:
    private, public = _keys()
    bootloader = b"bootloader-image"
    partition = b"partition-table"
    (tmp_path / "bootloader.bin").write_bytes(bootloader)
    (tmp_path / "partition-table.bin").write_bytes(partition)
    manifest = _manifest("00" * 32)
    manifest["components"] = [
        {
            "id": 1,
            "kind": "bootloader",
            "target_offset": 0x2000,
            "file": "bootloader.bin",
        },
        {
            "id": 2,
            "kind": "partition_table",
            "target_offset": 0x8000,
            "file": "partition-table.bin",
        },
    ]
    output = build_system_update_bundle(
        tmp_path / "protected-ranges.irisfw",
        manifest,
        tmp_path,
        signing_private_key=private,
    )
    bundle = load_system_update_bundle(output, trusted_public_key=public)
    assert bundle.components[0].data == bootloader.ljust(0x6000, b"\xff")
    assert bundle.components[1].data == partition.ljust(0x1000, b"\xff")
    assert bundle.components[0].target_offset + bundle.components[0].size == (
        bundle.components[1].target_offset
    )


def test_builder_pads_and_validates_recovery_protected_range(tmp_path) -> None:
    recovery = _application_image()
    (tmp_path / "factory.bin").write_bytes(recovery)
    manifest = _manifest("11" * 32)
    manifest.pop("signature")
    manifest["components"] = [
        {
            "id": 1,
            "kind": "recovery",
            "target_offset": 0x20000,
            "size": 0x2000,
            "file": "factory.bin",
        },
    ]
    output = build_system_update_bundle(
        tmp_path / "recovery.irisfw", manifest, tmp_path
    )
    bundle = load_system_update_bundle(output)
    with zipfile.ZipFile(output) as archive:
        built_manifest = json.loads(archive.read("manifest.json"))
        assert set(archive.namelist()) == {"manifest.json", "factory.bin"}
    schema = json.loads(
        (PROTOCOL_DIR / "system_update_manifest.schema.json").read_text()
    )
    validate(built_manifest, schema)
    component = bundle.components[0]
    assert component.kind is SystemUpdateComponentKind.RECOVERY
    assert component.data == recovery.ljust(0x2000, b"\xff")
    assert component.sha256 == hashlib.sha256(component.data).digest()


def test_builder_requires_complete_recovery_protected_size(tmp_path) -> None:
    (tmp_path / "factory.bin").write_bytes(_application_image())
    manifest = _manifest("11" * 32)
    manifest.pop("signature")
    manifest["components"] = [
        {
            "id": 1,
            "kind": "recovery",
            "target_offset": 0x20000,
            "file": "factory.bin",
        },
    ]
    with pytest.raises(TypeError, match="recovery protected size"):
        build_system_update_bundle(tmp_path / "recovery.irisfw", manifest, tmp_path)


def test_builder_requires_sector_aligned_recovery_protected_size(tmp_path) -> None:
    (tmp_path / "factory.bin").write_bytes(_application_image())
    manifest = _manifest("11" * 32)
    manifest.pop("signature")
    manifest["components"] = [
        {
            "id": 1,
            "kind": "recovery",
            "target_offset": 0x20000,
            "size": 0x1800,
            "file": "factory.bin",
        },
    ]
    with pytest.raises(ValueError, match="non-zero 4 KiB multiple"):
        build_system_update_bundle(tmp_path / "recovery.irisfw", manifest, tmp_path)


def test_builder_requires_explicit_recovery_layout_precondition(tmp_path) -> None:
    (tmp_path / "factory.bin").write_bytes(_application_image())
    manifest = _manifest("00" * 32)
    manifest.pop("signature")
    manifest["components"] = [
        {
            "id": 1,
            "kind": "recovery",
            "target_offset": 0x20000,
            "size": 0x2000,
            "file": "factory.bin",
        }
    ]
    with pytest.raises(ValueError, match="explicit target layout SHA-256"):
        build_system_update_bundle(tmp_path / "recovery.irisfw", manifest, tmp_path)


def test_builder_rejects_partition_table_in_recovery_bundle(tmp_path) -> None:
    (tmp_path / "partition-table.bin").write_bytes(b"partition-table")
    (tmp_path / "factory.bin").write_bytes(_application_image())
    manifest = _manifest("11" * 32)
    manifest.pop("signature")
    manifest["components"] = [
        {
            "id": 1,
            "kind": "partition_table",
            "target_offset": 0x8000,
            "file": "partition-table.bin",
        },
        {
            "id": 2,
            "kind": "recovery",
            "target_offset": 0x20000,
            "size": 0x2000,
            "file": "factory.bin",
        },
    ]
    with pytest.raises(ValueError, match="only component"):
        build_system_update_bundle(tmp_path / "recovery.irisfw", manifest, tmp_path)


def test_builder_rejects_mixed_recovery_transaction(tmp_path) -> None:
    (tmp_path / "factory.bin").write_bytes(_application_image())
    (tmp_path / "data.bin").write_bytes(b"data")
    manifest = _manifest("11" * 32)
    manifest.pop("signature")
    manifest["components"] = [
        {
            "id": 1,
            "kind": "recovery",
            "target_offset": 0x20000,
            "size": 0x2000,
            "file": "factory.bin",
        },
        {
            "id": 2,
            "kind": "data",
            "target_offset": 0x30000,
            "file": "data.bin",
        },
    ]
    with pytest.raises(ValueError, match="only component"):
        build_system_update_bundle(tmp_path / "recovery.irisfw", manifest, tmp_path)


def _bundle_for_session() -> SystemUpdateBundle:
    data = b"abc"
    component = SystemUpdateComponent(
        id=3,
        kind=SystemUpdateComponentKind.APPLICATION,
        flags=0,
        target_offset=0x1A0000,
        size=len(data),
        sha256=hashlib.sha256(data).digest(),
        filename="ota_0.bin",
        data=data,
    )
    manifest = json.dumps({"schema": SYSTEM_UPDATE_SCHEMA}).encode()
    return SystemUpdateBundle(
        manifest={"schema": SYSTEM_UPDATE_SCHEMA},
        manifest_bytes=manifest,
        signature=b"",
        manifest_sha256=hashlib.sha256(manifest).digest(),
        key_id=None,
        signature_verified=False,
        chip_id=0x20,
        flash_size=16 * 1024 * 1024,
        target_layout_sha256="22" * 32,
        components=(component,),
    )


def test_session_system_update_encodes_bounded_component_sequence() -> None:
    async def scenario() -> None:
        session = object.__new__(DeviceSession)
        session.info = type(
            "Info", (), {"capabilities": int(Capability.SYSTEM_UPDATE)}
        )()
        calls = 0
        begin_attempts = 0
        operation_id = bytes.fromhex("01" * 16)

        async def ready(timeout: float = 5.0):
            del timeout
            return session.info

        async def request(channel, type_, payload=b"", timeout=10.0):
            nonlocal begin_attempts, calls
            del timeout
            assert channel == Channel.SYSTEM_UPDATE
            if type_ == SystemUpdateType.BEGIN:
                begin_attempts += 1
                if begin_attempts == 1:
                    raise TimeoutError
            if type_ == SystemUpdateType.STATUS:
                return Frame(
                    channel=channel,
                    type=SystemUpdateType.STATUS_RESPONSE,
                    payload=operation_id
                    + struct.pack("<IBBBBIIi", 9, 1, 1, 0, 0, 0, 0, 0),
                )
            calls += 1
            if calls == 1:
                assert type_ == SystemUpdateType.BEGIN
                assert payload[:16] == operation_id
                assert struct.unpack_from("<H", payload, 18)[0] == 0
                return Frame(
                    channel=channel,
                    type=SystemUpdateType.BEGIN_RESPONSE,
                    payload=operation_id + struct.pack("<IHBB", 9, 2, 1, 0),
                )
            if calls == 2:
                assert type_ == SystemUpdateType.COMPONENT_BEGIN
                return Frame(
                    channel=channel,
                    type=SystemUpdateType.COMPONENT_BEGIN_RESPONSE,
                    payload=(
                        operation_id
                        + bytes([3, int(SystemUpdateComponentKind.APPLICATION)])
                        + struct.pack("<HI", 2, 3)
                    ),
                )
            if calls in (3, 4):
                assert type_ == SystemUpdateType.DATA
                offset = 2 if calls == 3 else 3
                return Frame(
                    channel=channel,
                    type=SystemUpdateType.DATA_RESPONSE,
                    payload=operation_id
                    + bytes([3, 0])
                    + struct.pack("<HI", 400 + calls, offset),
                )
            if calls == 5:
                assert type_ == SystemUpdateType.COMPONENT_END
                return Frame(
                    channel=channel,
                    type=SystemUpdateType.COMPONENT_END_RESPONSE,
                    payload=operation_id + bytes([3, 1, 0, 0]) + struct.pack("<i", 0),
                )
            assert calls == 6 and type_ == SystemUpdateType.COMMIT
            return Frame(
                channel=channel,
                type=SystemUpdateType.COMMIT_RESPONSE,
                payload=operation_id + struct.pack("<Ii", 9, 0),
            )

        session._request = request
        session.wait_ready = ready
        result = await session.system_update(
            _bundle_for_session(), operation_id=operation_id
        )
        assert result["bytes"] == 3
        assert result["completion_evidence"] == "commit_response"

    import asyncio

    asyncio.run(scenario())


def test_system_update_inventory_decodes_actual_flash_hashes() -> None:
    async def scenario() -> None:
        session = object.__new__(DeviceSession)
        session.info = type(
            "Info", (), {"capabilities": int(Capability.SYSTEM_INVENTORY)}
        )()

        async def ready(timeout: float = 5.0):
            del timeout
            return session.info

        async def request(channel, type_, payload=b"", timeout=10.0):
            del payload, timeout
            assert (channel, type_) == (
                Channel.SYSTEM_UPDATE,
                SystemUpdateType.INVENTORY,
            )
            response = (
                struct.pack("<II", 7, 4)
                + bytes.fromhex("aa" * 32)
                + bytes.fromhex("bb" * 32)
                + bytes.fromhex("cc" * 16)
                + struct.pack("<i", 0)
            )
            return Frame(
                channel=channel,
                type=SystemUpdateType.INVENTORY_RESPONSE,
                payload=response,
            )

        session._request = request
        session.wait_ready = ready
        inventory = await session.system_update_inventory()
        assert inventory["layout_version"] == 4
        assert inventory["bootloader_sha256"] == "aa" * 32
        assert inventory["partition_table_sha256"] == "bb" * 32
        assert inventory["last_operation_id"] == "cc" * 16

    import asyncio

    asyncio.run(scenario())
