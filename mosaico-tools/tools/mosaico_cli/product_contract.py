"""Mosaico normal application policy; never inferred from generic Iris defaults."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import BuildError

# The retained Recovery product owns identity and fixed layout values.
CONTRACT_PATH = Path(__file__).resolve().parents[3] / "esp-mosaico-recovery/product_contract.json"
PRODUCT_CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
COMPATIBILITY = PRODUCT_CONTRACT["compatibility"]



def validate_application_config(build_dir: Path) -> None:
    path = build_dir / "config/sdkconfig.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("effective configuration must be an object")
    except (OSError, ValueError) as error:
        raise BuildError(
            "Effective application configuration is unavailable; rebuild the project.",
            details={"config": str(path)},
        ) from error
    expected = {
        "ESP_IRIS_" + key.upper(): value
        for key, value in COMPATIBILITY.items()
        if key != "chip_target"
    }
    expected.update(
        ESP_IRIS_FIRMWARE_ROLE=1,
        ESP_IRIS_OTA=False,
        ESP_IRIS_OTA_DEFAULT_VIA_RECOVERY=True,
        ESP_IRIS_SYSTEM_INVENTORY=True,
        IDF_TARGET=COMPATIBILITY["chip_target"],
    )
    mismatches = {
        key: {"expected": value, "actual": config.get(key)}
        for key, value in expected.items()
        if config.get(key) != value or type(config.get(key)) is not type(value)
    }
    if mismatches:
        raise BuildError(
            "Application violates the Mosaico firmware contract; correct the effective sdkconfig and rebuild.",
            details={"config": str(path), "mismatches": mismatches},
        )
