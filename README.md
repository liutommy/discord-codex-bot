# Discord Codex Bot

Private Discord slash commands backed by Codex CLI authenticated with a ChatGPT subscription. The
runtime is one always-restarting Docker container (`BOT_CONTAINER_NAME`, default `tommy_test`).

## Architecture

```text
Discord guild allowlist
        |
        v
/codex slash command ─┐
                      ├─> discord.py -> validate -> serial queue -> codex exec (Luna, effort allowlist)
@mention + images ────┤                                       |
YouTube/Twitch poll ──┘ -> SQLite -> quota gate -> isolated classifier -> Discord notification
                                                              v
                                            Docker volume: CODEX_HOME
                                            (ChatGPT login + local memories)
```

There is no inbound HTTP port. The bot opens outbound connections to the Discord Gateway and
OpenAI. `ALLOWED_GUILD_IDS` is enforced when commands are registered and again for every
interaction. `ALLOWED_CHANNEL_IDS` is optional and intended for the initial test channel; it is one
global list across all allowed guilds, so leave it empty once every channel in both servers may use
the Bot.

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
ALLOWED_CHANNEL_IDS=975467992370520074
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
docker compose exec bot codex login --device-auth
```

Open the displayed URL, enter the one-time code, and sign in with the ChatGPT account whose
subscription quota should be used. The login is stored only in the named volume
`<BOT_CONTAINER_NAME>_codex_home`.

Verify the authentication mode:

```bash
docker compose exec bot codex login status
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
`/<prefix>-status`, `-reset`, `-remember`, `-forget`, `-memory`, `-style`, `-track`. This README uses the
default; set `COMMAND_PREFIX=my-bot` in `.env` and recreate to rename them all at once.

### Social tracking: YouTube and Twitch

The first version polls official sources: YouTube's channel Atom feed plus Data API v3 metadata,
and Twitch Helix streams/videos with an app access token. It does not scrape X, Instagram, or
TikTok. A source is fetched once even when several members watch it; Codex runs only when a new
item has no saved decision for that watch.

For YouTube, create a Google Cloud project, enable **YouTube Data API v3**, create an API key, and
restrict that key to the YouTube Data API. For Twitch, register an application in the Twitch
Developer Console as a **confidential** client. This implementation uses the server-side
`client_credentials` grant, so it never redirects a Discord user; if the console requires an
HTTPS OAuth redirect URL, enter one to satisfy registration, but it is not called by this Bot.

Put the credentials only in the ignored `.env` file and enable the worker:

```dotenv
TRACKING_ENABLED=true
TRACKING_INTERVAL_MINUTES=15
TRACKING_MIN_REMAINING_PERCENT=0
TRACKING_REASONING_EFFORT=high
YOUTUBE_API_KEY=<restricted YouTube API key>
TWITCH_CLIENT_ID=<Twitch client id>
TWITCH_CLIENT_SECRET=<Twitch client secret>
```

Recreate the Bot, then add the two test watches in Discord:

```text
/codex-track source:https://www.youtube.com/@HoushouMarine
/codex-track source:https://www.twitch.tv/chibidoki
/codex-track
```

Besides YouTube and Twitch channels, any public page can be a source. A page has no item
boundaries and no ids of its own, which is what made web tracking look expensive: noticing a
change would have meant asking a model every poll. Since the Bot started keeping anchors when it
reads a page, the links on it *are* the stable ids a feed would have provided — a URL that was
not there last time is new content, and an unchanged page produces no items at all, so the model
is not called. Only same-host links count — that is what the source *is*, not a guess about which
links matter. Nothing tries to work out which of them is an article: three attempts at that (by
URL shape, then by label length) each worked on the site they were written against and failed on
the next, so the division of labour is now the same one the rest of the Bot uses — code remembers
exactly which URLs have been seen, the model reads them and decides which is worth telling someone
about. A site's navigation is stable, so it arrives in the first fetch, which is the baseline and
is never classified. This also works on sites that refuse plain HTTP (Konami's Yu-Gi-Oh site answers
403 and is read through Chromium) because it goes through the same reader as `<fetch>`.

