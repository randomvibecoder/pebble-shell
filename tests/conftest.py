from __future__ import annotations

import os


def pytest_configure() -> None:
    os.environ["OPENCODE_AGENT_DISABLE_DOTENV"] = "1"
