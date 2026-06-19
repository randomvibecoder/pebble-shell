# Pebble Shell

A Docker-isolated coding agent inspired by OpenClaw/Hermes-style workflows. It exposes:

- `POST /chat` for direct testing.
- `POST /discord/interactions` for signed Discord HTTP interactions.
- `POST /webhooks/{name}` for agent-created external hooks.
- `GET /public/{path}` for published static files from `/workspace/public`.
- `GET/POST /cron/jobs` plus `POST /cron/jobs/{name}/run` for scheduled automation.
- `GET /status` for an authenticated runtime snapshot, including active background processes.
- Optional Discord bot gateway support when `DISCORD_BOT_TOKEN` is provided.
- OpenAI-compatible chat completions through `OPENAI_BASE_URL`.
- Optional Exa API web search through `EXA_API_KEY`.
- SQLite-backed conversation history, reactive summaries, and cached `context/MEMORY.md` durable memory.

## Quick Start

Create `.env` from the example and add real secrets:

```bash
cp .env.example .env
```

Run in Docker:

```bash
docker compose up --build
```

Send a transport-neutral chat message:

```bash
curl -s http://localhost:8080/chat \
  -H 'content-type: application/json' \
  -d '{"content": "List the files in your workspace."}' | jq
```

## Discord Notes

Discord client ID and client secret identify the OAuth application, but they do not let a service receive gateway message events by themselves.

There are two production paths:

- HTTP interactions: set `DISCORD_PUBLIC_KEY`, expose `POST /discord/interactions` publicly, and paste that URL into the Discord Developer Portal as the Interactions Endpoint URL. The endpoint verifies `X-Signature-Ed25519` and `X-Signature-Timestamp`, answers Discord `PING` requests, immediately defers `/agent` commands, and posts the agent result back through Discord's interaction follow-up webhook.
- Gateway bot messages: set `DISCORD_BOT_TOKEN`. The bot replies to DMs and mentions through Discord gateway events.

Discord-originated messages are restricted to `DISCORD_ALLOWED_USER_ID` when configured. Gateway messages from other users are ignored; signed HTTP interactions from other users are rejected with `403`.

Gateway messages save every attachment under `/workspace/sent_attachments/{YYYY}/{MM}/{DD}/{upload_id}/...`. Non-image files append `[attached file: ...]` lines to the agent-visible message. Image files append `[attached image file: ...; already included as an image in this message, ...]` and are also passed to vision-capable OpenAI-compatible models as `image_url` message parts. PDFs and other non-image files are saved for normal workspace tool use; they are not read into model context automatically.

Long agent replies are split into Discord-safe message chunks for both interaction follow-ups and gateway replies instead of being silently truncated.

Register the `/agent prompt:` slash command after the environment is configured:

```bash
pebble-shell-discord-register --guild-id YOUR_TEST_GUILD_ID
```

Omit `--guild-id` to register a global command. Guild commands update faster for testing. The registration command uses `DISCORD_BOT_TOKEN` when set, otherwise it uses `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET` with Discord's client-credentials `applications.commands.update` scope.

Print an invite URL:

```bash
pebble-shell-discord-register --print-invite
```

To test proactive Discord DMs, set `DISCORD_BOT_TOKEN` and run:

```bash
pebble-shell-discord-dm --user-id 111111111111111111 --random-number
```

Discord client credentials can register application commands, but sending DMs requires a bot token.

## Environment

See [.env.example](.env.example). Keep real keys out of source control.

`OPENAI_MODEL` is used for normal text turns, Discord image attachments, and `inspect_image`. Set `OPENAI_FALLBACK_MODELS` to a comma-separated list to try alternates if the primary model/provider call fails.

Set `EXA_API_KEY` to enable the `exa_search` tool for external web research.

Set `DISCORD_ALLOWED_USER_ID` to the single Discord user ID Pebble Shell should accept. `MAX_DISCORD_ATTACHMENT_BYTES` limits incoming attachment downloads. `MAX_DISCORD_SEND_FILE_BYTES` limits files sent back to Discord with `send_file_to_user`.

Set `API_AUTH_TOKEN` before exposing local/admin routes. When set, these endpoints require `Authorization: Bearer <token>`:

- `POST /chat`
- `POST /webhooks/{name}`
- `GET /status`
- Cron and heartbeat control endpoints

`GET /health` remains unauthenticated. `POST /discord/interactions` uses Discord Ed25519 request signatures instead of `API_AUTH_TOKEN`.

## Safety Model

The agent process and shell tools run inside the Docker container. File and shell tools are rooted at `AGENT_WORKSPACE` (`/workspace` in Docker). Shell execution has a timeout and is allowed inside the container; Docker isolation is the primary safety boundary.

## V0.0.1 Runtime Model

V0.0.1 uses one user-facing foreground supervisor plus up to four long-running background workers. Foreground requests still serialize through a process-wide async lock so conversation state stays coherent, but background workers run outside that foreground lock.

The foreground can start workers with `background_task_start`, inspect the whole pool with `background_agents_status`, inspect raw details with `background_task_status`, `background_tasks_list`, and `background_task_events`, ask focused questions with `background_task_ask`, send new instructions to running/blocked/needs-attention workers with `background_task_message`, and request cooperative cancellation with `background_task_cancel`. Workers do not have heartbeat behavior and never message the user directly; they emit internal events that wake the foreground supervisor, which decides what to tell the user.

