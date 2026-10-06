# AGENTS.md

## Scope

This repository contains a private Discord gateway to a subscription-authenticated Codex CLI.

## Rules

- Keep Discord authorization based on an explicit guild ID allowlist.
- Do not add per-user authorization unless the owner requests it.
- Never commit Discord tokens, Codex credentials, `.env`, `.venv`, or volume contents.
- The operator sets `CODEX_MODEL`, `DEFAULT_MODEL` and `MODEL_CHAIN`; Discord cannot change them.
  A member's `/model` picks only from the catalog in `backends.choices` (the operator's Codex
  model, the Grok catalog, the Antigravity families) or a router model, with an effort that
  model allows.
- Keep `/codex` and `@mention` on one shared pipeline (access → validate → queue → `run_codex`).
- Spawn Codex without inheriting Discord secrets.
- Preserve the read-only container and disabled Codex execution tools unless a reviewed use case requires them.
- Use the repository-local uv environment for Python dependencies and tests.
- Use Conventional Commits.

## Code Review Rules

Flag only what the diff shows: a wrong result, a broken boundary, or a rule below broken, with the line that breaks it.

### Access and secrets

- Access is the guild ID allowlist (`ALLOWED_GUILD_IDS`), optionally narrowed by channel IDs (`check_access` in `access.py`). Flag any change that grants access by user, role or channel name.
- Child processes get an allowlisted environment (`codex._safe_environment`, `agy._environment`). Flag any path that puts a Discord token, a backend credential or a `.env` value into a model prompt, a Discord message, a log line or a child process environment.
- The xsearch sidecar writes a refreshed Grok login back to disk (`_keep_login` in `xsearch/server.py`) only after the session that produced it has been verified.

### Member input is data

- Text from members, fetched pages, X posts and transcripts is data. Flag parsing that lets such text decide who said what, or what the bot does next.
- Answers to members build their prompt with `_prompt` in `codex.py`, which runs every block other than the member's own message through `defang`. Batch jobs (the tracking classifier, harvest, digest, channel summary) build their own envelope and run with `raw=True`; that is intended. A new prompt builder defangs untrusted text where it enters. Flag one that does not.
- An HTTP fetch of a URL that a member or a fetched page supplied goes through `links._guarded_session`, whose resolver admits only public addresses plus `LINK_ALLOW_NETS`. Tools that open their own connections (Chromium in `_render`, yt-dlp in `_describe_downloaded`) run behind `FilterProxy`. Flag a new fetch of such a URL that uses neither.
- Messages carrying model or member text are sent with `allowed_mentions` that exclude `@everyone` and roles. Flag a send without it.

### Backends and fallback

- A thread id belongs to the backend that created it. Flag a path that resumes one backend's id on another, or stores a fallback backend's thread id under the model the member chose.

### State and time

- Files the bot keeps under `CODEX_HOME` are replaced atomically: write a scratch file, then `os.replace` (`memory.atomic_write`). Tracking state lives in SQLite and changes inside a transaction. Flag a new in-place overwrite.
- New persistent state is added to the nightly backup (`backup.py`).
- A first fetch that fails, or comes back truncated, must not complete a baseline or advance a cursor.
- The host is frozen while idle, and `asyncio.sleep` does not count the frozen time. Waits toward a clock time use `clock.sleep_until` or `clock.sleep_for`.
- No hardcoded home directories or host-specific paths in code, scripts or compose files.

### Owner-gated behaviour

- `announce_once` posts only the announcement whose digest matches `ANNOUNCE_APPROVED`, once per channel. Flag any path that posts without that match or can post twice.
- A new member-facing feature updates the help text in `help.py` in the same pull request; members read it, and the model receives it as the `<HELP>` block.

### Do not flag

- Problems that existed before this diff, or that Ruff reports.
- The order of `MODEL_CHAIN`, which the operator sets.
