# Diagnostic image for #8, not the production image from #10.
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
USER root
# Snapshot pins all Debian dependencies, not only the top-level packages.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260926T000000Z bookworm main' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260926T000000Z bookworm-security main' \
      > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git tmux python3 procps && \
    dpkg-query -W > /usr/local/share/diagnostic-dpkg.txt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /opt/claude
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev && \
    test -x /opt/claude/node_modules/@anthropic-ai/claude-code-linux-x64/claude && \
    ln -s /opt/claude/node_modules/@anthropic-ai/claude-code-linux-x64/claude /usr/local/bin/claude && \
    npm cache clean --force && \
    mkdir -p /workspace && chown node:node /workspace
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    TERM=xterm-256color
USER node
WORKDIR /workspace
CMD ["sleep", "infinity"]
