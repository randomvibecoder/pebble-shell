from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    content: str


class ChatResponse(BaseModel):
    content: str
    steps: int


class CronJobRequest(BaseModel):
    name: str
    prompt: str
    every_seconds: int
    enabled: bool = True


class CronEnableRequest(BaseModel):
    enabled: bool
