# Terminal protocol contract

Status: **Contract delivered (Proposed); runtime implementation pending.** No terminal
backend exists yet: the ttyd-vs-own-PTY choice is an explicit OPEN decision and **no
runtime verification is claimed or performed.** The machine-checked table is
[terminal-protocol.json](terminal-protocol.json), validated by
`scripts/verify_terminal_protocol.py` (stdlib-only, offline), in the same idiom as the
[runner lifecycle contract](runner-lifecycle.md). Sources: the
[sandbox lifecycle ADR](../adr/sandbox-lifecycle.md) (Proposed; section 1 detach
semantics, section 3 unified entry and state gate) and the
[runner/language ADR](../adr/runner-language.md) (Accepted; the CLI goes through the
control plane, never the Runner directly). Issue #13 owns acceptance; per its 2026-10-02
note the contract proceeds in parallel to #76 and the ticket closes only with runtime
integration.

## Scope

- **Contract only**: the frames, error codes and semantics one terminal attachment
  speaks as JSON text frames over a single WebSocket — transport-agnostic, at the
  unified `console.<domain>` entry from ADR section 3.
- Both clients — the Go CLI (#14) and the browser (#15) — consume this same contract.
  There is no second terminal protocol.

## What it covers

- **10 frame types**, every frame carrying `seq` (per-direction monotonic) and `ts`
  (Unix ms): `hello` (token, sandbox_id, requested_size, optional generation),
  `hello_ack` (generation, cols, rows) / `hello_err` (code, message), `input` (UTF-8
  data; Ctrl-C and other control keys ride as raw bytes, the server never interprets
  them), `output` (UTF-8 stream text, ANSI passthrough, optional `origin`
  live/replay and notice `code`), `resize` (cols/rows), `resized` (notification to
  every attached window), `ping`/`pong`, `bye` (reason, optional code).
- **8 error codes as a closed set**: `auth_failed`, `sandbox_not_found` (unknown,
  destroyed, or cross-tenant — same answer, no probe signal), `sandbox_not_active`
  (every state outside Active/Idle, including Suspend; the client requests Active via
  the state endpoint first, per ADR section 3), `wrong_generation` (stale client must
  re-read state and attach fresh to the recreated instance), `backpressure_overflow`
  (fatal bye), `output_limit_exceeded` (non-fatal drop-to-sync notice),
  `invalid_frame`, `rate_limited`.
- **Detach and reconnect**: a client `bye` or dropped socket never kills the tmux
  session or its processes; a new attach replays the current visible screen from a
  tmux capture. There is no server-side terminal content buffer in any form.
- **Two-window size policy (Proposed, explicit decision tracked by #13)**:
  last-resize-wins — each accepted `resize` is applied to the tmux session and every
  attached connection is told via `resized`; an older window never vetoes a newer
  resize. Bounds: cols and rows each 1..500; outside is `invalid_frame`.
- **Activity classification**: only `input` counts as user activity; ping/pong are
  liveness, resize/resized are layout, output is server stream. Separate numeric
  counters (user_input_frames/bytes, output_frames/bytes) feed the #19 idle policy
  and #23 ops. Counters are numbers only — never content.
- **Lifecycle binding**: the connection is bound to sandbox_id + generation at
  `hello`; on stop/destroy the server sends `bye` and only then closes (bye is always
  the final server frame). Reconnect after destroy answers `sandbox_not_found`;
  reconnect to a new generation is a fresh attach guarded by `wrong_generation`.
- **Backpressure**: a bounded per-connection server-side output buffer (size set at
  runtime integration). On overflow the server enters **drop-to-sync**: discards
  queued output (counted in `dropped_bytes`, content not kept), sends exactly one
  `output` frame with `code: output_limit_exceeded` and empty data. The first client
  frame after the notice exits the mode: the server sends one `replay` output frame
  (tmux capture) and resumes live forwarding. If `dropped_bytes` exceeds the recovery
  cap: `bye` with `backpressure_overflow`, then close; reconnect is allowed.

## OPEN: ttyd or own PTY

The backend choice is deliberately undecided and must not leak into the frame table —
either backend speaks the same JSON frames. Criteria to decide at runtime integration
(#13):

- Ctrl-C / control-byte passthrough fidelity end to end (no swallowing, no
  translation, no server-side signal synthesis).
- UTF-8 and ANSI escape passthrough without re-encoding damage.
- Resize forwarding into tmux and the last-resize-wins `resized` notification fit.
- Auth fit: control-plane token at `hello` (#12), credentials never in logs.
- Backpressure control: bounded buffer, drop-to-sync notice, capture-based resync.
  Wrapping ttyd's own protocol is possible but is the cost to weigh against owning
  the PTY bridge.
- Reconnect replay via tmux capture.
- Maintenance and testability: license, upgrade surface, headless CI testability,
  hooks for #23 observability.

## What it does NOT cover (non-goals)

- **No terminal recording** (不錄製終端): no content capture, no scrollback buffer, no
  playback. The verifier rejects any `record`/`scrollback`-style capability.
- **No collaborative editing**: two windows attach the same tmux session;
  last-resize-wins is the entire coordination story. No OT, no locks.
- **No second terminal protocol.**
- **No runtime claims**: nothing here is implemented, executed or measured.

## Runtime acceptance still owed (#13 checkboxes)

1. Choose ttyd or own PTY and record the decision — OPEN above; the protocol and
   error codes are recorded in the JSON contract.
2. Ctrl-C passthrough, UTF-8/ANSI, resize, backpressure and output limit under real
   load — unverified (backpressure buffer/cap values also unset until then).
3. Detach does not kill tmux; reconnect replays the old screen; two-window size
   policy in practice — unverified.
4. User input counted separately from ping/resize/output, wired into #19/#23 —
   classes and counters defined here, live wiring unverified.
5. Connections end correctly after stop/destroy and never attach across sandboxes or
   tenants — unverified.

## Verify

```
python3 scripts/verify_terminal_protocol.py
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Both are pure-stdlib and offline. The unittest file also contains guard tests that
mutate the contract in-memory and assert the verifier rejects each broken rule,
including the no-recording rule (in the spirit of CONTRIBUTING's test rule for
pure-logic modules).
