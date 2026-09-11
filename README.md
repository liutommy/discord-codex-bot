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

Both entry points share one pipeline: guild allowlist → validation → serial queue → `codex exec`.
The `@mention` form keeps the question visible as the member's own message, supports up to
`MAX_ATTACHMENTS` images per message, and the Bot answers as a reply. Messages that do not
mention the Bot are discarded without processing.

`/codex-status` must report `ChatGPT 訂閱登入有效`, model `gpt-5.6-luna`, and the default reasoning
effort (`Medium`). A command in another server or outside the configured test channel must not execute.

The Bot serializes Codex work to one request at a time and caps the queue, prompt, response, and
runtime. Discord users cannot select the model. `/codex` has an optional `effort` choice — Low,
Medium (default), High, Extra high, Max — mapped to the CLI values `low/medium/high/xhigh/max`
verified against `codex debug models` for `gpt-5.6-luna`; the CLI forwards any string verbatim, so
the Bot only offers this allowlist. `@mention` requests use the default `CODEX_REASONING_EFFORT`.

Follow-up questions keep their context: each answer is a Codex thread, and the Bot resumes it with
`codex exec resume <thread_id>` when the same member asks again in the same channel within
`THREAD_TTL_MINUTES`, or when anyone replies to one of the Bot's answers (that exact thread, any
age). `/codex new:True` or `/codex-reset` starts fresh. The mapping lives in
`CODEX_HOME/discord_threads.json` and survives restarts; a thread that can no longer be resumed
falls back to a fresh one. This is conversation memory, not Codex's background "memories" feature,
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
durable fact (the tag is stored and stripped). Reading works like Claude's index-then-open: the
model sees only the index; if it needs a note it replies with `<recall scope="…" name="…"/>` and
the Bot feeds that file (up to `MEMORY_RECALL_MAX_BYTES`) back into the same thread, at most
`MEMORY_RECALL_ROUNDS` times per request. `<recall name="list"/>` lists archived notes. `/memory`
shows what is stored, `/forget` deletes a note. Capacity is capped per scope
(`MEMORY_USER_MAX_BYTES` 50 MB, `MEMORY_GUILD_MAX_BYTES` 200 MB); a full scope refuses new notes.
Codex's own background "memories" are not used for this: they consolidate only after 6 h idle in
a long-lived process and have no notion of Discord members.

`config/output-style.md` is the operator's default output style; when it has content it is
injected as `<OUTPUT_STYLE>` into every prompt (rebuild the image after editing).

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
