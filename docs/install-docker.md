# Docker Installation

Docker is the recommended way to run Pebble Shell. It keeps Pebble's shell access, workspace, databases, generated files, and dev servers inside a container.

## Prerequisites

- Docker Engine
- Docker Compose plugin
- Git
- An OpenAI-compatible API key for `OPENAI_API_KEY`

Check Docker:

```bash
docker --version
docker compose version
```

## Install

Clone the repository and create a local environment file:

```bash
git clone https://github.com/randomvibecoder/pebble-shell.git
cd pebble-shell
cp .env.example .env
```

Edit `.env` and set at least:

```bash
OPENAI_BASE_URL=https://nano-gpt.com/api/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=xiaomi/mimo-v2.5:thinking
API_AUTH_TOKEN=choose-a-local-admin-token
```

Optional integrations:

```bash
EXA_API_KEY=your-exa-key
DISCORD_BOT_TOKEN=your-discord-bot-token
DISCORD_ALLOWED_USER_ID=your-discord-user-id
INITIAL_DM_USER_ID=your-discord-user-id
```

Start Pebble:

```bash
docker compose up -d --build
```

Watch logs:

```bash
docker compose logs -f
```

Check health:

```bash
curl http://localhost:8080/health
```

If your `.env` sets `HOST_APP_PORT=18089`, use:

```bash
curl http://localhost:18089/health
```

## Ports

Docker Compose maps:

- `${HOST_APP_PORT:-8080}:8080` for Pebble's HTTP service
- `8081-8085` for app/dev servers Pebble or workers start
- `4001:4001` for agent-to-agent/libp2p style daemons

Host port `4002` is intentionally not mapped by default. If another host daemon, such as `agentchat`, owns `localhost:4002`, Docker cannot also bind it. Use a different host port or move the other daemon.

## Workspace and Data

Pebble sees its workspace at:

```text
/workspace
```

Docker persists that workspace in the `agent-workspace` volume. Pebble stores SQLite databases and secrets under:

```text
/workspace/.pebble_shell/
```

Uploaded Discord attachments are saved under:

```text
/workspace/sent_attachments/
```

Pebble-facing context files are seeded under:

```text
/workspace/context/
```

## Common Operations

Restart after config changes:

```bash
docker compose up -d --build
```

Stop without deleting state:

```bash
docker compose down
```

Fully reset the workspace and conversation state:

```bash
docker compose down -v
docker compose up -d --build
```

Open a shell inside the container:

```bash
docker compose exec agent bash
```

Inspect status when `API_AUTH_TOKEN` is set:

```bash
curl -H "Authorization: Bearer $(grep '^API_AUTH_TOKEN=' .env | cut -d= -f2-)" \
  http://localhost:8080/status
```

Use `http://localhost:18089/status` instead if `.env` sets `HOST_APP_PORT=18089`.

## Discord Setup

For gateway DM/mention handling, set:

```bash
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_ID=...
INITIAL_DM_USER_ID=...
```

Register the slash command if you use Discord HTTP interactions:

```bash
docker compose exec agent pebble-shell-discord-register --guild-id YOUR_TEST_GUILD_ID
```

Print an invite URL:

```bash
docker compose exec agent pebble-shell-discord-register --print-invite
```

## Safety Notes

Pebble has full shell control inside the container, including `sudo`. Docker is the host boundary. Keep `.env` out of git and avoid exposing authenticated routes without `API_AUTH_TOKEN`.
