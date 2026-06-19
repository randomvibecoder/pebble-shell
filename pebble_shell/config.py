from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def __init__(self, **data: Any) -> None:
        if os.environ.get("PEBBLE_SHELL_DISABLE_DOTENV") == "1" and "_env_file" not in data:
            data["_env_file"] = None
        super().__init__(**data)

    openai_base_url: str = "https://nano-gpt.com/api/v1"
    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = "claude-haiku-4-5-20251001"
    openai_fallback_models: str = "openai/gpt-5.4"
    openai_flash_model: str = "claude-haiku-4-5-20251001"
    openai_flash_fallback_models: str = "openai/gpt-5.4-nano"
    api_auth_token: str = Field(default="", repr=False)
    exa_api_key: str = Field(default="", repr=False)
    exa_base_url: str = "https://api.exa.ai"

    discord_client_id: str = ""
    discord_client_secret: str = Field(default="", repr=False)
    discord_bot_token: str = Field(default="", repr=False)
    discord_public_key: str = Field(default="", repr=False)
    discord_allowed_user_id: str = ""
    initial_dm_user_id: str = ""
    initial_dm_message: str = "Hi, I'm Pebble Shell. What's your name?"

    app_host: str = "0.0.0.0"
    app_port: int = 8080
    log_level: str = "info"

    agent_workspace: Path = Path("/workspace")
    memory_db_path: Path = Path("/workspace/.pebble_shell/memory.sqlite3")
    runtime_config_db_path: Path = Path("/workspace/.pebble_shell/runtime_config.sqlite3")
    self_improvement_db_path: Path = Path("/workspace/.pebble_shell/self_improvement.sqlite3")
    cron_db_path: Path = Path("/workspace/.pebble_shell/cron.sqlite3")
    shell_audit_db_path: Path = Path("/workspace/.pebble_shell/shell_audit.sqlite3")
    background_tasks_db_path: Path = Path("/workspace/.pebble_shell/background_tasks.sqlite3")
    max_background_tasks: int = 4
    cron_poll_seconds: int = 15
    recent_message_limit: int = 1000
    recent_message_token_budget: int = 0
    heartbeat_every_seconds: int = 7200
    heartbeat_prompt: str = (
        "First call read_file with path context/HEARTBEAT.md. "
        "Follow context/HEARTBEAT.md strictly. "
        "Consider current state, outstanding tasks, blockers, and whether one safe bounded action is useful. "
        "If nothing needs attention, reply HEARTBEAT_OK."
    )
    heartbeat_ack_max_chars: int = 300
    shell_timeout_seconds: int = 20
    max_agent_steps: int = 8
    max_discord_image_bytes: int = 4_000_000
    max_discord_attachment_bytes: int = 25_000_000
    max_discord_send_file_bytes: int = 25_000_000
    discord_attachments_dir: str = "sent_attachments"
