from __future__ import annotations

import os
from pathlib import Path

from pebble_shell.cli import _load_env_file, _resolve_env_file
from pebble_shell.config import Settings


def test_resolve_env_file_prefers_explicit_file(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "custom.env"
    home_env = tmp_path / ".pebble-shell" / ".env"
    home_env.parent.mkdir()
    explicit.write_text("OPENAI_API_KEY=explicit\n", encoding="utf-8")
    home_env.write_text("OPENAI_API_KEY=home\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PEBBLE_HOME", str(home_env.parent))

    assert _resolve_env_file(str(explicit)) == explicit


def test_resolve_env_file_uses_pebble_home_default(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "pebble-home"
    env_file = home / ".env"
    home.mkdir()
    env_file.write_text("OPENAI_API_KEY=home\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PEBBLE_HOME", str(home))

    assert _resolve_env_file(None) == env_file


def test_load_env_file_does_not_override_existing_env(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "OPENAI_API_KEY=from-file",
                "OPENAI_MODEL='quoted-model'",
                "INVALID",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "already-set")
    os.environ.pop("OPENAI_MODEL", None)

    _load_env_file(env_file)

    assert os.environ["OPENAI_API_KEY"] == "already-set"
    assert os.environ["OPENAI_MODEL"] == "quoted-model"
    os.environ.pop("OPENAI_MODEL", None)


def test_settings_uses_pebble_home_env_when_no_local_env(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (home / ".env").write_text(
        "\n".join(
            [
                "OPENAI_MODEL=home-model",
                f"AGENT_WORKSPACE={workspace}",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PEBBLE_SHELL_DISABLE_DOTENV", raising=False)
    monkeypatch.setenv("PEBBLE_HOME", str(home))
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    settings = Settings()

    assert settings.openai_model == "home-model"
    assert settings.agent_workspace == workspace
