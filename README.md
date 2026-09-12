# Discord Codex Bot

Private Discord slash commands backed by Codex CLI authenticated with a ChatGPT subscription. The
runtime is one always-restarting Docker container (`BOT_CONTAINER_NAME`, default `discord-codex-bot`).

## Architecture

```text
Discord guild allowlist
        |
        v
/codex slash command ─┐
                      ├─> discord.py -> validate -> serial queue -> codex exec (Luna, effort allowlist)
@mention + images ────┘                                       |
                                                              v
                                            Docker volume: CODEX_HOME
                                            (ChatGPT login + local memories)
```

There is no inbound HTTP port. The bot opens outbound connections to the Discord Gateway and
OpenAI. `ALLOWED_GUILD_IDS` is enforced when commands are registered and again for every
interaction. `ALLOWED_CHANNEL_IDS` is optional and intended for the initial test channel; it is one
global list across all allowed guilds, so leave it empty once every channel in both servers may use
the Bot (the current deployment).

The two allowed guilds share one Codex identity and one local memory store because this deployment
represents one agent. Use separate containers and separate `CODEX_HOME` volumes if guild memories
must be isolated from each other.

## Local development

This repo owns its Python environment. Do not use another repo's uv environment.

```bash
cd discord-codex-bot
UV_CACHE_DIR=.uv-cache uv sync
UV_CACHE_DIR=.uv-cache uv run pytest
UV_CACHE_DIR=.uv-cache uv run ruff check .
```

## 1. Create the Discord application

1. Open the Discord Developer Portal and select **New Application**.
2. Open **Bot**, select **Reset Token**, and copy the token once.
3. Under **Privileged Gateway Intents** enable only **Message Content Intent** (needed for the
   `@mention` entry point). Leave Presence and Server Members off.
4. Open **OAuth2 > URL Generator**.
5. Select scopes `bot` and `applications.commands`.
6. Grant the bot permission to view channels and send messages, then open the generated URL and
   invite it to the test server.
7. In Discord, enable Developer Mode under **User Settings > Advanced**. Copy the application ID,
   test server ID, and test channel ID.

Never paste the Discord token into chat, source files, or GitHub.

## 2. Configure the test server

```bash
cd discord-codex-bot
cp .env.example .env
chmod 600 .env
```

Edit `.env` locally:

```dotenv
DISCORD_TOKEN=<bot token>
DISCORD_APPLICATION_ID=<application id>
ALLOWED_GUILD_IDS=<test guild id>
ALLOWED_CHANNEL_IDS=<test channel id>
```

The `.env` file is ignored by Git and excluded from the Docker build context.

## 3. Build the always-restarting container

```bash
docker compose up -d --build
docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' discord-codex-bot
docker compose ps
```

Without Discord configuration, the container stays alive and logs which variables are missing.
After `.env` changes, recreate it so Compose loads the new environment:

```bash
docker compose up -d --force-recreate
```

## 4. Log Codex in with the ChatGPT subscription

Run device authentication inside the container:

```bash
docker exec -it discord-codex-bot codex login --device-auth
```

Open the displayed URL, enter the one-time code, and sign in with the ChatGPT account whose
subscription quota should be used. The login is stored only in the named volume
`<BOT_CONTAINER_NAME>_codex_home`.

Verify the authentication mode:

```bash
docker exec discord-codex-bot codex login status
```

The required result is:

```text
Logged in using ChatGPT
```

Do not use `codex login --with-api-key`; that selects API-key authentication instead of the
subscription login requested for this project.

Recreate the Bot after login if it was still in pending-configuration mode:

```bash
docker compose up -d --force-recreate
docker compose logs --tail=100 bot
```

## 5. Test in Discord

Guild-scoped slash commands normally appear quickly. Run:

```text
/codex-status
/codex prompt:請用一句話說明你目前能做什麼
@<your bot> 這張圖裡有什麼？   (with images attached to the message)
```

