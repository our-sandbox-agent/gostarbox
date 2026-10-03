# Console datasource contract

Status: **demo mode delivered (default); real-API client delivered (async,
server-authoritative surface). Real-mode UI wiring NOT delivered here** —
`src.js` still consumes the demo datasource only; wiring the console to the
async client, the mode banner, and the terminal WebSocket mount are the
remaining #15 runtime work (terminal side blocked on #13). Source of truth:
`console-datasource.js`, tested by `console-datasource.test.js` (node --test).
The API the client speaks: `docs/contracts/control-plane-api.json`, served by
`server/` (issue #123 = #76 package 1). Owner: issue #15.

## Scope

The datasource is the only layer through which the console reads and writes sandbox
state and auto-tiering policy. `src.js` holds no direct `localStorage` state access.
Demo mode preserves today's behavior exactly — same keys (`sandbox-v1`,
`sandbox-policy`), same `{revision, boxes}` wrapper format, same tab-sync revision
guard — so there is **zero data migration**. This slice does not touch the terminal
mount (xterm/WebSocket survival across search/tab-switch/redraw is separate work
under #15, blocked on #13/#77 runtime).

## Interface (demo implements)

| Member | Behavior |
|---|---|
| `mode` | Always `'demo'` (`MODE_DEMO`). |
| `read()` | `readState(storage, 'sandbox-v1')`: `{revision, boxes}` or `null`; legacy bare-array records load as revision 0. |
| `createSync({revision, get, adopt})` | `createTabSync` bound to `sandbox-v1`: revision-guarded `write()`, `refresh()`, `onStorage(event)` with wholesale adoption of newer stored states. |
| `readPolicy()` | Parsed `sandbox-policy` JSON, or `null` when absent/malformed (caller normalizes). |
| `writePolicy(policy)` | JSON-serializes to `sandbox-policy`; quota errors are swallowed (change lives until page close). |

Mode selection: `resolveMode({apiBase, mode})` → `MODE_DEMO` (default, no API
configured) or `MODE_REAL` (`apiBase` non-empty); an explicit `mode` must be one of
the two known values, anything else throws. Demo stays the default for the Pages
deployment.

## Real-API client (delivered)

`createApiDatasource({apiBase, token, fetchImpl = fetch})` returns an async client
against `docs/contracts/control-plane-api.json`. `fetchImpl` is injectable (tests,
#15 wiring). Both `apiBase` and `token` are required — the factory throws on a
missing one, so real mode is never accidental.

**This is not the demo interface and does not fake it.** The server is
authoritative for state and usage (issue #15 2026-10-02 note: 不得照搬
localStorage 計時), so the client has no `read()`/`createSync()`/
`readPolicy()`/`writePolicy()`: no localStorage writes, no tab-sync revision
guard, no client-side timing inference. Until #15's wiring lands, `src.js`
stays demo-only and nothing here silently impersonates demo storage.

| Member | Contract call | Resolves with |
|---|---|---|
| `mode` | — | Always `'real'` (`MODE_REAL`). |
| `list()` | `GET /v1/sandboxes` | `{sandboxes: [...]}` (Destroyed tombstones not listed). |
| `get(id)` | `GET /v1/sandboxes/{id}` | Sandbox view (version, pending_operation, …; tombstone still queryable). |
| `create(body, {idempotencyKey?})` | `POST /v1/sandboxes` | 202 `{sandbox_id, operation}` (type create, state pending). |
| `requestState(id, body, {idempotencyKey?})` | `POST /v1/sandboxes/{id}/state` | 202 `{operation}` or 200 `{sandbox}` when the target is already synchronously achieved. |
| `destroy(id, body, {idempotencyKey?})` | `DELETE /v1/sandboxes/{id}` | 202 `{operation}` (type destroy). |
| `getOperation(id)` | `GET /v1/operations/{id}` | Operation resource. |
| `pollOperation(id, {timeoutMs, intervalMs})` | polls `GET /v1/operations/{id}` | The operation once terminal (`succeeded`/`failed`). |

**Error model.** Every failure rejects `ApiError {status, code, message}` parsed
from the contract's closed error set — 401 `unauthorized`, 404 `not_found`, 409
`version_conflict` / `body_conflict` / `credentials_required` /
`opposite_operation`, 422 `invalid` / `unsupported_suspend_mode`, 429
`capacity_exceeded`. A network failure (no HTTP answer at all) rejects
`ApiError` with `status: null, code: 'unreachable'`. A rejected
`credentials_required` or `capacity_exceeded` means **no operation was created**
(contract: `creates_operation: false`) — the rejection itself is the honest
surface, nothing is faked.

**Idempotency.** Each mutating call (`create`/`requestState`/`destroy`) sends a
fresh `Idempotency-Key` generated with `crypto.randomUUID()`; pass
`{idempotencyKey}` to reuse a key when retrying the same logical call (e.g.
after `unreachable`). Same key + same body replays the original operation; same
key + different body answers 409 `body_conflict`. The credential field never
participates in the comparison (contract rule; the client just forwards it).

**Polling.** `pollOperation` polls until the operation is `succeeded` or
`failed` — a `failed` operation **resolves** (it is a completed verdict with
`retryable: true`), it does not reject. On `timeoutMs` it rejects
`PollTimeoutError {code: 'poll_timeout', operation}` carrying the last observed
operation state: per the contract's runtime-honesty semantics a timeout is NOT
a failure verdict — the outcome stays unknown until lease expiry /
reconciliation resolves it, never an assumption.

## Integration boundary (remains #15 runtime)

- `src.js` wiring to this async client + real-mode banner + cold-resume copy
  (obligations below). Demo stays default for Pages (`resolveMode` unchanged).
- Terminal WebSocket / xterm mount and the `terminal-ticket` edge stay unwired
  here (#13 / #77 runtime).
- The client is exercised today by contract-fidelity tests (injected `fetchImpl`
  serving hand-written fixtures from `control-plane-api.json`) and by the live
  server below — never by mocked demo success.

## Real-mode obligations (UI wiring, #15)

- **Authoritative server state**: the server is the source of truth for sandbox
  state and usage. The localStorage revision guard and any client-side timing
  inference (restore-clock heuristics) must not be reused for multi-client state —
  after #76, multi-client consistency means the server's answer, not a localStorage
  race (issue #15 2026-10-02 note re #55).
- **Banner separation**: demo and real modes must be clearly distinguished in the
  UI (visible mode banner/copy); a real-mode session must never display the
  "本機模擬 / browser-local demo" framing, and vice versa.
- **Cold-resume copy**: real-mode suspend/resume and reconnect notices follow the
  persistence contract semantics (workspace preserved, new terminal generation
  announced explicitly — see the terminal protocol's `wrong_generation` / NEW
  session notice rule), not the demo "關閉頁面後計時暫停" copy.
- **No partial fallback**: once `resolveMode` returns `'real'`, the console must
  not silently fall back to localStorage for state it failed to fetch; failures
  surface as errors.

## Running the control plane locally

The client speaks the real service in `server/` — see `server/README.md`:
`npm install && npm start` there serves `http://127.0.0.1:8787` (loopback only)
with `SANDBOX_TOKEN` as the single bearer token; pass the same token into
`createApiDatasource` (the token is sent in the Authorization header and never
logged). Known boundary while #11 (runner) is open: HTTP 202 is acceptance
only — `observed_state` moves solely on Runner evidence, so operations polled
over plain HTTP stay `pending` and `pollOperation` times out honestly.

## What it does NOT cover (non-goals here)

- No `src.js`/UI rewiring to real mode, no WebSocket, no xterm mount refactor —
  those belong to the remaining #15 acceptance items.
- No UI redesign, no public signup or real billing (#15 本票不含).
