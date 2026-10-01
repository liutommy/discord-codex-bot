ARG UV_VERSION=0.10.0

FROM node:22-bookworm-slim AS node-runtime
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-runtime

# The repo files the image carries, staged at their final paths and taken into the image by a
# single COPY below.
FROM scratch AS files
COPY discord_codex_bot /app/discord_codex_bot
COPY scripts /app/scripts
# The rules half of AGENTS.md. The Bot composes the working directories from it plus whatever
# persona is in force, at start-up and after every change, so neither needs an image rebuild.
COPY workspace /opt/discord-codex/rules
COPY config/codex-config.toml /opt/discord-codex/config.toml
# The operator's own file is gitignored, so a fresh clone has only the sample; the glob copies
# whichever exist and the last RUN falls back to the sample.
COPY config/output-style*.md /opt/discord-codex/
COPY config/consolidate-schema.json config/harvest-schema.json config/tracking-schema.json \
     config/digest-user-schema.json config/digest-guild-schema.json \
     config/apis.json config/lol-names.json config/clearurls.json config/agy-settings.json \
     /opt/discord-codex/
COPY permanent /opt/discord-codex/permanent
COPY persona /opt/discord-codex/persona
COPY announce /opt/discord-codex/announce

FROM python:3.12-slim-bookworm

ARG CODEX_VERSION=0.156.1

COPY --from=node-runtime /usr/local/ /usr/local/
COPY --from=uv-runtime /uv /uvx /bin/

RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Chromium for the link-fetch fallback. Its version comes from uv.lock alone, so it sits before
# the source COPYs: a code or data change reuses this layer instead of reinstalling it.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright /app/.venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chown -R 1000:1000 /opt/ms-playwright

# The repo files (the files stage above) in one layer: a vfs dockerd stores every layer as a full
# copy of the filesystem, so a dozen small COPYs here would cost a dozen copies of Chromium.
# Copying / also chowns /opt itself; the last RUN gives it back to root.
COPY --from=files --chown=1000:1000 / /

# Antigravity CLI (second backend). Installed for the runtime user; the installer fetches the
# current release at every build and self-update is disabled at runtime
# (AGY_CLI_DISABLE_AUTO_UPDATE). /home/node/.gemini is a named volume; creating it here (owned by
# 1000) makes Docker seed a fresh volume with that ownership instead of root's.
# The Codex working directories live in the named volume, because the persona they are composed
# from can be replaced at runtime and the image's filesystem is read-only.
RUN mkdir -p /home/node \
    && HOME=/home/node bash -c 'curl -fsSL https://antigravity.google/cli/install.sh | bash' \
    && /home/node/.local/bin/agy --version \
    && mkdir -p /home/node/.gemini \
    && chown -R 1000:1000 /home/node \
    && mkdir -p /var/lib/codex \
    && { [ -f /opt/discord-codex/output-style.md ] \
         || cp /opt/discord-codex/output-style.example.md /opt/discord-codex/output-style.md; } \
    && chown -R 1000:1000 /var/lib/codex /opt/discord-codex /app \
    && chown root:root /opt \
    && chmod 0555 /app/scripts/entrypoint.sh

ENV PATH="/app/.venv/bin:/home/node/.local/bin:${PATH}" \
    AGY_CLI_DISABLE_AUTO_UPDATE=true \
    AGY_HOME=/home/node \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    CODEX_HOME=/var/lib/codex \
    CODEX_WORKSPACE=/var/lib/codex/workspace \
    CODEX_WORKSPACE_PLAIN=/var/lib/codex/workspace-plain \
    CODEX_RULES=/opt/discord-codex/rules/AGENTS.md \
    PYTHONUNBUFFERED=1

USER 1000:1000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
