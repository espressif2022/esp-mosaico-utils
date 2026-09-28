"""Check the boot splash's wire image, board routing and bounded failures."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEADERS = (
    "esp_efuse.h", "esp_efuse_table.h", "esp_log.h", "esp_rom_gpio.h",
    "esp_rom_sys.h", "hal/gpio_ll.h", "hal/spi_ll.h",
    "soc/efuse_reg.h", "soc/gpio_sig_map.h", "soc/soc.h",
    "soc/lp_system_reg.h",
)


def build_splash(tmp_path, te, virtual):
    compiler = shutil.which("cc") or shutil.which("clang")
    assert compiler, "A host C compiler is required"
    for name in HEADERS:
        header = tmp_path / name
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text('#include "sdk.h"\n')
    (tmp_path / "sdkconfig.h").write_text(
        f"#define CONFIG_BSP_CO5300_ENABLE_TE {te}\n#define CONFIG_EFUSE_VIRTUAL {virtual}\n"
    )
    splash = ROOT / "firmware/recovery/bootloader_components/main"
    shutil.copy(splash / "mosaico_boot_splash.c", tmp_path / "splash_under_test.c")
    executable = tmp_path / "splash"
    subprocess.run([
        compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
        "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-pie", "-no-pie",
        "-I", str(tmp_path), "-I", str(ROOT / "tests/host_boot_splash"), "-I", str(splash),
        str(ROOT / "tests/host_boot_splash/main.c"), "-o", str(executable),
    ], check=True, capture_output=True)
    return executable


@pytest.mark.skipif(sys.platform != "linux", reason="Linux ASan/UBSan host fixture")
@pytest.mark.parametrize("te,virtual", [(0, 0), (1, 0), (1, 1)])
def test_splash_pixels_and_failure_guards(tmp_path, te, virtual):
    executable = build_splash(tmp_path, te, virtual)
    # SHA-256 of the pre-optimization SPI trace: line count, CS hold, length,
    # and payload for every transaction, including all 480x480 RGB565 pixels.
    expected = {0: "e1cecc97b59474acce16ff839dbca807896ac74862a00993c959afe94b3f98ec", 1: "30b47e72381984144dfa321abd2347568688cd2fe9610ae207b4a92c686fcd73"}
    for version in (0x100, 0x101, 0x102, 0xA5A50102):
        result = subprocess.run([str(executable), str(version), "0"], check=True, capture_output=True)
        assert hashlib.sha256(result.stdout).hexdigest() == expected[te]
        # Fail in panel setup, black clear, or orange wordmark drawing.
        for fail_at in (1, 100, 7218):
            subprocess.run([str(executable), str(version), str(fail_at)], check=True, capture_output=True)
    for unknown in (0, 0x103, 0x200, 0xffff):
        result = subprocess.run([str(executable), str(unknown), "0"], check=True, capture_output=True)
        assert not result.stdout
