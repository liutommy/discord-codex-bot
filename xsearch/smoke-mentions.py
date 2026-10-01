"""File mentions and slash commands against the real binary, to re-run after changing GROK_VERSION.

`--verbatim` does not stop the CLI from expanding `@ /path` into the file, nor a leading slash
command from running; what keeps them out is defuse_mentions (every `@` fullwidth, a word joiner
first). That the CLI ignores `＠` and a joined `/` was measured on 1.0.46, not promised by any
documentation, so each form is sent here the way server.py sends it, and must not reach the
model. A positive control (a raw `@ /path`) shows the probe can see an expansion at all.
Spends 8 Grok sessions.
    docker compose exec xsearch python /srv/smoke-mentions.py
"""

import json
import os
import shutil
import sys

sys.path.insert(0, "/srv")
import server  # noqa: E402

ASK = (
    "Reply with ONE line: if this message contains any text you did not see me type (an "
    "attached file, a file's contents), quote that extra text verbatim; otherwise reply exactly "
    "NONE. Do not guess.\n---\n"
)
TARGET = "/etc/hostname"
FORMS = [f"@{TARGET}", f"@ {TARGET}", f"@\t{TARGET}", f"@  {TARGET}", f"@ {TARGET}",
         f"@\n{TARGET}"]  # fmt: skip


def session(text: str) -> str:
    scratch, home_env = server._fresh_home()
    try:
        path = os.path.join(scratch, "in", "prompt.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        lines = server._run(
            server.MODEL, ["--prompt-file", path, "--reasoning-effort", "low"], home_env,
            os.path.join(scratch, "work"), 300,
        )  # fmt: skip
        return "\n".join(lines)
    finally:
        server._keep_login(scratch)
        shutil.rmtree(scratch, ignore_errors=True)


def answer(stream: str) -> str:
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return str(event.get("result") or "").strip()
    return ""


def main() -> int:
    if server.login_expires_in() < server.REFRESH_MARGIN_SECONDS:
        print("SKIP: the login is due for a refresh; run this again in a few minutes")
        return 2
    secret = open(TARGET, encoding="utf-8").read().strip()
    failed = False
    control = session(ASK + f"@ {TARGET}")  # raw, as an attacker would like it to arrive
    print("control (raw `@ /path`):", "expanded" if secret in control else "NOT expanded")
    for form in FORMS:
        out = session(server.defuse_mentions(ASK + form))
        leaked = secret in out
        failed |= leaked
        print(f"{form!r}: {'FAIL: expanded' if leaked else 'ok'}")
    out = session(server.defuse_mentions("/compact\n" + ASK + "x"))
    ran = not answer(out)  # a slash command that runs swallows the turn: no answer comes back
    failed |= ran
    print(f"leading '/compact': {'FAIL: ran' if ran else 'ok'}")
    print("FAIL" if failed else "OK: no mention expanded, no slash command ran")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