A watch is live the moment it is made. It only ever considers content published after that — the
first sight of a source is its baseline and is never classified — and it posts only the items that
match, mentioning the watch's owner. The model decides both halves: whether the item is worth an
interruption *and* how to say it. It is given the source's identity and the member's own policy,
and returns the sentence to send; the Bot only does what the model cannot — escaping mentions out
of untrusted text, bounding the length, adding the link and the @. A decision carrying no wording
(one made before this existed) falls back to a plain generated line. Items judged not worth a notification are recorded and stay
silent. `/codex-track log:<id>` shows that judgement history to the member who owns the watch and
to nobody else: it is the log behind the notifications, not something to push into a channel.
`/codex-track cancel:<id>` removes a watch. The default policy alerts on major announcements, new models/outfits/3D, music
releases, concerts/events, anniversaries/milestones, hiatus/return/graduation, major collaborations,
and rare charity/subathon/marathon streams. Routine streams, clips, repeated merchandise, and
uncertain titles are ignored.

Fetching and judging run on two different clocks. Every source is fetched on
`TRACKING_INTERVAL_MINUTES` (15) because HTTP costs nothing, but a single watch only spends a
classification every `TRACKING_CLASSIFY_INTERVAL_MINUTES` (60) — the part that costs subscription
quota. Each watch stores its own interval, an attempt stamps its clock (failures included, so a
broken source cannot burn quota every pass), and a member can change theirs by asking.

Watches can also be managed by asking in words, the way reminders can: the model appends
`<track source="…" interest="…" who="…" every="60"/>`, `<track_every id="N" minutes="120"/>` or
`<cancel_track id="N"/>`
after its answer and the Bot performs it, reporting what it did. Attributes are read by name, not
by position. There is no mode to switch and no way to ask for the judgement log in words — that
is a slash command the member runs for themselves. A watch pings its owner; other
people are added only when the member names them in the request, exactly like `<remind who=…>`.
The member's own watches are listed in the prompt, so an id is never guessed — the store also
refuses to change a watch that belongs to someone else — cancelling included.

A tag the Bot does not implement is stripped before the answer is sent and the member is told the
operation did not happen. The model generalises from the tags it has: when cancelling was
slash-only it invented `<cancel_track/>`, told a member the watch was cancelled, and the watch
kept notifying. The vocabulary now matches the operations one-for-one, and anything left over is
reported rather than posted as if it had worked.

Classification always uses the operator-controlled Codex model and `high` effort. It runs with
Codex memories, history persistence, web search, apps, browser/computer use, image generation, and
multi-agent features disabled. Before each classifier call the Bot reads
`account/rateLimits/read`; if either the five-hour or weekly window has less than
`TRACKING_MIN_REMAINING_PERCENT` remaining, it keeps the item pending for a later poll. That
gate defaults to `0`, meaning the probe is skipped entirely: a spent subscription now runs the
classification on `CODEX_FALLBACK_MODEL` instead of failing, so there is nothing to hold quota
back for. agy is acceptable here because its own settings deny commands, writes, URL reads and
MCP, and each classification is a fresh conversation — the properties `isolated` buys on the
Codex side. Memory consolidation follows the same rule through
`CONSOLIDATE_MIN_REMAINING_PERCENT`. Provider credentials are excluded from the Codex child
environment.
Tracking state and its durable notification outbox live in `CODEX_HOME/tracking.sqlite3` and are
included in the daily backup through SQLite's online backup API.

Social titles and descriptions remain untrusted even though they are JSON-encoded in the prompt:
the output schema prevents structural escape, but it cannot guarantee that a model will never make
a schema-valid false positive. Check `/codex-track log:<id>` after the first few notifications to
see what it judged and why.
Notification delivery is intentionally at-least-once; a process crash after Discord accepts a
message but before SQLite records delivery can produce one duplicate after restart.

Four backends share that pipeline. Codex CLI is the default; Google's Antigravity CLI (`agy`) is
the second; OpenRouter and OrcaRouter (free models only, one shared OpenAI-compatible router core
— they differ in host, key, free-model rule and thread prefix) the third and fourth. OrcaRouter's
free tier is its `-free` ids (plus `orcarouter/free`); the account must have a GitHub login linked
or the API answers `429 free_rate_limited`. Each member picks `provider` → `model`
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

