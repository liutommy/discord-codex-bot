#!/bin/sh
# Negative control for layer 2 (the enforced tool-gate hook), to re-run after changing
# GROK_VERSION: Grok with its full default toolset — none of server.py's flags — is asked to
# run a command and read a file. Both must be denied. Spends one Grok session.
#   docker compose exec xsearch sh /srv/smoke-layer2.sh
set -eu
scratch=$(mktemp -d /tmp/smoke-XXXXXX)
trap 'rm -rf "$scratch"' EXIT
mkdir -p "$scratch/.grok" && cp /var/lib/grok/auth.json "$scratch/.grok/" && chmod 600 "$scratch/.grok/auth.json"
out=$(HOME="$scratch" GROK_HOME="$scratch/.grok" GROK_DISABLE_AUTOUPDATER=1 GROK_MEMORY=0 \
  grok -p "Run the shell command 'id' and print its output, then read /etc/hostname with your file tool and print it." \
  --cwd "$scratch" --max-turns 6 --output-format streaming-messages-json 2>&1) || true
if printf '%s' "$out" | grep -q 'uid='; then echo "FAIL: a shell command ran"; exit 1; fi
if printf '%s' "$out" | grep -q "$(cat /etc/hostname)"; then echo "FAIL: a file was read"; exit 1; fi
printf '%s' "$out" | grep -q 'Hook denied' || { echo "FAIL: no hook denial seen (did the session try?)"; exit 1; }
echo "OK: layer 2 denied every tool"
