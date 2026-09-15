#!/usr/bin/env bash
# Daily: regenerate the LoL name tables (scripts/build_lol_names.py) and rebuild the image only
# when they actually changed. A new champion, augment or item arrives with a patch, not daily, so
# most runs end at "unchanged" and the Bot is not restarted for nothing.
#
#   crontab: 0 5 * * * /home/tommy_liu/tommy/discord-codex-bot/scripts/daily_rebuild.sh \
#              >> /home/tommy_liu/tommy/discord-codex-bot/logs/daily-rebuild.log 2>&1
#
# 05:00 Taipei is after the 02:00 memory consolidation (CONSOLIDATE_HOUR). On a change the
# regenerated config/lol-names.json is committed locally (never pushed) so the tree stays clean;
# permanent/topics/*.md are gitignored and only baked. DRY_RUN=1 prints what would happen.
set -euo pipefail
export PATH="/home/tommy_liu/.local/bin:/usr/bin:/bin"
cd "$(dirname "$0")/.."
stamp() { date '+%F %T'; }
digest() { cat config/lol-names.json permanent/topics/*譯名對照.md 2>/dev/null | md5sum | cut -c1-12; }

before=$(digest)
if ! summary=$(UV_CACHE_DIR=.uv-cache uv run python scripts/build_lol_names.py 2>&1); then
    echo "$(stamp) build_lol_names.py failed: ${summary##*$'\n'}"
    exit 1
fi
if [ "$before" = "$(digest)" ]; then
    echo "$(stamp) unchanged ($before); no rebuild"
    exit 0
fi
if [ "${DRY_RUN:-0}" = 1 ]; then
    echo "$(stamp) DRY_RUN: tables changed ($before -> $(digest)); would commit + rebuild"
    exit 0
fi
git add config/lol-names.json
git commit -q -m "chore(names): 每日重產譯名表（$(date +%F)）" -- config/lol-names.json || true
docker compose up -d --build bot >/dev/null
cid=$(docker compose ps -q bot)
for _ in $(seq 1 40); do
    health=$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)
    [ "$health" = healthy ] && break
    sleep 3
done
echo "$(stamp) tables changed -> rebuilt, health=$health; ${summary##*$'\n'}"