Every slash command is named from `COMMAND_PREFIX` (default `codex`): `/<prefix>`,
`/<prefix>-status`, `-reset`, `-remember`, `-forget`, `-memory`, `-style`. This README uses the
default; set `COMMAND_PREFIX=my-bot` in `.env` and recreate to rename them all at once.

Three backends share that pipeline. Codex CLI is the default; Google's Antigravity CLI (`agy`) is
the second; OpenRouter (free models only) the third. Each member picks `provider` → `model`
(autocomplete, typing filters; the OpenRouter list is the live free-model catalog, image-capable
first) → default `effort` with `/<prefix>-model` (Codex Luna; Gemini 3.8/3.7/3.6 Flash; Gemini
3.1 Pro; Claude Sonnet 4.6; Claude Opus 4.6; GPT-OSS 120B; whatever OpenRouter lists as free that
hour). OpenRouter is stateless, so the Bot keeps those conversations itself (`OPENROUTER_DIR`,
replayed within `OPENROUTER_HISTORY_CHARS`), sends the persona as the system message, inlines
images for models the catalog marks image-capable and `reasoning.effort` for models that take it;
picking one reminds the member that free models are unstable and may vanish. The shared `effort`
option is mapped onto what each family can run
— agy bakes the effort into the model slug (Flash: low/medium/high, Pro: low/high, Claude and
gpt-oss fixed), verified against a full model × `--effort` matrix. `agy` runs headless
(`--input-format stream-json`, `--conversation` to resume, `--json-schema` for structured output,
`--add-dir` + `view_file` for images), inside a registered project so `AGENTS.md` (the persona)
applies, with `config/agy-settings.json` denying commands, writes, URL access and MCP. Its Google
sign-in lives in the `<name>_agy_home` volume: run `agy` inside the container once (SSH-style URL
+ code loop). Threads never cross backends; switching models starts a new thread and harvests the
old one. Google's content policy may reject prompts on the Gemini models that the Claude models
accept. Release announcements (`announce/latest.md`) are posted only to `ANNOUNCE_CHANNEL_IDS`.

Links are read by the Bot itself, so both backends see the same thing: every http(s) URL in a
member's message (up to `LINK_MAX_URLS`) is fetched, converted to text (`LINK_MAX_CHARS` per page)
and injected as untrusted `<LINK>` blocks; the model can also ask for a page with
`<fetch url="…"/>` during its read loop (a reply that is *only* tags; a tag quoted inside prose is
just text). Only public addresses are fetched — LAN, loopback and reserved ranges are refused at
connect time, on every redirect hop and on every request a rendered page makes — with size, time
and redirect bounds. When the
plain fetch is blocked (bot challenge, 403/429/503, no readable text) or the model asks with
`<fetch url="…" render="1"/>` because the member wants to know what a page *looks* like, the Bot
falls back to headless Chromium (Playwright, installed in the image): it waits out the challenge,
takes the rendered text and attaches a full-page screenshot (`LINK_RENDER_TIMEOUT_SECONDS`,
`LINK_SCREENSHOT_MAX_HEIGHT`) so the model can see the pictures. X posts (x.com and the
fxtwitter/vxtwitter/fixupx/fixvx mirrors) are read through the fxtwitter API instead — text plus
the photos or video poster frames, attached as images — with the page as fallback. When neither
the plain fetch nor Chromium can read a page (an interactive Turnstile, e.g. Dcard, is given up
on at once), the Bot falls back to the link preview Discord attached to the message — Discord's
crawler is a Cloudflare-verified bot and gets the title, description and picture — labelled as
a preview, not the full page (`LINK_PREVIEW_WAIT_SECONDS` waits once for the embed to appear).
Pages behind logins still come back as "打不開"; on the Codex backend OpenAI's own web tool
remains available as a second path.

