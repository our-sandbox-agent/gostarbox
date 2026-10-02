# Reference production image for #10 — NOT yet built or runtime-verified.
# Build + runtime acceptance (reclone, cold recovery) pending #8 GO and a real build host.
# Pinning pattern follows diagnostics/gvisor/Dockerfile; digest reused from there.
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
USER root
# Snapshot pins all Debian dependencies, not only the top-level packages.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20260926T000000Z bookworm main' \
      'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20260926T000000Z bookworm-security main' \
      > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git tmux procps && \
    dpkg-query -W > /usr/local/share/work-image-dpkg.txt && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /opt/claude
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev && \
    test -x /opt/claude/node_modules/@anthropic-ai/claude-code-linux-x64/claude && \
    ln -s /opt/claude/node_modules/@anthropic-ai/claude-code-linux-x64/claude /usr/local/bin/claude && \
    npm cache clean --force
COPY image/entrypoint.sh /usr/local/bin/entrypoint.sh
# Reserve non-root home dirs for IDE mode (#52); no code-server in this ticket.
RUN chmod 0755 /usr/local/bin/entrypoint.sh && \
    mkdir -p /workspace && chown node:node /workspace && \
    install -d -o node -g node /home/node/.config /home/node/user-data /home/node/extensions
ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    TERM=xterm-256color
USER node
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
