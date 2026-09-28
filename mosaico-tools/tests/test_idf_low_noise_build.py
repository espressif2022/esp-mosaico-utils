from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills/idf-low-noise-build/scripts/idf_low_noise_build.py"
)
SPEC = importlib.util.spec_from_file_location("idf_low_noise_build", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class IdfLowNoiseBuildTests(unittest.TestCase):
    def test_preview_target_adds_preview_before_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "sdkconfig.defaults").write_text(
                'CONFIG_IDF_TARGET="esp32s31"\n', encoding="utf-8"
            )
            self.assertEqual(
                MODULE.idf_action_arguments(project, "build"),
                ["--preview", "build"],
            )

    def test_project_description_artifacts_are_relative_to_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            build = project / "build"
            build.mkdir()
            (build / "game.bin").write_bytes(b"game")
            (build / "game.elf").write_bytes(b"elf")
            (build / "ota_data_initial.bin").write_bytes(b"ota")
            (build / "project_description.json").write_text(
                json.dumps({"app_bin": "game.bin", "app_elf": "game.elf"}),
                encoding="utf-8",
            )

            artifacts = MODULE.collect_artifacts(project)

            self.assertEqual(Path(artifacts[0]["path"]), build / "game.bin")
            self.assertEqual(Path(artifacts[1]["path"]), build / "game.elf")
            self.assertEqual(len(artifacts), 3)


if __name__ == "__main__":
    unittest.main()
