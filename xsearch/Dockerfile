# The X lookup sidecar: Grok Build (signed in with the operator's SuperGrok / X Premium+ plan)
# answering three fixed questions about X through xAI's server-side X search. Grok Build is a
# coding agent with a shell, file and web tools; here it gets none of them (a chat turn with
# images gets one tool that returns those images, nothing else). Three independent layers keep
# it that way (see server.py), and compose adds read-only rootfs, no caps, no root.
FROM python:3.12-slim-bookworm

# The binary itself, pinned by checksum (the official installer verifies nothing beyond TLS).
# One build per architecture: x86_64 for amd64 hosts, aarch64 for arm64 ones (an Apple Silicon
# Mac; "-linux-arm64" is a 404). TARGETARCH comes from BuildKit; the classic builder leaves it
# empty, so the build's own architecture stands in. Changing the version: update it and both
# checksums, then re-run /srv/smoke-layer2.sh (see README).
ARG GROK_VERSION=1.0.46
ARG GROK_SHA256_AMD64=41626a53292324140b92556b9d42ff5542e3dcd04aff85eafb8689dd4adb44fc
ARG GROK_SHA256_ARM64=45b0943e736f00a249b9cf02af2be9e0749d97c09a6f55cfcf3029a1a836f23e
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
         amd64) build=x86_64 sum="${GROK_SHA256_AMD64}" ;; \
         arm64) build=aarch64 sum="${GROK_SHA256_ARM64}" ;; \
         *) echo "no Grok build for this architecture" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://x.ai/cli/grok-${GROK_VERSION}-linux-${build}" -o /usr/local/bin/grok \
    && echo "${sum}  /usr/local/bin/grok" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/grok \
    && grok --version \
    && useradd --uid 1000 --create-home grok \
    && mkdir -p /var/lib/grok && chown grok:grok /var/lib/grok

# Root-owned, so Grok treats it as enforced organisation policy: a PreToolUse hook that denies
# every tool but the image one, and the image MCP server that policy pins by command.
COPY requirements.toml /etc/grok/requirements.toml
COPY tool-gate /usr/local/bin/tool-gate
COPY server.py image_mcp.py /srv/
COPY smoke-layer2.sh /srv/smoke-layer2.sh
COPY smoke-mentions.py smoke-images.py /srv/
RUN chmod 0644 /etc/grok/requirements.toml /srv/image_mcp.py && chmod 0755 /usr/local/bin/tool-gate

USER grok
WORKDIR /tmp
# The volume at /var/lib/grok holds only the login (auth.json). Each lookup runs Grok in a
# fresh home under /tmp (tmpfs) with a copy of it; see server.py.
ENV XSEARCH_AUTH_DIR=/var/lib/grok XSEARCH_SCRATCH=/tmp XSEARCH_PORT=8090
EXPOSE 8090
CMD ["python3", "/srv/server.py"]
