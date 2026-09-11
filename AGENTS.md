# AGENTS.md

## Scope

This repository contains a private Discord gateway to a subscription-authenticated Codex CLI.

## Rules

- Keep Discord authorization based on an explicit guild ID allowlist.
- Do not add per-user authorization unless the owner requests it.
- Never commit Discord tokens, Codex credentials, `.env`, `.venv`, or volume contents.
- Keep the Codex model operator-controlled; Discord may only pick the reasoning effort from the
  allowlist in `config.py`, which must match what `codex debug models` reports for that model.
- Keep `/codex` and `@mention` on one shared pipeline (access → validate → queue → `run_codex`).
- Spawn Codex without inheriting Discord secrets.
- Preserve the read-only container and disabled Codex execution tools unless a reviewed use case requires them.
- Use the repository-local uv environment for Python dependencies and tests.
- Use Conventional Commits.
