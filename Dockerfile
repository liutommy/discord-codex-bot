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
COPY --chown=1000:1000 config/consolidate-schema.json /opt/discord-codex/consolidate-schema.json
COPY --chown=1000:1000 config/tracking-schema.json /opt/discord-codex/tracking-schema.json
COPY --chown=1000:1000 config/apis.json /opt/discord-codex/apis.json
COPY --chown=1000:1000 permanent /opt/discord-codex/permanent
COPY --chown=1000:1000 persona /opt/discord-codex/persona
COPY --chown=1000:1000 config/agy-settings.json /opt/discord-codex/agy-settings.json
COPY --chown=1000:1000 announce /opt/discord-codex/announce

# Antigravity CLI (second backend). Installed for the runtime user; the installer fetches the
# current release, so the version actually baked is recorded in the image label below and
# self-update is disabled at runtime (AGY_CLI_DISABLE_AUTO_UPDATE).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright /app/.venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R 1000:1000 /opt/ms-playwright \
    && mkdir -p /home/node \
    && HOME=/home/node bash -c 'curl -fsSL https://antigravity.google/cli/install.sh | bash' \
    && /home/node/.local/bin/agy --version \
    && mkdir -p /home/node/.gemini \
    && chown -R 1000:1000 /home/node
# /home/node/.gemini is a named volume; creating it here (owned by 1000) makes Docker seed a fresh
# volume with that ownership instead of root's.

# Two Codex working directories: /workspace = runtime rules + operator persona (persona/*.md,
# gitignored, appended at build time); /workspace-plain = runtime rules only, used when a member
# has set a personal output style.
RUN mkdir -p /var/lib/codex /workspace-plain \
    && cp /workspace/AGENTS.md /workspace-plain/AGENTS.md \
    && for f in /opt/discord-codex/persona/*.md; do \
         case "$f" in */README.md) ;; *) printf '\n\n' >> /workspace/AGENTS.md; cat "$f" >> /workspace/AGENTS.md ;; esac; \
       done \
    && chown -R 1000:1000 /var/lib/codex /workspace /workspace-plain /app \
    && chmod 0555 /app/scripts/entrypoint.sh

ENV PATH="/app/.venv/bin:/home/node/.local/bin:${PATH}" \
    AGY_CLI_DISABLE_AUTO_UPDATE=true \
    AGY_HOME=/home/node \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    CODEX_HOME=/var/lib/codex \
    CODEX_WORKSPACE=/workspace \
    CODEX_WORKSPACE_PLAIN=/workspace-plain \
    PYTHONUNBUFFERED=1

USER 1000:1000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
