#!/usr/bin/env python3
"""ESP-Iris developer gateway source entrypoint.

This file intentionally runs directly from the component tree; ESP-Iris PC
tooling is not distributed as an installable Python package.
"""

from __future__ import annotations

import pathlib
import sys

TOOLS_DIR = pathlib.Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from iris_gateway.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