Videos in a link are understood through Gemini's free tier used as a tool (not a chat backend;
`GEMINI_API_KEY`, no billing attached). A YouTube link is understood from its URL alone — Gemini
watches the frames and listens — with the captions (`youtube-transcript-api`) as a fallback when
Gemini cannot; an X clip is downloaded (up to `GEMINI_VIDEO_INLINE_MAX_BYTES`) and sent inline.
The description is injected as an untrusted `<VIDEO>` block so whichever backend the member picked
can answer about the clip. Understanding races a timer: a video that takes longer than
`VIDEO_INTERIM_AFTER_SECONDS` shows a "still watching" reply that is edited into the final answer
when it is done, so short clips answer in one shot and long ones do not look stalled.

Both entry points share one pipeline: guild allowlist → validation → serial queue → `codex exec`.
The `@mention` form keeps the question visible as the member's own message, supports up to
`MAX_ATTACHMENTS` images per message, and the Bot answers as a reply. Replying to another
member's message while mentioning the Bot points it at that message: its text is quoted into the
request and its images are attached, so "@Bot what is this?" as a reply to a picture works.
Messages that do not mention the Bot are discarded without processing.

`/codex-status` must report `ChatGPT 訂閱登入有效`, model `gpt-5.6-luna`, and the default reasoning
effort (`Medium`). A command in another server or outside the configured test channel must not execute.

The Bot serializes Codex work to one request at a time and caps the queue, prompt, response, and
runtime. `/codex` has an optional `effort` choice — Low, Medium (default), High, Extra high, Max —
that overrides the member's stored default for one request; on Codex these map to the CLI values
`low/medium/high/xhigh/max` verified against `codex debug models` for `gpt-5.6-luna` (the CLI
forwards any string verbatim, so the Bot only offers this allowlist). `@mention` requests use the
member's stored effort, else `CODEX_REASONING_EFFORT`.

Follow-up questions keep their context: each answer is a Codex thread, and the Bot resumes it with
`codex exec resume <thread_id>` when the same member asks again in the same channel within
`THREAD_TTL_MINUTES`, or when anyone replies to one of the Bot's answers (that exact thread, any
age). `/codex new:True` or `/codex-reset` starts fresh. The mapping lives in
`CODEX_HOME/discord_threads.json` and survives restarts; a thread that can no longer be resumed
falls back to a fresh one. Codex bakes the instruction files (`AGENTS.md`, output style) into a
thread when it starts and does not re-read them on resume, so every stored thread carries a
fingerprint of those files; after a rebuild that changes them, old threads are not resumed and
the next message starts fresh with the new persona/style. The same applies to the workspace a
thread was started in: setting or clearing a personal style switches between the persona and
persona-free workspaces, so the member's next message starts a new thread rather than
continuing under the old `AGENTS.md`. This is conversation memory, not
Codex's background "memories" feature,
which consolidates asynchronously and is not tied to Discord members.

Long-term memory is Bot-owned and two-tier, shaped like Claude Code auto memory, with a personal
scope per member and a shared scope per server:

```text
CODEX_HOME/memory/<guild>/guild/              shared by everyone in that server
CODEX_HOME/memory/<guild>/users/<member>/     seen only in that member's requests
    MEMORY.md            index, one line per note; the first MEMORY_INDEX_MAX_LINES / MEMORY_INDEX_MAX_BYTES
                         (200 lines / 25 KB) are injected into every prompt
    MEMORY-archive.md    index lines that fell off the injected window (still recallable)
    topics/<slug>.md     the note itself, read on demand
```

