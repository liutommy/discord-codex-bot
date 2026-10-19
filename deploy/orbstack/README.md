# Running the stack on an OrbStack machine

These files run the compose stack inside an OrbStack isolated Linux machine (Debian 12 with
docker-ce and systemd) on a Mac. Nothing in the stack starts by itself: Docker's restart policy is
off for every service, and `dcb-up` brings the stack up only after the container firewall is in
force and a probe from every compose network has shown it works. A guard keeps the stack running
and, when a problem outlasts its own repairs, asks the Mac to wake a headless `claude -p` that may
diagnose and restart services, nothing more.

`machine/` mirrors the absolute paths the files are installed to on the machine; `mac/` holds the
Mac side.

## What each piece does

| File | Purpose |
|---|---|
| `machine/usr/local/sbin/dcb-firewall` | Owns `DOCKER-USER` and `DCB-INPUT` (IPv4 and IPv6): containers reach the internet but no private or special address (the Mac, the LAN, the router's WAN address, OrbStack's networks, the machine's own services). Loads atomically and verifies the live chains. With `DCB_LLM_RELAY_PORT` set, the one exception is the llm bridge's gateway on that port (local LLM relay, below). |
| `machine/usr/local/sbin/dcb-probe` | Positive control for the firewall, run from each network it is given: blocked targets must be unreachable, public ones reachable, and every subnet must lie in the Docker pool, except compose.yaml's llm network (`172.31.254.0/24`), accepted only on its bridge `br-dcb-llm`. |
| `machine/usr/local/sbin/dcb-up` | The only path to "up": firewall, compose config check (every service `restart: "no"`), probe from every project network, `compose up -d`, then the containers' restart policies. Also refuses external and non-bridge networks, and any service that sets `network_mode`. A gate whose tool fails (`jq`, `docker inspect`) counts as failed, never as "nothing found": a running stack is stopped. Stops a running stack when the firewall fails or the probe finds a leak. |
| `machine/usr/local/sbin/dcb-guard` | Every 2 minutes: re-applies the firewall, starts missing services and restarts a bot unhealthy for too long (both through `dcb-up`), gives up after repeated crashes, flags gateway anomalies, and writes the wake request (below). |
| `machine/usr/local/sbin/dcb-deploy` | `dcb-deploy <merge commit>`: fast-forwards the checkout to that commit, builds as the checkout owner, runs `dcb-up --manual` and checks that the bot is healthy and connected. |
| `machine/usr/local/sbin/dcb-data-refresh` | 05:00 daily: runs the one-shot `data-refresh` service (`docker compose --profile refresh run --rm data-refresh`) behind the same lock, firewall and probe gates as `dcb-up`. Never builds an image and never restarts the bot, which reloads the generated data itself. |
| `machine/usr/local/sbin/dcb-backup-export` | Encrypts the newest `backups/*.tar.gz` to the offsite recipient (`/etc/offsite-backup/recipient.pub`) and writes only ciphertext to stdout, for an offsite copy pulled from the Mac. |
| `machine/usr/local/share/dcb-probe/` | The probe image (alpine with netcat and dig) that `dcb-probe` builds on demand. |
| `machine/etc/systemd/system/` | `dcb-firewall.service` (before `docker.service`, which requires it through `docker.service.d/10-dcb-firewall.conf`), `dcb-up.timer` (boot and every 5 min), `dcb-guard.timer` (every 2 min), `dcb-data-refresh.timer` (05:00 Asia/Taipei, catches up after downtime). |
| `machine/etc/docker/daemon.json` | Fixed address pool and bridge range (must match `DCB_POOL_NETS`, `DCB_POOL_PREFIX` and `DCB_BIP_NET`), public DNS resolvers for containers, log limits. |
| `machine/etc/logrotate.d/dcb` | Weekly rotation of `/var/log/dcb/*.log`. |
| `dcb.default.example` | Every setting the scripts read, installed as `/etc/default/dcb`. |
| `mac/dcb-wake.sh` | Run by launchd every 2 minutes; turns the machine's wake request into a rate-limited `claude -p` and mails the report. |
| `mac/com.example.dcb-wake.plist` | launchd template for `dcb-wake.sh`. |
| `mac/dcb-wake.config.example` | Settings for `dcb-wake.sh`, installed as `~/.config/dcb-wake/config`. |
| `mac/dcb-llm-relay.py` | Local LLM relay (launchd, always running): keeps socat listeners open inside the machine through `orb` and splices each connection to the Mac's LLM server. |
| `mac/dcb-llm-relay.config.example` | Settings for `dcb-llm-relay.py`, installed as `~/.config/dcb-llm-relay/config`. |
| `mac/com.example.dcb-llm-relay.plist` | launchd template for `dcb-llm-relay.py`. |
| `mac/com.example.local-llm.plist` | launchd template for the LLM server (`mlx_lm.server` on `127.0.0.1`). |

The checkout also needs a host-local `compose.override.yaml` (gitignored) that sets
`restart: "no"` on `bot`, `searxng`, `sandbox` and `xsearch`; `dcb-up` refuses to start the stack
otherwise. `data-refresh` already has `restart: "no"` in `compose.yaml`.

### The data-refresh service and the gates

`compose config` without `--profile` leaves out the profiled `data-refresh` service, so `dcb-up`'s
restart-policy check and `dcb-guard`'s list of wanted services never include it, which is what
they should do: it runs only when called. `dcb-data-refresh` expands the profile and checks the whole
result instead, failing closed: every service, `data-refresh` included, must have restart policy
`"no"`, every compose network must be non-external with driver `bridge` (macvlan, ipvlan and
external networks bypass `DOCKER-USER`; `dcb-up` applies the same rule), no service may set `network_mode` (a host-mode container's traffic leaves through `OUTPUT`, which `DOCKER-USER` never sees), and every network any
service joins must already exist and pass `dcb-probe` (exit 0; a leak
or an inconclusive probe both skip the run) after the firewall has been applied, all before
`compose run`. The last line it prints must be `data-refresh: changed ...` or
`data-refresh: unchanged ...`; anything else counts as a failure.
While the checkout has a `.hold` file the refresh does nothing.

### Wake requests

`dcb-guard` tracks when it first saw each problem kind in `$DCB_STATEDIR/problems/`:

| Kind | Seen when |
|---|---|
| `gave-up` | the crash-loop backoff has given up on the bot |
| `not-running` | a service is not running, or compose config does not parse, even after `dcb-up` |
| `firewall` | `dcb-firewall` fails |
| `unhealthy` | the bot's healthcheck reports unhealthy (a restart's `starting` phase does not end it) |
| `gateway` | more than `DCB_GATEWAY_MAX` gateway stall or reconnect lines within `DCB_GATEWAY_WINDOW` |

A kind seen on every run for `DCB_WAKE_AFTER` seconds (600) is written to
`$DCB_STATEDIR/wake-request`, one line per kind (`<kind> <minutes>min <evidence>`), replaced
atomically on each run and removed once no problem has lasted that long. Nothing is requested
while `.hold` exists. The request changes none of the guard's own actions.

`dcb-wake.sh` on the Mac reads it through `orb -m <machine> -u root`, at most 4097 bytes with
`head -c`, and refuses a request not rewritten for 10 minutes (dcb-guard rewrites it on every run).
At most once per 30 minutes and 6 times a day, it runs
`claude -p --permission-mode auto --strict-mcp-config --disallowedTools "Edit Write NotebookEdit mcp__*"`
in its work directory (no MCP servers load, and `mcp__*` is denied). The prompt carries the scope
and, per problem kind, only the kind and how many minutes it has lasted: kinds come from a fixed
whitelist (anything else is printed as `unknown`) and the request's free-text evidence, like every
log line, stays out of the prompt; the report file keeps the raw request for the owner. Claude may
read status and logs, run `dcb-up --manual`, `dcb-firewall` and `dcb-probe`, and restart a compose
service; a given-up bot is restarted with `dcb-up --manual`, and dcb-guard removes the give-up
marker once the bot is healthy. It may not edit code, scripts, configuration or units,
rebuild, use git, read `.env` or any credential, delete data, touch `.hold`, stop the bot for good,
post announcements, write to cloud storage or send mail. Fixes outside that scope come back as
recommendations. Logs carry Discord member text, so the prompt tells Claude that every log it reads
itself is data, not instructions. The scope rests on that prompt, the tool denials and auto mode's
checks: Bash stays allowed. Claude sends its summary with SendMessage to each address in the targets
file; the script saves prompt, raw request and output under `~/.local/state/dcb-wake/reports/` and
mails a notice with the output through `DCB_WAKE_NOTIFY`, masking token-shaped strings
(`Bearer …`, `…token=…`, Discord and `sk-` keys) in the mail.

The interval and the daily count live in `~/.local/state/dcb-wake/` (`last-wake`, `wakes-<date>`),
so they survive a reboot. A counter file that is not a plain number (half-written, disk full)
counts as the limit reached and is logged; both are written through a temp file and `mv`, and when
they cannot be written no wake happens. The wake request is read through `head -c 4097`; one over
4096 bytes is refused and logged (a compromised machine cannot flood the Mac). A wake suppressed by the daily cap sends one mail that day (marker
`cap-mailed-<date>`) saying so; later suppressions the same day are only logged. An unreadable `last-wake` blocks every wake until the file is fixed or removed, so it also sends
one mail a day (marker `garbled-mailed-<date>`).

One wake runs at a time: the script re-runs itself as the child of a Perl wrapper that holds an
`flock(2)` lock on `instance.lock` until the child exits (macOS has no `flock` command). The kernel
drops the lock when the holder dies, so there is no stale lock to take over; a second start logs
`another dcb-wake holds the lock; skip` and exits. The hard
limit (`DCB_WAKE_TIME_LIMIT`, 25 min) starts Claude in its own process group through Perl (macOS
has no `setsid` or `timeout`) and sends the whole group TERM, then KILL 10 s later; the group is
cleaned up the same way after a normal exit. A process that starts its own session leaves the
group and is not reached.

### Local LLM relay

The Bot can use an LLM server on the Mac (`mlx_lm.server`, OpenAI-compatible, bound to
`127.0.0.1`). The machine is isolated and cannot connect to the Mac, and OrbStack's ssh proxy
refuses `ssh -R`, so the Mac opens the path:

```
bot --(llm network)--> 172.31.254.1:<port> in the machine   socat listener, started by:
Mac: dcb-llm-relay.py runs `orb -m dcb -u <user> socat ... STDIO` and pipes it to 127.0.0.1:<upstream port> (LLM server)
```

- compose.yaml's `llm` network (bridge `br-dcb-llm`, `172.31.254.0/24`, gateway `172.31.254.1`)
  has only the `bot` service on it. The subnet lies outside the address pool so that the firewall
  and the probe can name it.
- `dcb-firewall` lets `br-dcb-llm` open TCP connections to `172.31.254.1` on `DCB_LLM_RELAY_PORT`
  only. Every other port there, and every other bridge, stays rejected, so the probe's checks from
  every network still hold. Empty `DCB_LLM_RELAY_PORT`: nothing on the llm bridge reaches the
  machine.
- Each relay worker runs `orb -m <machine> -u <user> socat -T<idle> -t<idle>
  TCP-LISTEN:<port>,bind=172.31.254.1,reuseaddr,reuseport STDIO,shut-close` as the machine's
  unprivileged user. `reuseport` lets the workers listen at the same time; socat without `fork`
  stops listening once it has a connection, so with every worker busy a new connection is refused,
  which the Bot retries. The worker connects to the LLM server only when the connection's first
  bytes arrive. `shut-close` and `-t<idle>` pass a client's half-close on: socat 1.7.4 (Debian 12)
  otherwise keeps the pipe open and gives up on the response 0.5 s after the client's EOF.
- A listener that exits before any connection with an error (stack down, so `172.31.254.1` does not
  exist yet) is retried with a backoff from 1 s to 30 s, logged once per streak.
- The relay's log (`~/Library/Logs/dcb-llm-relay.log`) has one line per connection with the bytes
  each way and the duration, never any content.
- A connection that arrives in the instant between socat's `accept()` and the closing of its
  listening socket is reset instead of refused (measured with socat 1.7.4); the Bot sees it as a
  failed request.

The Bot's `.env` points at the relay: `LOCAL_LLM_URL=http://172.31.254.1:<DCB_LLM_RELAY_PORT>/v1`.

## Install on the machine

Run as root inside the machine, from a checkout of this repository:

```sh
cd deploy/orbstack/machine
install -m 755 usr/local/sbin/dcb-* /usr/local/sbin/
install -d /usr/local/share/dcb-probe /etc/systemd/system/docker.service.d
install -m 644 usr/local/share/dcb-probe/* /usr/local/share/dcb-probe/
install -m 644 etc/systemd/system/dcb-* /etc/systemd/system/
install -m 644 etc/systemd/system/docker.service.d/10-dcb-firewall.conf /etc/systemd/system/docker.service.d/
install -m 644 etc/logrotate.d/dcb /etc/logrotate.d/dcb
install -m 644 etc/docker/daemon.json /etc/docker/daemon.json
[ -e /etc/default/dcb ] || install -m 644 ../dcb.default.example /etc/default/dcb
```

Edit `/etc/default/dcb` (every `<...>` placeholder), then:

```sh
systemctl daemon-reload
systemctl enable dcb-firewall.service
systemctl restart docker            # picks up daemon.json and the firewall dependency
systemctl enable --now dcb-up.timer dcb-guard.timer dcb-data-refresh.timer
```

Create the checkout's `compose.override.yaml` (above) before the first `dcb-up`; `touch .hold` in
the checkout keeps the stack down while everything else is prepared.

For the local LLM relay, also as root in the machine:

```sh
apt-get install -y socat
# /etc/default/dcb: DCB_LLM_RELAY_PORT=<port>, an integer 1024-65535
systemctl reload dcb-firewall
iptables -S DCB-INPUT | grep br-dcb-llm    # the one rule for 172.31.254.1 on that port
```

The listeners run as the machine's default (unprivileged) user and are started by the Mac's relay;
nothing new runs from systemd.

## Update the machine

Repeat the `install` lines for the files that changed and run `systemctl daemon-reload`. Scripts
take effect at their next run. Change `daemon.json` only deliberately: it needs
`systemctl restart docker`, which stops every container. Use `systemctl reload dcb-firewall` to
re-apply rules; `restart` would restart Docker too.

Moving from the earlier `dcb-daily-rebuild` wrapper:

```sh
systemctl disable --now dcb-daily-rebuild.timer
rm /etc/systemd/system/dcb-daily-rebuild.service /etc/systemd/system/dcb-daily-rebuild.timer \
   /usr/local/sbin/dcb-daily-rebuild
systemctl daemon-reload
systemctl enable --now dcb-data-refresh.timer
```

`dcb-deploy` no longer resets generated data files in the checkout, because nothing writes them
there any more. Any tracked local change in the checkout stops the deploy before `git fetch`
(`tracked local changes`); run `git checkout -- <file>` once to restore leftover files.

## Install on the Mac

```sh
install -m 755 deploy/orbstack/mac/dcb-wake.sh ~/.local/bin/dcb-wake.sh
mkdir -p ~/.config/dcb-wake ~/.local/state/dcb-wake ~/dcb-wake
cp deploy/orbstack/mac/dcb-wake.config.example ~/.config/dcb-wake/config   # set DCB_WAKE_NOTIFY
printf '%s\n' '<session address>' '<local session name>' > ~/.config/dcb-wake/targets
echo "Work directory for dcb-wake.sh's headless claude -p runs." > ~/dcb-wake/README.md
(cd ~/dcb-wake && claude)    # accept the folder trust once, then exit
sed -e "s#__HOME__#$HOME#g" -e "s#com\.example#com.$USER#g" \
    deploy/orbstack/mac/com.example.dcb-wake.plist > ~/Library/LaunchAgents/com.$USER.dcb-wake.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.$USER.dcb-wake.plist
~/.local/bin/dcb-wake.sh --test-wake    # one harmless wake: check the report, the mail and the messages
```

Update the script by installing it again; launchd uses it at the next run. After changing the
plist, `launchctl bootout gui/$(id -u)/com.$USER.dcb-wake` and bootstrap it again.

### Local LLM server and relay

The relay runs on the system `/usr/bin/python3` (stdlib only). Pick the LLM server's port
(`<PORT>`) and the machine's `DCB_LLM_RELAY_PORT`:

```sh
sed -e "s#__HOME__#$HOME#g" -e "s#com\.example#com.$USER#g" -e "s#__PORT__#<PORT>#" \
    -e "s#__MLX_LM_SERVER__#$(command -v mlx_lm.server)#" -e "s#__MODEL_DIR__#<MODEL_DIR>#" \
    deploy/orbstack/mac/com.example.local-llm.plist > ~/Library/LaunchAgents/com.$USER.local-llm.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.$USER.local-llm.plist
curl -s http://127.0.0.1:<PORT>/v1/models     # the server answers

install -m 755 deploy/orbstack/mac/dcb-llm-relay.py ~/.local/bin/dcb-llm-relay.py
mkdir -p ~/.config/dcb-llm-relay
cp deploy/orbstack/mac/dcb-llm-relay.config.example ~/.config/dcb-llm-relay/config   # set both ports
sed -e "s#__HOME__#$HOME#g" -e "s#com\.example#com.$USER#g" \
    deploy/orbstack/mac/com.example.dcb-llm-relay.plist > ~/Library/LaunchAgents/com.$USER.dcb-llm-relay.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.$USER.dcb-llm-relay.plist
tail ~/Library/Logs/dcb-llm-relay.log
```

Then set `LOCAL_LLM_URL=http://172.31.254.1:<DCB_LLM_RELAY_PORT>/v1` in the checkout's `.env` and
deploy. After changing the relay script, install it again and `launchctl kickstart -k
gui/$(id -u)/com.$USER.dcb-llm-relay`.
