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
| `machine/usr/local/sbin/dcb-firewall` | Owns `DOCKER-USER` and `DCB-INPUT` (IPv4 and IPv6): containers reach the internet but no private or special address (the Mac, the LAN, the router's WAN address, OrbStack's networks, the machine's own services). Loads atomically and verifies the live chains. |
| `machine/usr/local/sbin/dcb-probe` | Positive control for the firewall, run from each network it is given: blocked targets must be unreachable, public ones reachable, and every subnet must lie in the Docker pool. |
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

`dcb-wake.sh` on the Mac reads it with `orb -m <machine> -u root cat <statedir>/wake-request` and,
at most once per 30 minutes and 6 times a day, runs
`claude -p --permission-mode auto --strict-mcp-config --disallowedTools "Edit Write NotebookEdit mcp__*"`
in its work directory (no MCP servers load, and `mcp__*` is denied). The prompt carries the scope
and, per problem kind, only the kind and how many minutes it has lasted: kinds come from a fixed
whitelist (anything else is printed as `unknown`) and the request's free-text evidence, like every
log line, stays out of the prompt; the report file keeps the raw request for the owner. Claude may
read status and logs, run `dcb-up --manual`, `dcb-firewall` and `dcb-probe`, restart a compose
service and remove the give-up marker; it may not edit code, scripts, configuration or units,
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
