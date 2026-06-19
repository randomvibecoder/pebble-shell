FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq ripgrep sudo unzip wget xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash agent \
    && mkdir -p /workspace \
    && mkdir -p /ms-playwright \
    && mkdir -p /home/agent/.config \
    && ln -s /workspace/.config/solana /home/agent/.config/solana \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent \
    && chown -R agent:agent /workspace /app /ms-playwright

RUN pip install --upgrade pip \
    && pip install \
        "discord.py>=2.4.0" \
        "fastapi>=0.115.0" \
        "openai>=1.55.0" \
        "playwright>=1.50.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.6.0" \
        "PyNaCl>=1.5.0" \
        "uvicorn[standard]>=0.32.0" \
    && playwright install --with-deps chromium \
    && chown -R agent:agent /ms-playwright

COPY --chown=agent:agent pyproject.toml README.md AGENTS.md SOUL.md USER.md TOOLS.md HEARTBEAT.md SKILLS.md MEMORY.md /app/
COPY --chown=agent:agent pebble_shell /app/pebble_shell
RUN pip install --no-deps .

EXPOSE 8080 8081 8082 8083 8084 8085

USER agent

CMD ["python", "-m", "pebble_shell"]
