# AGENTS.md

## Scope

This repository contains a private Discord gateway to a subscription-authenticated Codex CLI.

## Rules

- Keep Discord authorization based on an explicit guild ID allowlist.
- Do not add per-user authorization unless the owner requests it.
- Never commit Discord tokens, Codex credentials, `.env`, `.venv`, or volume contents.
- Keep the Codex model and reasoning effort operator-controlled; Discord prompts must not override them.
- Spawn Codex without inheriting Discord secrets.
- Preserve the read-only container and disabled Codex execution tools unless a reviewed use case requires them.
- Use the repository-local uv environment for Python dependencies and tests.
- Use Conventional Commits.
