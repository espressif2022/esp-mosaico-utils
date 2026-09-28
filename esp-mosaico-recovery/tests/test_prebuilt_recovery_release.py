"""Keep the published Recovery release, image descriptor and manifest in sync."""
import hashlib
import json
from pathlib import Path


def test_prebuilt_recovery_release():
    project = Path(__file__).resolve().parents[1] / "firmware/recovery"
    directory = project / "prebuilt/recovery"
    manifest = json.loads((directory / "manifest.json").read_text())
    assert 'CONFIG_APP_PROJECT_VER="0.1.2"' in (
        project / "sdkconfig.recovery.defaults"
    ).read_text()
    assert manifest["version"] == "0.1.2"
    assert manifest["target"] == "esp32s31"
    assert manifest["initial_boot"]["partition"] == "factory"
    assert manifest["initial_boot"]["expected_mode"] == "recovery"
    offsets = {"bootloader": 0x2000, "partition_table": 0x8000,
               "ota_data": 0x9000, "recovery": 0x20000}
    for name, offset in offsets.items():
        item = manifest["images"][name]
        data = (directory / item["file"]).read_bytes()
        assert int(item["offset"], 0) == offset
        assert len(data) == item["size"]
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
    recovery = (directory / "factory.bin").read_bytes()
    assert recovery[0] == 0xE9
    assert int.from_bytes(recovery[32:36], "little") == 0xABCD5432
    assert recovery[48:80].split(b"\0", 1)[0] == b"0.1.2"
    assert len(recovery) <= 0x1C0000
    assert (directory / "bootloader.bin").stat().st_size <= 0x6000
    assert (directory / "ota_data_initial.bin").read_bytes() == b"\xff" * 0x2000
    # A release update must not silently alter the retained Recovery layout.
    assert manifest["images"]["partition_table"]["sha256"] == (
        "7f638212e0abfc152ff45cce4a3d9ff3c1312f96465a972b5be3b7eb79c8367c"
    )
