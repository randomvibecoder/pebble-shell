from __future__ import annotations

import os
import sys
from pathlib import Path


def pytest_configure() -> None:
    os.environ["PEBBLE_SHELL_DISABLE_DOTENV"] = "1"
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