Notes get in two ways: `/remember scope name text` (explicit) and automatically, when the model
ends an answer with `<memory scope="user|guild" name="…">…</memory>` because the member stated a
durable fact (the tag is stored and stripped). Reading is snippet-first, like pi-context: the
model sees only the index; it can reply with `<search scope="…" query="regex"/>` to get matching
lines with `MEMORY_SEARCH_CONTEXT_LINES` of context (up to `MEMORY_SEARCH_MAX_MATCHES` hits), or
`<recall scope="…" name="…" offset="1" lines="200"/>` to read one page of a note. Every result is
fed back into the same thread and bounded like pi's tool output (`MEMORY_READ_MAX_LINES` 2000 /
`MEMORY_READ_MAX_BYTES` 50 KB per page, with the total line count in the header so the model can
page on); at most `MEMORY_RECALL_ROUNDS` (10) rounds per request. `<recall name="list"/>` lists
archived notes. `/memory` shows what is stored, `/forget` deletes a note. Capacity is capped per
scope (`MEMORY_USER_MAX_BYTES` 50 MB, `MEMORY_GUILD_MAX_BYTES` 200 MB); a full scope evicts its
oldest notes.
Codex's own background "memories" are not used for this: they consolidate only after 6 h idle in
a long-lived process and have no notion of Discord members.

A third, operator-only tier lives outside the Bot's control: `permanent/` in the repo directory
(gitignored except its README) is baked into the image at `PERMANENT_MEMORY_DIR`. Its `MEMORY.md`
is injected into every prompt verbatim as `[永久記憶索引]` — no line/byte window, never evicted,
never consolidated — and `permanent/topics/*.md` are searchable/recallable with
`scope="permanent"`. The Bot never writes there and has no command for it; edit the files and
rebuild.

Finished conversations feed memory too. When a thread can no longer be resumed — its TTL passed,
the persona/style was rebuilt, or the member used `new:True` / `-reset` — a background pass (every
`HARVEST_INTERVAL_MINUTES`) reads its transcript from the Codex session rollout and asks Codex
(`--output-schema`) for the few things worth remembering about that member next month; each
becomes a personal note. One-off questions, looked-up facts and the persona itself are skipped.
The rule is a state, not an event: whatever makes a thread non-resumable (TTL, instruction
fingerprint, workspace switch, `new:True`, `-reset`, being replaced) makes it a harvest
candidate, and a switch wakes the pass immediately instead of waiting for the interval. Each
thread is harvested once per retirement — a thread continued afterwards by replying to an old
answer is harvested again when it retires next. The pass waits while the last known 5-hour
reading is under `CONSOLIDATE_MIN_REMAINING_PERCENT`. Operators can run it on demand inside the container with
`python -m discord_codex_bot.harvest` (`--force` ignores the quota gate); the nightly
consolidation has the same entry point, `python -m discord_codex_bot.consolidate [--force]`.

Notes are consolidated once a day. At `CONSOLIDATE_HOUR` (`CONSOLIDATE_TIMEZONE`, default 02:00
Asia/Taipei) the Bot runs one minimal Codex turn so the session rollout carries fresh
`rate_limits`, reads the 5-hour window's `used_percent`, and proceeds only if at least
`CONSOLIDATE_MIN_REMAINING_PERCENT` (50) remains. It then rewrites every scope of every guild
(server-wide and each member) through `codex exec --output-schema`: duplicates and fragments are
merged, contradictions resolved newest-wins, nothing invented. Input is fed in batches of
`CONSOLIDATE_MAX_INPUT_BYTES`; each scope's previous state is kept in `.backup/` until the next
run, and a failed scope is left untouched. Codex's own background memory consolidation is not used
(it needs 6 h of idle time in a long-lived process and has no member dimension).

The persona is a fourth operator-only layer: `persona/*.md` (gitignored except its README) is
appended to `/workspace/AGENTS.md` at build time, so Codex loads it as project instructions on
every request — the place with the most weight this deployment can give it. A member who sets a
personal style is served from `/workspace-plain` (rules only), so the personal style replaces the
persona instead of competing with it; a thread keeps the workspace it started in.

Output style has two layers. `config/output-style.md` is the operator's default; when it has
content it is injected as `<OUTPUT_STYLE>` into every prompt (rebuild the image after editing).
Each member can set their own with `/style text:…` (stored as
`memory/<guild>/users/<member>/style.md` and injected as `<PERSONAL_STYLE>`, which wins over the
default where they conflict), inspect it with `/style`, and return to the default with
`/style clear:True`.