When the ChatGPT subscription quota runs out or the selected model is temporarily at capacity,
Codex reports it *inside* its JSONL stream (`codex_error_info: usage_limit_exceeded` or
`server_overloaded`, often with an empty stderr), so the exit code alone cannot tell these remote
conditions apart from a crash. The Bot recognises both and answers the rest of the request on
`CODEX_FALLBACK_MODEL` (default `agy:gemini-3.8-flash|medium`, written like a member's stored
model; empty disables it and the member gets the usual failure message). The reply says which
model answered, and that thread is not remembered as resumable — a Codex thread id means nothing
to another backend. The member's own model choice is untouched; the next request tries Codex
again. Background classification and memory jobs use the same fallback while preserving their
isolated/fresh-run settings.

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
Gemini cannot; an X clip is downloaded (up to `GEMINI_VIDEO_INLINE_MAX_BYTES`) and sent inline, and a link on one
of the curated short-video hosts (TikTok, Instagram, Bilibili, Reddit, Streamable, …) is pulled by
yt-dlp — a single progressive stream under the same cap, no ffmpeg — and sent inline too.
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
archived notes. `/memory` lists all stored notes, including archived notes. `/forget` offers
a searchable name picker after selecting a scope, and deletes only the selected note. Duplicate
titles require selecting a specific entry. Tracking and reminder IDs are separate: use `/track
cancel:<id>` or `/remind cancel:<id>`; entering these IDs in `/forget` gives guidance without
cancelling anything. Deleting a note does not erase existing conversation context; `/reset`
starts a fresh conversation. Capacity is capped per
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
answer is harvested again when it retires next. The pass shares
`CONSOLIDATE_MIN_REMAINING_PERCENT` with the nightly consolidation, and at its default of `0`
never defers: a spent subscription runs on `CODEX_FALLBACK_MODEL` instead.
Operators can run it on demand inside the container with
`python -m discord_codex_bot.harvest` (`--force` ignores the quota gate); the nightly
consolidation has the same entry point, `python -m discord_codex_bot.consolidate [--force]`.

Notes are consolidated once a day. At `CONSOLIDATE_HOUR` (`CONSOLIDATE_TIMEZONE`, default 02:00
Asia/Taipei) the Bot rewrites every scope of every guild. `CONSOLIDATE_MIN_REMAINING_PERCENT`
defaults to `0`, so it simply runs: a spent subscription falls back to `CODEX_FALLBACK_MODEL`
rather than failing. Set a percentage and the Bot first reads the five-hour window's
`usedPercent` from the Codex app-server (no turn spent) and skips the night when less than that
remains. The rewrite goes through every scope of every guild
(server-wide and each member) through `codex exec --output-schema`: duplicates and fragments are
merged, contradictions resolved newest-wins, nothing invented. Input is fed in batches of
`CONSOLIDATE_MAX_INPUT_BYTES`; each scope's previous state is kept in `.backup/` until the next
run, and a failed scope is left untouched. Codex's own background memory consolidation is not used
(it needs 6 h of idle time in a long-lived process and has no member dimension).

The persona is a fourth operator-only layer: `persona/*.md` (gitignored except its README and
the sample) is appended to the runtime rules to make the `AGENTS.md` Codex loads as project
instructions on every request — the place with the most weight this deployment can give it. A
member who wants the plain assistant turns it off with `/style persona:關閉人設`, which serves
them from the persona-free working directory; a thread keeps the one it started in. Setting a
personal style does not do this by itself: style and persona are separate settings, so asking for
shorter answers does not also discard the character.

That composition happens when the Bot starts, not when the image is built, so the persona and the
default output style can be replaced while it runs: `/<prefix>-persona action:上傳` opens a modal
taking one `.md` for each (either alone is fine, UTF-8, 20000 characters). An uploaded file lives
in the Codex volume and wins over the image copy until `action:還原…` deletes it, so a rebuild no
longer discards it — and editing the repo then rebuilding has no visible effect while an upload is
in force, which is what the status action is for. Whatever the upload replaced is written to
`BACKUP_DIR/instructions/<kind>-<timestamp>.md` first. The command is gated like the other
operator switches (server owner, Administrator/Manage Guild, or `LINKCLEAN_ADMIN_IDS`); note that
the persona is shared by every allowed guild, so an admin of one changes it for all of them.

Codex reads `AGENTS.md` once when a thread starts and never re-reads it on resume, so every change
here retires all live threads: the instruction fingerprint moves and no existing thread resumes.
Members see the next message start a new conversation, and the retired threads are harvested into
personal memory as usual.

Both files are the operator's own, so neither is in git: copy `persona/AGENTS.example.md` to
`persona/AGENTS.md` and `config/output-style.example.md` to `config/output-style.md`, or upload
them at runtime. A clone with neither runs with no persona and no default style.

Output style has two layers. `config/output-style.md` is the operator's default; when it has
content it is injected as `<OUTPUT_STYLE>` into every prompt (rebuild the image after editing).
Each member can set their own with `/style text:…` for a one-liner, or `/style upload:True` to
send one Markdown file — a slash-command option is a single line, so anything longer belongs in a
file. The upload is UTF-8 `.md` bounded by 4000 characters, the same ceiling Discord puts on a
modal paragraph. Either way it is stored as `memory/<guild>/users/<member>/style.md` and injected
as `<PERSONAL_STYLE>`, which wins over the default where they conflict. Inspect it with `/style`
and return to the default with `/style clear:True`.

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