Workers self-check before completion by answering exactly `COMPLETE`, `BLOCKED`, or `NEEDS_MORE_WORK`. `NEEDS_MORE_WORK` keeps the worker running up to a bounded retry cap; `BLOCKED` and repeated incomplete checks keep the job inspectable and messageable for foreground follow-up. `OPENAI_FLASH_MODEL` powers cheap status summaries such as the `recent_activity` column in `background_agents_status`.

Each worker gets a folder at `/workspace/background_jobs/{job_id}/` and is instructed to edit only that folder and `/tmp` unless the foreground prompt explicitly grants another path. This is prompt-policy isolation, not a hard filesystem sandbox. Docker Compose exposes ports `8080-8085` so several background workers can run webdev tests in parallel.

## Self-Improvement

The agent can improve itself through bounded, auditable primitives:

- `skill_save`: persist a new procedural skill under `/workspace/skills`.
- `skill_install`: install a local workspace `SKILL.md`, `.md`, or `.txt` file into `/workspace/skills` after inspecting it.
- `skill_disable` / `skill_enable`: unload or reload a skill without deleting it.
- `skill_delete`: delete a workspace-installed skill. Bundled skills can be disabled but not deleted by the agent.
- `context/MEMORY.md`: durable self-memory maintained with normal file tools.
- `webhook_hook_save`: register a named webhook, for example an email event hook.
- `set_runtime_config`: change safe runtime settings like model or heartbeat interval.
- `cron_job_save`: create scheduled automations with persisted run history.
- `self_improvements_list`: inspect the recent improvement ledger and hooks.

These mechanisms let the agent learn workflows and connect future events without silently rewriting arbitrary core code.

Webhook triggers normally return the agent result. Browser forms can use `POST /webhooks/{name}?background=true` to receive an immediate acknowledgement while the agent handles the payload asynchronously. Every accepted webhook payload is recorded in the self-improvement ledger and appears in `GET /status` as a recent webhook event with receipt, processing status, and a short result or error excerpt.

For browser-testable pages, the agent can call `publish_static_site` to copy a workspace file or directory into `/workspace/public/{name}`. Published files are served by the app at `/public/{name}/...`.

For downloadable artifacts, the agent can call `send_file_to_user` with a workspace-relative path. The active transport adapter sends the file back to the user, for example after compiling a PDF.

For dev servers and other long-running commands, use the background process tools: `process_start`, `processes_list`, `process_status`, `process_logs`, and `process_stop`. `GET /status` also reports active processes so a UI or Discord command can show what is still running.

## Long-Running Operation

The Docker entrypoint starts the HTTP server and the cron runner. It also starts the heartbeat runner when `DISCORD_BOT_TOKEN` is not set; if the Discord gateway bot is enabled, the bot owns heartbeat delivery so alerts can be sent back to Discord. Manual testing can still use `POST /heartbeat/run`.

## Memory

The agent keeps three layers of context:

- A cached `context/MEMORY.md` snapshot for durable self-memory. It is loaded at process start and refreshed after context compaction, not reread every turn.
- Recent exact messages for the primary chat, bounded by both `RECENT_MESSAGE_LIMIT` and `RECENT_MESSAGE_TOKEN_BUDGET`.
- Reactive summaries when a foreground or background model call hits a provider context-length limit.

The system prompt, repository context files from `context/`, relevant skills, cached `context/MEMORY.md`, and newest user request are placed in context. Pebble Shell keeps a large exact message window by default (`RECENT_MESSAGE_LIMIT=1000`, `RECENT_MESSAGE_TOKEN_BUDGET=0`) so the model can use its available context. During an active foreground or background run, Pebble Shell keeps using the full assembled context until the provider returns a context-length error; then it summarizes older non-system conversation/tool history, refreshes `context/MEMORY.md`, keeps the newest exact messages, reappends the current message/tool result, retries, and sends `[compacted]` to the active Discord transport for debugging.

To remember stable preferences or operating notes, the agent edits `context/MEMORY.md` with normal file tools. If `context/MEMORY.md` changes during a turn, the current cached prompt snapshot does not change until compaction or restart, which keeps most prompt prefixes cacheable.

On first contact in the chat, the agent is prompted to ask a few lightweight questions about the user's hobbies, interests, work style, and what it should remember.

## Tool Strategy

The default integration style is CLI-first inside Docker. The agent should discover tools with `--help` or project skills, prefer dry runs and JSON output, and compose commands through normal Unix pipelines. This keeps prompt context small compared with preloading large external tool schemas. MCP-style integrations can still be added later for compliance-sensitive or multi-tenant APIs where governance is worth the extra context overhead.

## Shell Execution

Shell commands are audited in SQLite and run inside the Docker container workspace. For v0.0.1, Pebble allows container-local shell commands, including `sudo`, package installs, and piped install scripts; Docker isolation is the safety boundary.

## Repository Layout

Pebble-facing prompt and context files are grouped under `context/`:

```text
context/
  AGENTS.md
  HEARTBEAT.md
  MEMORY.md
  SKILLS.md
  SOUL.md
  TOOLS.md
  USER.md
```

On startup, Pebble seeds missing workspace copies under `/workspace/context/` so the agent can edit `context/MEMORY.md`, `context/HEARTBEAT.md`, and the other context files with normal workspace tools. Root `AGENTS.md` remains as repository-level guidance for coding tools that look for that conventional filename.
