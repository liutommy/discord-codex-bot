# The X lookup sidecar: Grok Build (signed in with the operator's SuperGrok / X Premium+ plan)
# answering three fixed questions about X through xAI's server-side X search. Grok Build is a
# coding agent with a shell, file and web tools; here it gets none of them. Three independent
# layers keep it that way (see server.py), and compose adds read-only rootfs, no caps, no root.
FROM python:3.12-slim-bookworm

# The binary itself, pinned by checksum (the official installer verifies nothing beyond TLS).
# Changing the version: update both, then re-run /srv/smoke-layer2.sh (see README).
ARG GROK_VERSION=1.0.46
ARG GROK_SHA256=41626a53292324140b92556b9d42ff5542e3dcd04aff85eafb8689dd4adb44fc

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://x.ai/cli/grok-${GROK_VERSION}-linux-x86_64" -o /usr/local/bin/grok \
    && echo "${GROK_SHA256}  /usr/local/bin/grok" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/grok \
    && grok --version \
    && useradd --uid 1000 --create-home grok \
    && mkdir -p /var/lib/grok && chown grok:grok /var/lib/grok

# Root-owned, so Grok treats it as enforced organisation policy: a deny-all PreToolUse hook.
COPY requirements.toml /etc/grok/requirements.toml
COPY deny-all /usr/local/bin/deny-all
COPY server.py /srv/server.py
COPY smoke-layer2.sh /srv/smoke-layer2.sh
RUN chmod 0644 /etc/grok/requirements.toml && chmod 0755 /usr/local/bin/deny-all

USER grok
WORKDIR /tmp
# The volume at /var/lib/grok holds only the login (auth.json). Each lookup runs Grok in a
# fresh home under /tmp (tmpfs) with a copy of it; see server.py.
ENV XSEARCH_AUTH_DIR=/var/lib/grok XSEARCH_SCRATCH=/tmp XSEARCH_PORT=8090
EXPOSE 8090
CMD ["python3", "/srv/server.py"]
