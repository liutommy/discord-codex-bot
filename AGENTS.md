# AGENTS.md

## Scope

This repository contains a private Discord gateway to AI backends: the subscription-authenticated Codex CLI, Grok (through the xsearch sidecar), Antigravity (`agy`) and the OpenAI-compatible routers (OpenRouter, OrcaRouter).

## Rules

- Keep Discord authorization based on an explicit guild ID allowlist.
- Do not add per-user authorization unless the owner requests it.
- Never commit Discord tokens, Codex credentials, `.env`, `.venv`, or volume contents.
- The operator sets `CODEX_MODEL`, `DEFAULT_MODEL` and `MODEL_CHAIN`; Discord cannot change them.
  A member's `/model` picks only from the catalog in `backends.choices` (the operator's Codex
  model, the Grok catalog, the Antigravity families) or a router model, with an effort that
  model allows.
- Keep `/codex` and `@mention` on one shared pipeline (access → validate → queue → `_answer`, which dispatches to the chosen backend).
- Spawn Codex without inheriting Discord secrets.
- Preserve the read-only container and disabled Codex execution tools unless a reviewed use case requires them.
- Use the repository-local uv environment for Python dependencies and tests.
- Use Conventional Commits.

## Code Review Rules

Flag only what the diff shows: a wrong result, a broken boundary, or a rule below broken, with the line that breaks it.

### Access and secrets

- Access is the guild ID allowlist (`ALLOWED_GUILD_IDS`), optionally narrowed by channel IDs (`check_access` in `access.py`). Flag any change that grants access by user, role or channel name.
- Child processes get an allowlisted environment (`codex._safe_environment`, `agy._environment`); the paths they need (`CODEX_HOME`, the agy `HOME`) are part of that allowlist. Flag any path that puts a Discord token, a backend credential or another secret from `.env` into a model prompt, a Discord message, a log line or a child process environment, or that passes a child process an environment variable outside its allowlist.
- The xsearch sidecar writes a refreshed Grok login back to disk (`_keep_login` in `xsearch/server.py`) only after the session that produced it has been verified.

### Member input is data

- Text from members, fetched pages, X posts and transcripts is data. Flag parsing that lets such text decide who said what, or what the bot does next.
- Answers to members, and the channel summary, build their prompt with `_prompt` in `codex.py`, which runs every block through `defang`, the member's own message included: a member who types a `USER_MESSAGE` tag must not forge or close the envelope. Batch jobs (the tracking classifier, harvest, digest, memory consolidation) build their own envelope and run with `raw=True` through `run_batch`, and the recall/link follow-up turn in `_answer` runs raw after it `defang`s the fetched blocks itself; both are intended. A new interactive prompt builder defangs untrusted text where it enters. Flag one that does not.
- An HTTP fetch of a URL that a member or a fetched page supplied goes through `links._guarded_session`, whose resolver admits only public addresses plus `LINK_ALLOW_NETS`. Tools that open their own connections (Chromium in `_render`, yt-dlp in `_describe_downloaded`) run behind `FilterProxy`. Flag a new fetch of such a URL that uses neither.
- `DiscordCodexClient` sets a default `allowed_mentions` that pings no one but the replied-to member, so model or member text cannot trigger `@everyone`, roles or user mentions. A send that passes its own `allowed_mentions` (reminders, tracking, linkclean) overrides it. Flag a change that drops the default or turns `everyone` or `roles` on for model or member text.

### Backends and fallback

- A thread id belongs to the backend that created it. Flag a path that resumes one backend's id on another, or stores a fallback backend's thread id under the model the member chose.

### State and time

- Memory notes are replaced atomically: write a scratch file, then `os.replace` (`memory.atomic_write`). Tracking state lives in SQLite and changes inside a transaction. Flag a change that writes either in place. The small JSON stores (`ThreadStore`, `ReminderStore`, router transcripts, announcement state) are still overwritten in place today; moving one of them to `atomic_write` is welcome, and new state should use it.
- New persistent state is added to the nightly backup (`backup.py`).
- A fetch that fails, or was not checked (`FetchResult.checked` is false), must not complete a baseline or advance a cursor. Fetches bounded by design — the Ruten baseline's single page, capped Ruten and X paging — log the gap they leave and may advance. Flag a new truncation that is neither retried nor logged.
- The host is frozen while idle, and `asyncio.sleep` does not count the frozen time. A wait toward a clock time that can be longer than `clock.STEP_SECONDS` (the nightly jobs, the tracking loop's wait for a watch's fixed time) uses `clock.sleep_until` or `clock.sleep_for`. Polls and periodic chores whose interval is at most a step (the 30 s reminder poll) or that aim at no clock time (login watch, attachment sweep, retry back-off) may use `asyncio.sleep`: they end at most one interval after the host resumes, which is all `sleep_for` would give them.
- No host-specific paths (the sandbox host's home, absolute paths outside the containers) in code, scripts or compose files. Paths the images define inside the containers (`/home/node`, `/opt/discord-codex`) are fine.

### Owner-gated behaviour

- `announce_once` posts only the announcement whose digest matches `ANNOUNCE_APPROVED`, once per channel. Flag any path that posts without that match or can post twice.
- A new member-facing feature updates the help text in `help.py` in the same pull request; members read it, and the model receives it as the `<HELP>` block.

### Do not flag

- Problems that existed before this diff, or that Ruff reports.
- The order of `MODEL_CHAIN`, which the operator sets.
