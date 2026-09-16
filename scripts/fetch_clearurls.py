#!/usr/bin/env python3
"""Fetch the ClearURLs rule set into config/clearurls.json (deterministic, sorted).

    uv run python scripts/fetch_clearurls.py

Upstream: https://gitlab.com/ClearURLs/rules (LGPL-3.0). Providers whose regex Python
cannot compile are dropped here, at fetch time, so the Bot never loads a rule it would only
skip with a warning at every start. Fewer than MIN_PROVIDERS usable providers means the
upstream format moved: the file is left untouched and the exit code is non-zero, so the
daily job keeps the last good copy."""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

URL = "https://rules2.clearurls.xyz/data.minify.json"
OUT = Path(__file__).resolve().parent.parent / "config" / "clearurls.json"
MIN_PROVIDERS = 100
LISTS = ("rules", "rawRules", "exceptions", "redirections", "referralMarketing")


def usable(name: str, spec: dict) -> bool:
    if not isinstance(spec, dict) or not isinstance(spec.get("urlPattern"), str):
        return False
    try:
        re.compile(spec["urlPattern"], re.IGNORECASE)
        for key in LISTS:
            for pattern in spec.get(key) or ():
                re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        print(f"drop {name}: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    with urllib.request.urlopen(URL, timeout=30) as response:
        data = json.load(response)
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        print("clearurls: no providers in upstream payload", file=sys.stderr)
        return 1
    kept = {name: spec for name, spec in providers.items() if usable(name, spec)}
    if len(kept) < MIN_PROVIDERS:
        print(
            f"clearurls: only {len(kept)} usable providers; keeping the old file", file=sys.stderr
        )
        return 1
    text = json.dumps({"providers": kept}, sort_keys=True, ensure_ascii=False) + "\n"
    changed = not OUT.exists() or OUT.read_text(encoding="utf-8") != text
    if changed:
        OUT.write_text(text, encoding="utf-8")
    print(
        f"clearurls: {len(kept)} providers ({len(providers) - len(kept)} dropped), "
        f"{'updated' if changed else 'unchanged'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
