ARG UV_VERSION=0.10.0

FROM node:22-bookworm-slim AS node-runtime
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-runtime
FROM python:3.12-slim-bookworm

ARG CODEX_VERSION=0.153.4

COPY --from=node-runtime /usr/local/ /usr/local/
COPY --from=uv-runtime /uv /uvx /bin/

RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=1000:1000 discord_codex_bot ./discord_codex_bot
COPY --chown=1000:1000 scripts ./scripts
COPY --chown=1000:1000 workspace /workspace
COPY --chown=1000:1000 config/codex-config.toml /opt/discord-codex/config.toml
COPY --chown=1000:1000 config/output-style.md /opt/discord-codex/output-style.md

RUN mkdir -p /var/lib/codex \
    && chown -R 1000:1000 /var/lib/codex /workspace /app \
    && chmod 0555 /app/scripts/entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    CODEX_HOME=/var/lib/codex \
    CODEX_WORKSPACE=/workspace \
    PYTHONUNBUFFERED=1

USER 1000:1000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
