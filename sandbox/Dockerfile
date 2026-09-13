# The sandbox sidecar image: a plain toolbox (python + a few libraries, ffmpeg, jq) and the exec
# server, running as an unprivileged user. Hardening (read-only rootfs, no caps, no egress) is
# applied by compose, not here.
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg jq \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pillow==11.3.0 pypdf==6.18.1 numpy==2.3.3 \
    && useradd --uid 1000 --create-home runner \
    && mkdir -p /work && chown runner:runner /work

COPY server.py /srv/server.py
USER runner
WORKDIR /work
ENV SANDBOX_WORK=/work SANDBOX_PORT=8070
EXPOSE 8070
CMD ["python3", "/srv/server.py"]
