from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    content: str
    user_id: str | None = None
    channel_id: str | None = None


class ChatResponse(BaseModel):
    content: str
    steps: int


class CronJobRequest(BaseModel):
    name: str
    prompt: str
    channel_id: str | None = None
    every_seconds: int
    enabled: bool = True


class CronEnableRequest(BaseModel):
    enabled: bool


class DiscordAuthor(BaseModel):
    id: str
    username: str | None = None
    bot: bool = False


class DiscordAttachment(BaseModel):
    id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    url: str
    proxy_url: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None


class DiscordMessage(BaseModel):
    id: str | None = None
    channel_id: str = "local-channel"
    author: DiscordAuthor = Field(default_factory=lambda: DiscordAuthor(id="local-user"))
    content: str
    attachments: list[DiscordAttachment] = Field(default_factory=list)


class DiscordGatewayPayload(BaseModel):
    op: int | None = None
    t: str | None = None
    d: dict[str, Any] | None = None
