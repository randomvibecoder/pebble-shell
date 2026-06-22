from __future__ import annotations

import asyncio
import json
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import __version__
from .agent import AgentResponse, CodingAgent, ImageInput
from .config import Settings
from .cron import CronRunner
from .discord_interactions import (
    APPLICATION_COMMAND,
    PING,
    PONG,
    InteractionMessage,
    deferred_interaction_response,
    interaction_to_message,
    send_interaction_followup,
    verify_discord_signature,
)
from .heartbeat import HeartbeatRunner
from .public_sites import list_public_sites
from .schemas import ChatRequest, ChatResponse, CronEnableRequest, CronJobRequest
from .self_improvement import format_webhook_message

app = FastAPI(title="Pebble Shell", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_agent() -> CodingAgent:
    configured_agent = getattr(app.state, "agent", None)
    if configured_agent is not None:
        return configured_agent
    return CodingAgent(get_settings())


def set_agent(agent: CodingAgent) -> None:
    app.state.agent = agent
    get_agent.cache_clear()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/public/{path:path}")
async def public_file(path: str) -> FileResponse:
    settings = get_settings()
    public_root = (settings.agent_workspace / "public").resolve()
    target = _resolve_public_path(public_root, path)
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        raise HTTPException(status_code=404, detail="public file not found")
    return FileResponse(target)


@app.get("/status")
async def status(request: Request) -> dict[str, Any]:
    _require_api_auth(request)
    settings = get_settings()
    agent = get_agent()
    runtime_config = agent.runtime_config.all()
    heartbeat_every_seconds = runtime_config.get("heartbeat_every_seconds") or str(settings.heartbeat_every_seconds)
    hooks = agent.self_improvement.list_hooks()
    jobs = agent.cron.list_jobs()
    recent_improvements = agent.self_improvement.list_records(limit=10)
    recent_webhook_events = agent.self_improvement.list_webhook_events(limit=10)
    return {
        "agent": {
            "version": app.version,
            "workspace": str(settings.agent_workspace),
            "max_agent_steps": settings.max_agent_steps,
            "single_threaded": False,
            "max_background_tasks": settings.max_background_tasks,
        },
        "model": {
            "base_url": settings.openai_base_url,
            "current": agent.current_model,
            "fallbacks": [model for model in agent.candidate_models() if model != agent.current_model],
            "flash": settings.openai_flash_model,
            "flash_fallbacks": [model for model in agent.flash_candidate_models() if model != settings.openai_flash_model],
        },
        "runtime_config": runtime_config,
        "heartbeat": {
            "every_seconds": int(heartbeat_every_seconds),
            "ack_max_chars": settings.heartbeat_ack_max_chars,
        },
        "discord": {
            "client_id": settings.discord_client_id,
            "gateway_enabled": bool(settings.discord_bot_token),
            "interactions_enabled": bool(settings.discord_public_key),
            "client_secret_configured": bool(settings.discord_client_secret),
        },
        "security": {
            "api_auth_enabled": bool(settings.api_auth_token),
            "shell_policy": "all_commands_allowed_in_container",
        },
        "public_sites": list_public_sites(settings.agent_workspace),
        "processes": agent.tools.processes.list(),
        "background_tasks": {
            "max_active": settings.max_background_tasks,
            "active_count": agent.background_store.count_active(),
            "recent": agent.background_store.list_jobs(limit=10),
        },
        "cron": {
            "job_count": len(jobs),
            "enabled_job_count": sum(1 for job in jobs if job["enabled"]),
            "recent_run_count": len(agent.cron.list_runs(limit=10)),
        },
        "self_improvement": {
            "webhook_hook_count": len(hooks),
            "recent_webhook_events": recent_webhook_events,
            "recent_improvements": recent_improvements,
        },
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(chat_request: ChatRequest, request: Request) -> ChatResponse:
    _require_api_auth(request)
    return await _run_user_message_or_500(chat_request.content)


@app.post("/discord/interactions")
async def discord_interactions(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    body = await request.body()
    settings = get_settings()
    if not settings.discord_public_key:
        raise HTTPException(status_code=503, detail="DISCORD_PUBLIC_KEY is required for Discord interactions")
    verified = verify_discord_signature(
        settings.discord_public_key,
        request.headers.get("x-signature-ed25519"),
        request.headers.get("x-signature-timestamp"),
        body,
    )
    if not verified:
        raise HTTPException(status_code=401, detail="invalid request signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    interaction_type = payload.get("type")
    if interaction_type == PING:
        return {"type": PONG}
    if interaction_type != APPLICATION_COMMAND:
        raise HTTPException(status_code=400, detail=f"Unsupported interaction type: {interaction_type}")

    message = interaction_to_message(payload)
    _require_allowed_discord_user(message.author_id)
    interaction_token = str(payload.get("token") or "")
    if not interaction_token:
        raise HTTPException(status_code=400, detail="Discord interaction token is required")
    background_tasks.add_task(_run_interaction_followup, message, interaction_token)
    return deferred_interaction_response()


async def _run_interaction_followup(message: InteractionMessage, interaction_token: str) -> None:
    try:
        if _is_background_agents_command(message.content):
            content = await _background_agents_yaml(message.content)
        else:
            response = await _run_user_message_response(message.content)
            content = response.content
    except Exception as exc:
        content = f"Agent failed while handling the Discord interaction: {exc}"
    await asyncio.to_thread(send_interaction_followup, get_settings().discord_client_id, interaction_token, content)


async def _run_user_message_or_500(
    content: str,
    images: list[ImageInput] | None = None,
) -> ChatResponse:
    try:
        response = await _run_user_message_response(content, images)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ChatResponse(content=response.content, steps=response.steps)


async def _run_user_message_response(
    content: str,
    images: list[ImageInput] | None = None,
) -> AgentResponse:
    agent = get_agent()
    return await agent.run_user_message(content, images=images)


def _is_background_agents_command(content: str) -> bool:
    return content.strip().split(maxsplit=1)[0] in {"/background_agents", "/background-agents", "/bg"}


async def _background_agents_yaml(content: str) -> str:
    limit, status = _parse_background_agents_args(content)
    agent = get_agent()
    agent.bind_background_loop()
    yaml = await agent.background_tasks.status_yaml(limit=limit, status=status)
    return f"```yaml\n{yaml}\n```"


def _parse_background_agents_args(content: str) -> tuple[int, str | None]:
    limit = 10
    status: str | None = None
    for part in content.strip().split()[1:]:
        if part.isdigit():
            limit = int(part)
            continue
        key, sep, value = part.partition("=")
        if not sep:
            key, sep, value = part.partition(":")
        key = key.lower().strip()
        value = value.strip()
        if key == "limit" and value.isdigit():
            limit = int(value)
        elif key == "status" and value:
            status = value
    return max(1, min(limit, 100)), status


@app.post("/heartbeat/run", response_model=ChatResponse)
async def heartbeat_run(request: Request) -> ChatResponse:
    _require_api_auth(request)
    result = await HeartbeatRunner(get_agent(), get_settings()).tick()
    return ChatResponse(content=result, steps=0)


@app.get("/cron/jobs")
async def cron_jobs(request: Request) -> dict[str, Any]:
    _require_api_auth(request)
    agent = get_agent()
    return {"jobs": agent.cron.list_jobs(), "runs": agent.cron.list_runs()}


@app.post("/cron/jobs")
async def cron_job_save(cron_request: CronJobRequest, request: Request) -> dict[str, str]:
    _require_api_auth(request)
    agent = get_agent()
    try:
        agent.cron.upsert_job(
            cron_request.name,
            cron_request.prompt,
            cron_request.every_seconds,
            enabled=cron_request.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "saved", "name": cron_request.name}


@app.post("/cron/jobs/{name}/enabled")
async def cron_job_enabled(name: str, enable_request: CronEnableRequest, request: Request) -> dict[str, str | bool]:
    _require_api_auth(request)
    agent = get_agent()
    try:
        agent.cron.set_enabled(name, enable_request.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"name": name, "enabled": enable_request.enabled}


@app.post("/cron/jobs/{name}/run", response_model=ChatResponse)
async def cron_job_run(name: str, request: Request) -> ChatResponse:
    _require_api_auth(request)
    agent = get_agent()
    try:
        content = await CronRunner(agent, agent.cron).run_job(name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ChatResponse(content=content, steps=0)


@app.post("/webhooks/{name}", response_model=ChatResponse)
async def webhook_trigger(
    name: str,
    payload: dict[str, Any],
    request: Request,
    background_tasks: BackgroundTasks,
    background: bool = False,
) -> ChatResponse:
    _require_api_auth(request)
    agent = get_agent()
    hook = agent.self_improvement.get_hook(name)
    if not hook:
        raise HTTPException(status_code=404, detail=f"Unknown webhook hook: {name}")
    if not hook["enabled"]:
        raise HTTPException(status_code=409, detail=f"Webhook hook is disabled: {name}")
    event_id = agent.self_improvement.record_webhook_event(name, payload, background)
    if background:
        background_tasks.add_task(_run_webhook_hook_recorded, name, payload, event_id)
        return ChatResponse(content=f"Webhook hook `{name}` accepted for background processing.", steps=0)

    response = await _run_webhook_hook_recorded(name, payload, event_id)
    return ChatResponse(content=response.content, steps=response.steps)


@app.post("/webhooks/events/{event_id}/replay", response_model=ChatResponse)
async def webhook_event_replay(event_id: int, request: Request, background_tasks: BackgroundTasks, background: bool = True) -> ChatResponse:
    _require_api_auth(request)
    agent = get_agent()
    event = agent.self_improvement.get_webhook_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail=f"Unknown webhook event: {event_id}")
    if background:
        background_tasks.add_task(agent.replay_hook_event, event_id)
        return ChatResponse(content=f"Webhook event `{event_id}` accepted for replay.", steps=0)
    response = await agent.replay_hook_event(event_id)
    return ChatResponse(content=response.content, steps=response.steps)


async def _run_webhook_hook_recorded(name: str, payload: dict[str, Any], event_id: int) -> AgentResponse:
    agent = get_agent()
    agent.self_improvement.mark_webhook_event_processing(event_id)
    try:
        response = await _run_webhook_hook(name, payload)
    except Exception as exc:
        agent.self_improvement.mark_webhook_event_failed(event_id, str(exc))
        raise
    agent.self_improvement.mark_webhook_event_completed(event_id, response.content)
    return response


async def _run_webhook_hook(name: str, payload: dict[str, Any]) -> AgentResponse:
    agent = get_agent()
    hook = agent.self_improvement.get_hook(name)
    if not hook:
        raise ValueError(f"Unknown webhook hook: {name}")
    content = format_webhook_message(name, hook["prompt"], payload)
    return await agent.run_internal_event(content, f"webhook:{name}")


def _require_api_auth(request: Request) -> None:
    token = get_settings().api_auth_token
    if not token:
        return
    header = request.headers.get("authorization", "")
    expected = f"Bearer {token}"
    if not secrets.compare_digest(header, expected):
        raise HTTPException(status_code=401, detail="invalid or missing API auth token")


def _require_allowed_discord_user(user_id: str) -> None:
    allowed = get_settings().discord_allowed_user_id.strip()
    if allowed and str(user_id) != allowed:
        raise HTTPException(status_code=403, detail="discord user is not allowed")


def _resolve_public_path(public_root: Path, path: str) -> Path:
    target = (public_root / path).resolve()
    if target != public_root and public_root not in target.parents:
        raise HTTPException(status_code=404, detail="public file not found")
    if any(part.startswith(".") for part in target.relative_to(public_root).parts):
        raise HTTPException(status_code=404, detail="public file not found")
    return target
