# Bare-Metal Installation

Bare-metal installation runs Pebble Shell directly on a host instead of inside Docker. Use this when you intentionally want Pebble to administer that machine directly. Docker remains the safer default because bare metal gives Pebble host-level blast radius.

The simple bare-metal path is:

```bash
curl -fsSL https://raw.githubusercontent.com/randomvibecoder/pebble-shell/main/install | bash
pebble serve
```

`pebble serve` runs Pebble in the foreground. If you close that terminal, Pebble stops. To keep it running over SSH, run `pebble serve` inside your own `tmux`, `screen`, or process manager.

## Prerequisites

- Linux or macOS
- `bash`, `curl`, `git`, and `python3`
- An OpenAI-compatible API key for `OPENAI_API_KEY`
- Optional Discord bot/application credentials

Ubuntu/Debian prerequisites:

```bash
sudo apt-get update
sudo apt-get install -y bash ca-certificates curl git python3 python3-venv python3-pip
```

macOS prerequisites:

```bash
xcode-select --install
brew install python git
```

## Install

Run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/randomvibecoder/pebble-shell/main/install | bash
```

The installer creates:

```text
~/.pebble-shell/
  app/        # git checkout
  venv/       # Python virtual environment
  workspace/  # Pebble's working directory, context files, SQLite state
  .env        # local configuration and secrets
```

It also writes a `pebble` wrapper to `~/.local/bin/pebble`.

If `~/.local/bin` is not in your PATH, either add it or run:

```bash
~/.local/bin/pebble serve
```

## Configure

Edit the generated env file:

```bash
nano ~/.pebble-shell/.env
```

Set at least:

```text
OPENAI_API_KEY=...
```

For Discord gateway messages, also set:

```text
DISCORD_BOT_TOKEN=...
DISCORD_ALLOWED_USER_ID=...
INITIAL_DM_USER_ID=...
```

Optional web search:

```text
EXA_API_KEY=...
```

## Serve

Run Pebble in the foreground:

```bash
pebble serve
```

Check health from another terminal:

```bash
curl http://localhost:8080/health
```

To keep Pebble running after disconnecting from SSH, use tmux:

```bash
tmux new -s pebble
pebble serve
```

Detach with `Ctrl-b d`, then reattach later:

```bash
tmux attach -t pebble
```

## Optional: Create a Dedicated User

On a VPS, you can create a dedicated user if you do not want Pebble running as your login account:

```bash
sudo useradd --create-home --shell /bin/bash pebble
sudo -iu pebble
curl -fsSL https://raw.githubusercontent.com/randomvibecoder/pebble-shell/main/install | bash
nano ~/.pebble-shell/.env
pebble serve
```

Optional full host administration:

```bash
echo 'pebble ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/pebble
sudo chmod 0440 /etc/sudoers.d/pebble
```

Only add passwordless sudo if you want Pebble to control the host. Without it, shell commands run with that user's normal permissions.

## Optional: systemd Service

Use systemd only when you want Pebble to start on boot and restart after crashes.

Example service for the dedicated `pebble` user:

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
Environment=PEBBLE_HOME=/home/pebble/.pebble-shell
ExecStart=/home/pebble/.local/bin/pebble serve
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
pebble-shell-discord-register --guild-id YOUR_TEST_GUILD_ID
```

Print an invite URL:

```bash
pebble-shell-discord-register --print-invite
```

## Updating

Run the installer again. It updates `~/.pebble-shell/app`, reinstalls the venv package, and keeps your existing `.env`:

```bash
curl -fsSL https://raw.githubusercontent.com/randomvibecoder/pebble-shell/main/install | bash
```

If Pebble is running, stop and restart `pebble serve` after updating.

## Resetting State

This deletes Pebble's workspace, memory, databases, uploaded files, and generated projects:

```bash
rm -rf ~/.pebble-shell/workspace
mkdir -p ~/.pebble-shell/workspace
```

## Safety Notes

Bare metal is not Docker-isolated. If the account running Pebble has passwordless sudo, Pebble can modify the host. Keep backups, restrict firewall exposure, protect `~/.pebble-shell/.env`, and use a dedicated VPS if you want Pebble to have full host control.
