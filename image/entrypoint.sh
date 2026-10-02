#!/bin/sh
# Work image entrypoint (#10) — reference skeleton, NOT runtime-verified.
# Lines marked NEEDS-RUNTIME require a real container run (#8 GO + build host).
set -u

SESSION_NAME="${WORK_SESSION:-work}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace}"
REPO_URL="${REPO_URL:-}"
CLONE_DEST="$WORKSPACE_DIR/repo"

log() { printf '[entrypoint] %s\n' "$*"; }

# NEEDS-RUNTIME: tmux server boot + fixed session name under non-root user.
if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    tmux new-session -d -s "$SESSION_NAME"
    log "started tmux session '$SESSION_NAME'"
else
    log "tmux session '$SESSION_NAME' already running"
fi

mkdir -p "$WORKSPACE_DIR"

# Idempotency contract: first start / restart / cold recovery all take the same
# path; never re-clone, never overwrite an existing repo, record the decision.
# NEEDS-RUNTIME: skip-clone across restart and cold recovery.
if [ -n "$(ls -A "$CLONE_DEST" 2>/dev/null)" ]; then
    log "workspace already populated at $CLONE_DEST, skipping clone"
elif [ -z "$REPO_URL" ]; then
    log "REPO_URL unset and workspace empty, starting bare"
else
    # argv form only: git clone -- "$URL" "$DEST"; never shell concatenation,
    # never tmux send-keys. URL validation contract: scripts/clone_args.py.
    # NEEDS-RUNTIME: real clone success/failure paths over HTTPS.
    if git clone -- "$REPO_URL" "$CLONE_DEST"; then
        log "cloned into $CLONE_DEST"
    else
        log "git clone failed, refusing to continue"
        exit 1
    fi
fi

# NEEDS-RUNTIME: agent handoff inside the tmux session (non-interactive shell
# handling per #70 findings; Claude startup/session/key per #75).
exec claude
