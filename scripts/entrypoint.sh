#!/bin/sh
set -eu

# The named volume keeps whatever config.toml it was created with; refresh it from the image so the
# checked-in policy file stays the single source of truth.
cp /opt/discord-codex/config.toml "${CODEX_HOME}/config.toml"

missing=""
for name in DISCORD_TOKEN DISCORD_APPLICATION_ID ALLOWED_GUILD_IDS; do
  eval "value=\${$name:-}"
  if [ -z "$value" ]; then
    missing="$missing $name"
  fi
done

if [ -n "$missing" ]; then
  echo "discord-codex-bot is waiting for configuration; missing:$missing"
  exec python -c "import time; time.sleep(2147483647)"
fi

exec python -m discord_codex_bot
