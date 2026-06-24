from __future__ import annotations

from pydantic import BaseModel


class ChatResponse(BaseModel):
    content: str
    steps: int


class WebhookAcceptedResponse(BaseModel):
    event_id: int
    status: str
    content: str
    steps: int = 0


class CronJobRequest(BaseModel):
    name: str
    every_seconds: int
    enabled: bool = True


class CronEnableRequest(BaseModel):
    enabled: bool
