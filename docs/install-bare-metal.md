# Bare-Metal VPS Installation

Bare-metal installation runs Pebble Shell directly on a Linux host instead of inside Docker. Use this when you intentionally want Pebble to administer the VPS itself. Docker remains the safer default because bare metal gives Pebble host-level blast radius.

This guide targets Ubuntu 24.04.

## Prerequisites

- Ubuntu 24.04 VPS
- A non-root login user with sudo
- An OpenAI-compatible API key for `OPENAI_API_KEY`
- Optional Discord bot/application credentials

Update the system and install packages:

```bash
sudo apt-get update
sudo apt-get install -y \
  bash ca-certificates curl git jq ripgrep sudo unzip wget xz-utils \
  python3 python3-venv python3-pip
```

## Create the Pebble User and Directories

Create a dedicated user:

```bash
sudo useradd --create-home --shell /bin/bash pebble
```

Create application, configuration, and workspace directories:

```bash
sudo mkdir -p /opt/pebble-shell
sudo mkdir -p /etc/pebble-shell
sudo mkdir -p /var/lib/pebble-shell/workspace
sudo chown -R pebble:pebble /opt/pebble-shell /var/lib/pebble-shell
sudo chmod 750 /etc/pebble-shell
```

Optional full host administration:

```bash
echo 'pebble ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/pebble
sudo chmod 0440 /etc/sudoers.d/pebble
```

Only add passwordless sudo if you want Pebble to control the VPS. Without it, shell commands run with the `pebble` user's normal permissions.

## Install Pebble Shell

Clone and install the app as the `pebble` user:

```bash
sudo -iu pebble
git clone https://github.com/randomvibecoder/pebble-shell.git /opt/pebble-shell
cd /opt/pebble-shell
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -e .
exit
```

## Configure Environment

Create `/etc/pebble-shell/pebble-shell.env`:

```bash
sudo tee /etc/pebble-shell/pebble-shell.env >/dev/null <<'EOF'
OPENAI_BASE_URL=https://nano-gpt.com/api/v1
OPENAI_API_KEY=replace-me
OPENAI_MODEL=claude-haiku-4-5-20251001
OPENAI_FALLBACK_MODELS=openai/gpt-5.4
OPENAI_FLASH_MODEL=claude-haiku-4-5-20251001
OPENAI_FLASH_FALLBACK_MODELS=openai/gpt-5.4-nano

API_AUTH_TOKEN=choose-a-local-admin-token
EXA_API_KEY=
EXA_BASE_URL=https://api.exa.ai

DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
DISCORD_PUBLIC_KEY=
DISCORD_BOT_TOKEN=
DISCORD_ALLOWED_USER_ID=
INITIAL_DM_USER_ID=
INITIAL_DM_MESSAGE=Hi, I'm Pebble Shell. What's your name?

APP_HOST=0.0.0.0
APP_PORT=8080
AGENT_WORKSPACE=/var/lib/pebble-shell/workspace
MEMORY_DB_PATH=/var/lib/pebble-shell/workspace/.pebble_shell/memory.sqlite3
RUNTIME_CONFIG_DB_PATH=/var/lib/pebble-shell/workspace/.pebble_shell/runtime_config.sqlite3
EVENT_HOOKS_DB_PATH=/var/lib/pebble-shell/workspace/.pebble_shell/event_hooks.sqlite3
CRON_DB_PATH=/var/lib/pebble-shell/workspace/.pebble_shell/cron.sqlite3
SHELL_AUDIT_DB_PATH=/var/lib/pebble-shell/workspace/.pebble_shell/shell_audit.sqlite3
BACKGROUND_TASKS_DB_PATH=/var/lib/pebble-shell/workspace/.pebble_shell/background_tasks.sqlite3

MAX_BACKGROUND_TASKS=4
CRON_POLL_SECONDS=15
RECENT_MESSAGE_LIMIT=1000
RECENT_MESSAGE_TOKEN_BUDGET=0
HEARTBEAT_EVERY_SECONDS=7200
HEARTBEAT_ACK_MAX_CHARS=300
SHELL_TIMEOUT_SECONDS=20
MAX_AGENT_STEPS=200
MAX_DISCORD_IMAGE_BYTES=4000000
MAX_DISCORD_ATTACHMENT_BYTES=25000000
MAX_DISCORD_SEND_FILE_BYTES=25000000
DISCORD_ATTACHMENTS_DIR=sent_attachments
EOF
sudo chown root:pebble /etc/pebble-shell/pebble-shell.env
sudo chmod 640 /etc/pebble-shell/pebble-shell.env
```

Edit the file and replace placeholder secrets:

```bash
sudoedit /etc/pebble-shell/pebble-shell.env
```

## Create the systemd Service

Create `/etc/systemd/system/pebble-shell.service`:

```bash
sudo tee /etc/systemd/system/pebble-shell.service >/dev/null <<'EOF'
[Unit]
Description=Pebble Shell agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pebble
Group=pebble
WorkingDirectory=/opt/pebble-shell
EnvironmentFile=/etc/pebble-shell/pebble-shell.env
ExecStart=/opt/pebble-shell/.venv/bin/python -m pebble_shell
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

Start Pebble:

```bash
sudo systemctl daemon-reload
sudo systemctl enable pebble-shell
sudo systemctl start pebble-shell
```

Check status and logs:

```bash
sudo systemctl status pebble-shell
sudo journalctl -u pebble-shell -f
```

Check health:

```bash
curl http://localhost:8080/health
```

## Ports and Firewall

Pebble's HTTP service listens on `APP_HOST:APP_PORT`, default `0.0.0.0:8080`. Keep admin/local routes protected with `API_AUTH_TOKEN`.

Typical local development ports:

- `8080` for Pebble HTTP
- `8081-8085` for apps or dev servers Pebble starts
- `4001` for agent-to-agent/libp2p style daemons

Use your VPS firewall to expose only the ports you actually need:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 8080/tcp
sudo ufw allow 4001/tcp
sudo ufw enable
```

For public web access, prefer a reverse proxy with TLS. Do not expose local-only webhook/admin workflows without authentication and network controls.

## Discord Setup

For Discord gateway messages, set:

```bash
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_ID=...
INITIAL_DM_USER_ID=...
```

Register the slash command from the venv:

```bash
sudo -iu pebble
cd /opt/pebble-shell
. .venv/bin/activate
pebble-shell-discord-register --guild-id YOUR_TEST_GUILD_ID
exit
```

Print an invite URL:

```bash
sudo -iu pebble /opt/pebble-shell/.venv/bin/pebble-shell-discord-register --print-invite
```

## Updating

```bash
sudo systemctl stop pebble-shell
sudo -iu pebble
cd /opt/pebble-shell
git pull
. .venv/bin/activate
pip install -e .
exit
sudo systemctl start pebble-shell
```

## Resetting State

This deletes Pebble's workspace, memory, databases, uploaded files, and generated projects:

```bash
sudo systemctl stop pebble-shell
sudo rm -rf /var/lib/pebble-shell/workspace
sudo mkdir -p /var/lib/pebble-shell/workspace
sudo chown -R pebble:pebble /var/lib/pebble-shell/workspace
sudo systemctl start pebble-shell
```

## Safety Notes

Bare metal is not Docker-isolated. If the `pebble` user has passwordless sudo, Pebble can modify the VPS. Keep backups, restrict firewall exposure, protect `/etc/pebble-shell/pebble-shell.env`, and use a dedicated VPS if you want Pebble to have full host control.