### Shared-link cleaning

`/<prefix>-linkclean` sets the per-server mode from a menu: `all` (default) replaces
links-only messages and appends clean links under messages that also carry text; `links`
only replaces links-only messages and never appends, so a message with text is left exactly as
posted (and with no Manage Messages nothing happens); `off` disables the member-visible part.
`status` reports the current mode. The server owner, Administrator, Manage Guild, or
operators listed in `LINKCLEAN_ADMIN_IDS` may use this command.

Two layers remove tracking parameters. The [ClearURLs](https://gitlab.com/ClearURLs/rules)
rule set (LGPL-3.0, 200+ site-scoped providers, fetched daily by `scripts/fetch_clearurls.py`
into `config/clearurls.json`) is applied first, including its known redirector unwrapping
(for example `google.com/url?q=`); its affiliate-id (`referralMarketing`) and request-blocking
(`completeProvider`) rules are not used. The Bot's own blocklist (`utm_*`, click ids such as
`fbclid`, `gclid`, `ttclid`, `igsh`) is then applied as the floor, so cleaning still works
with no rules file. Generic application parameters such as `ref` and `source` remain.
YouTube `si`/`feature` and Threads `xmt` are removed only on those hosts. Recognized signed
URLs are left intact. This cannot identify every site's custom signing or routing scheme.

`/<prefix>-embedfix` (default on) additionally swaps post links on X, Threads, TikTok, Pixiv
and Tumblr to embed-fixer proxies (`vxtwitter.com` / `fixupx.com`, `vxthreads.com`,
`tnktok.com` / `tiktxk.com`, `phixiv.net`, `tpmblr.com`) so Discord previews the video or image; a human who clicks is
sent back to the original site. Before swapping, the Bot fetches the proxy page as Discord's
crawler would and keeps the original link unless that page carries a card, so
a proxy that is down or blocked never replaces a working link. A card means a video or image
tag; for Threads it may instead be the post's own text, because a text post has no picture and
is exactly the post whose native preview is worth replacing. That relaxation is guarded: a
share code the proxy cannot resolve still answers 200 with a card, so the generic placeholder
description it serves there is refused. Pixiv works are rated through
Pixiv's public illust endpoint first: R-18 / R-18G links are delivered as `||spoilers||` so
Discord blurs the preview, and a work whose rating cannot be read is not swapped at all. X posts
are rated the same way through the vxtwitter API's `possibly_sensitive` flag. A link
the member already spoilered stays spoilered in every copy the Bot posts, and a message that
is only a spoilered link still counts as links-only. Replying to one of these reposts does not
wake the Bot by itself (Discord's reply ping lands in `mentions`); a typed `@Bot` in the reply
does, and the repost's links are then folded into the question. Delivery follows the linkclean mode above. Instagram, Reddit and Bluesky are not proxied: no live proxy that redirects humans
and beats the native preview was found (verified 2026-09-16).

With Manage Messages in the channel, link-only messages are reposted with author attribution
before deleting the original. Emoji and punctuation are preserved. A link-only reply is
reposted as a reply to the same message, so it stays in its conversation. Attachments,
stickers, forwards, thread starters and oversized replacements keep the original; clean links
are appended instead. Without Manage Messages, clean links are always appended. If deletion fails,
both messages may remain. The switch controls visible reposts; internal link cleanup stays on.

## Operations

```bash
# Status
docker compose ps
docker compose exec bot codex login status

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
docker compose exec bot codex --version
docker compose exec bot codex login status
```

### Daily name tables

The Taiwan/mainland name tables the hexdata API relies on (`config/lol-names.json` for the
Bot, `permanent/topics/*譯名對照.md` for the model) are generated from Data Dragon,
CommunityDragon and hexdata by `scripts/build_lol_names.py`. `scripts/daily_rebuild.sh` runs it
from the operator's crontab at 05:00 (after the 02:00 memory consolidation) and rebuilds the
image **only when a table changed** — a new champion, augment or item arrives with a patch,
not every day. On a change it commits `config/lol-names.json` locally (never pushes). Log:
`logs/daily-rebuild.log`. To run it by hand: `scripts/daily_rebuild.sh` (`DRY_RUN=1` to see
what it would do without rebuilding).
