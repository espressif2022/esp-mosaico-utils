from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from mosaico_cli.cli import build_parser


class GameCommandTests(unittest.TestCase):
    def test_iris_build_needs_only_the_game(self) -> None:
        arguments = build_parser().parse_args([
            "game", "build", "sky_hop", "--target", "iris",
        ])

        self.assertEqual(arguments.project_path, "sky_hop")
        self.assertEqual(arguments.target, "iris")

    def test_build_no_longer_accepts_a_product_root(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([
                "game", "build", "sky_hop", "--target", "iris",
                "--product-root", "/tmp/product",
            ])


if __name__ == "__main__":
    unittest.main()
