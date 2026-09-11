# Discord Codex Bot

Private Discord slash commands backed by Codex CLI authenticated with a ChatGPT subscription. The
runtime is one always-restarting Docker container named `tommy_test`.

## Architecture

```text
Discord guild allowlist
        |
        v
/codex slash command -> discord.py -> serial queue -> codex exec
                                                |
                                                v
                              Docker volume: CODEX_HOME
                              (ChatGPT login + local memories)
```

There is no inbound HTTP port. The bot opens outbound connections to the Discord Gateway and
OpenAI. `ALLOWED_GUILD_IDS` is enforced when commands are registered and again for every
interaction. `ALLOWED_CHANNEL_IDS` is optional and intended for the initial test channel.

The two allowed guilds share one Codex identity and one local memory store because this deployment
represents one agent. Use separate containers and separate `CODEX_HOME` volumes if guild memories
must be isolated from each other.

## Local development

This repo owns its Python environment. Do not use another repo's uv environment.

```bash
cd /home/tommy_liu/tommy/discord-codex-bot
UV_CACHE_DIR=.uv-cache uv sync
UV_CACHE_DIR=.uv-cache uv run pytest
UV_CACHE_DIR=.uv-cache uv run ruff check .
```

## 1. Create the Discord application

1. Open the Discord Developer Portal and select **New Application**.
2. Open **Bot**, select **Reset Token**, and copy the token once.
3. Keep privileged gateway intents disabled; this bot only needs the `Guilds` intent.
4. Open **OAuth2 > URL Generator**.
5. Select scopes `bot` and `applications.commands`.
6. Grant the bot permission to view channels and send messages, then open the generated URL and
   invite it to the test server.
7. In Discord, enable Developer Mode under **User Settings > Advanced**. Copy the application ID,
   test server ID, and test channel ID.

Never paste the Discord token into chat, source files, or GitHub.

## 2. Configure the test server

```bash
cd /home/tommy_liu/tommy/discord-codex-bot
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
docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' tommy_test
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
docker exec -it tommy_test codex login --device-auth
```

Open the displayed URL, enter the one-time code, and sign in with the ChatGPT account whose
subscription quota should be used. The login is stored only in the named volume
`tommy_test_codex_home`.

Verify the authentication mode:

```bash
docker exec tommy_test codex login status
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
```

`/codex-status` must report `ChatGPT 訂閱登入有效`, model `gpt-5.6-luna`, and reasoning effort
`high`. A command in another server or outside the configured test channel must not execute.

The Bot serializes Codex work to one request at a time and caps the queue, prompt, response, and
runtime. Discord users cannot select the model or reasoning effort.

`/codex` accepts an optional `image` attachment (PNG/JPEG/WebP/GIF, `MAX_ATTACHMENT_BYTES`). The
file is saved into a per-request directory under `/tmp/discord-codex` (container tmpfs), passed to
`codex exec -i`, and deleted when the request finishes; a sweeper removes any leftover request
directory older than one request timeout every `ATTACHMENT_SWEEP_MINUTES`. Restarting the container
also clears the tmpfs.

## 6. Add the production server

Invite the same application to the production server. Then update `.env`:

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
- Codex command tools, apps, browser, computer use, image generation, plugins, `view_image`
  (reads local files), goals, multi-agent and web search are disabled. The model-visible tool list
  then contains only `apply_patch`, which the read-only sandbox plus `approval_policy = "never"`
  rejects; verify after login by asking `/codex` to create a file and expecting a refusal.
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
docker exec tommy_test codex login status

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
docker exec tommy_test codex --version
docker exec tommy_test codex login status
```
