# The X lookup sidecar: Grok Build (signed in with the operator's SuperGrok / X Premium+ plan)
# answering three fixed questions about X through xAI's server-side X search. Grok Build is a
# coding agent with a shell, file and web tools; here it gets none of them. Three independent
# layers keep it that way (see server.py), and compose adds read-only rootfs, no caps, no root.
FROM python:3.12-slim-bookworm

ARG GROK_VERSION=1.0.46

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && HOME=/tmp/grok-install bash -c "curl -fsSL https://x.ai/cli/install.sh | bash -s ${GROK_VERSION}" \
    && install -m 0755 "$(readlink -f /tmp/grok-install/.grok/bin/grok)" /usr/local/bin/grok \
    && rm -rf /tmp/grok-install \
    && grok --version \
    && useradd --uid 1000 --create-home grok \
    && mkdir -p /var/lib/grok /work && chown grok:grok /var/lib/grok /work

# Root-owned, so Grok treats it as enforced organisation policy: a deny-all PreToolUse hook.
COPY requirements.toml /etc/grok/requirements.toml
COPY deny-all /usr/local/bin/deny-all
COPY server.py /srv/server.py
RUN chmod 0644 /etc/grok/requirements.toml && chmod 0755 /usr/local/bin/deny-all

USER grok
WORKDIR /work
# The login (auth.json) and session files live in the grok-home volume.
ENV GROK_HOME=/var/lib/grok XSEARCH_PORT=8090
EXPOSE 8090
CMD ["python3", "/srv/server.py"]
