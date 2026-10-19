FROM alpine:3.22
RUN apk add --no-cache netcat-openbsd bind-tools
COPY probe.sh /probe.sh
USER 65534:65534
ENTRYPOINT ["/bin/sh", "/probe.sh"]