Codex can also generate images (`image_generation = true`). The built-in tool writes them to
`CODEX_HOME/generated_images/<thread_id>/`; the Bot attaches them to the reply (up to 10) and
deletes that directory afterwards. Leftovers from crashed requests are swept with the attachments.

`/codex` accepts an optional `image` attachment (PNG/JPEG/WebP/GIF, `MAX_ATTACHMENT_BYTES`). The
file is saved into a per-request directory under `/tmp/discord-codex` (container tmpfs), passed to
`codex exec -i`, and deleted when the request finishes; a sweeper removes any leftover request
directory older than one request timeout every `ATTACHMENT_SWEEP_MINUTES`. Restarting the container
also clears the tmpfs.

## 6. Add the production server

Invite the same application to the production server. If that server is administered by someone
else, the owner temporarily enables **Public Bot** in the Developer Portal, hands them the invite
URL below, and turns Public Bot off again once they have added it. A server that is not listed in
`ALLOWED_GUILD_IDS` gets no commands and every request is rejected, so the allowlist — not the
Public Bot switch — is the real gate.

```text
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot%20applications.commands&permissions=2147486720&integration_type=0
```

Then update `.env`:

```dotenv
ALLOWED_GUILD_IDS=<test guild id>,<production guild id>
```

For server-only restriction across every channel, clear the channel allowlist:

```dotenv
ALLOWED_CHANNEL_IDS=
```

Recreate and verify:

```bash
docker compose up -d --force-recreate
docker compose logs --tail=100 bot
```

The startup log must show command registration for exactly the two allowed guild IDs. No per-user
filter exists; every member who can see and invoke the command in those guilds may use it.

## Memory and security boundaries

- Codex runtime policy lives in `config/codex-config.toml`; the entrypoint copies it into
  `CODEX_HOME` on every start, so the named volume never keeps a stale policy.
- Codex local memory is enabled and stored under `/var/lib/codex/memories/` in the named volume.
- CLI prompt history is disabled with `history.persistence = "none"`.
- Session files remain enabled because automatic memory generation needs durable session input.
- The container mounts no host home or source workspace.
- Codex command tools, apps, browser, computer use, plugins, `view_image` (reads local files),
  goals and multi-agent are disabled; image generation is enabled. Web search is enabled (`web_search =
  "live"`); it runs on OpenAI's side, so the container itself never makes outbound requests for
  it. The model-visible tool list is then `web__run` plus `apply_patch`, which the read-only
  sandbox plus `approval_policy = "never"` rejects (verified: asking `/codex` to create a file is
  refused and nothing is written).
- The Codex child process receives a minimal environment without `DISCORD_TOKEN` and runs in its
  own process group so a timed-out request cannot leave the native binary running.
- The root filesystem is read-only, Linux capabilities are dropped, and no host port is published.
  Unprivileged user namespaces are unavailable inside the container, so Codex's own `bwrap`
  sandbox cannot start; container isolation is the effective boundary, which is why the execution
  tools stay disabled.

This is a private personal deployment. Anyone in either allowed guild can spend the same ChatGPT
subscription quota. The serial queue limits concurrency but does not create additional quota.

## Operations

```bash
# Status
docker compose ps
docker exec discord-codex-bot codex login status

# Logs
docker compose logs --tail=200 bot

# Restart
docker compose restart bot

# Stop without deleting login or memory
docker compose down

# Start again
docker compose up -d
```

Do not run `docker compose down -v` unless the ChatGPT login and all Bot memories should be deleted.

## Updating

Change `CODEX_VERSION` deliberately in `.env`, rebuild, then re-run the tests and authentication
check:

```bash
UV_CACHE_DIR=.uv-cache uv sync
UV_CACHE_DIR=.uv-cache uv run pytest
docker compose build --pull
docker compose up -d
docker exec discord-codex-bot codex --version
docker exec discord-codex-bot codex login status
```
