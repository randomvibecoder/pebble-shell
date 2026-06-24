FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git jq ripgrep sudo unzip wget xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash agent \
    && mkdir -p /workspace \
    && mkdir -p /home/agent/.config \
    && ln -s /workspace/.config/solana /home/agent/.config/solana \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent \
    && chown -R agent:agent /workspace /app

RUN pip install --upgrade pip \
    && pip install \
        "discord.py>=2.4.0" \
        "fastapi>=0.115.0" \
        "openai>=1.55.0" \
        "pydantic>=2.9.0" \
        "pydantic-settings>=2.6.0" \
        "PyNaCl>=1.5.0" \
        "uvicorn[standard]>=0.32.0"

COPY --chown=agent:agent pyproject.toml README.md AGENTS.md /app/
COPY --chown=agent:agent context /app/context
COPY --chown=agent:agent pebble_shell /app/pebble_shell
RUN pip install --no-deps .

EXPOSE 8080 8081 8082 8083 8084 8085 4001 4002

USER agent

CMD ["python", "-m", "pebble_shell"]
