"""Guard the dependency direction and the public compatibility policy."""
import ast
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

UTILS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(UTILS / "mosaico-tools/tools"))

from mosaico_cli.errors import EnvironmentError
from mosaico_cli.gateway import _require_compatible_gateway, run_ota
from mosaico_cli.workspace import WorkspaceConfig


def test_product_only_imports_the_public_iris_host_api():
    package = UTILS / "mosaico-tools/tools/mosaico_cli"
    for file in package.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("iris_gateway"):
                assert node.module == "iris_gateway" and [item.name for item in node.names] == ["client"], file
            if isinstance(node, ast.Import):
                assert all(not item.name.startswith("iris_gateway") for item in node.names), file


def test_default_api_compatibility_is_independent_of_git_revision():
    health = {"gateway_api": {"major": 1}, "capabilities": [], "esp_iris_revision": "other-commit"}
    assert _require_compatible_gateway(health) is health


def test_exact_policy_requires_algorithm_and_content_fingerprint():
    expected = {"algorithm": "sha256-runtime-v1", "fingerprint": "a" * 64}
    health = {"gateway_api": {"major": 1}, "capabilities": [], "source": expected}
    assert _require_compatible_gateway(health, expected_source=expected) is health
    for source in [None, {**expected, "fingerprint": "b" * 64}, {**expected, "algorithm": "unknown"}]:
        with pytest.raises(EnvironmentError, match="was not stopped"):
            _require_compatible_gateway({**health, "source": source}, expected_source=expected)


def test_install_rejects_old_gateway_before_upload():
    with patch("mosaico_cli.gateway.gateway_json", return_value={"capabilities": []}) as request:
        with pytest.raises(EnvironmentError, match="preconditions"):
            run_ota(Mock(), Mock(), device_id="d", image=Path("image.bin"), elf=Path("image.elf"),
                    map_file=Path("image.map"), validation="elf-sha256", timeout=30,
                    preconditions={"recovery_version": "0.1.1"})
    assert [call.args[2:] for call in request.call_args_list] == [("health",)]


def test_canonical_tools_resolve_sibling_recovery():
    work = WorkspaceConfig.__new__(WorkspaceConfig)
    object.__setattr__(work, "tool_root", UTILS / "mosaico-tools")
    assert work.recovery_project == UTILS / "esp-mosaico-recovery/firmware/recovery"
