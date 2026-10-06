# Control-plane service (v0.1 skeleton)

TypeScript + Hono implementation of `docs/contracts/control-plane-api.json`
(issue #123 = issue #76's sanctioned package 1: API/schema + single-tenant
trusted entry + contract tests). Same contract as
`scripts/control_plane_double.py`; the Python suite stays the executable spec
and the node:test suite in `test/contract.test.ts` is its port.

## Status — what is and is not real

- **In-memory persistence only** (`MemoryAdapter`, with `snapshot()/restore()`
  simulating an API restart). The `PostgresAdapter` is a placeholder that
  **throws on any use** — no fake persistence, no mock-success paths.
- **No Runner integration.** observed_state moves only through the
  Runner-evidence hooks (`cp.advance` / `cp.confirm` / `cp.expireLease` /
  `cp.markReconciled`), which are test stand-ins until #11 lands. HTTP 202 is
  acceptance, never success.
- Real Runner + Postgres integration, deployment, multi-tenant authorization:
  blocked on #11, which waits on the #8 GO (per the #76 packaging).

## Run

```sh
npm install
SANDBOX_TOKEN="$(openssl rand -hex 24)" npm start  # http://127.0.0.1:8787
```

- `SANDBOX_TOKEN` — the single bearer token required on every endpoint
  (required; startup fails if unset/blank, no hard-coded fallback; never logged).
- `PORT` — listen port, default `8787`. The server binds **127.0.0.1 only**:
  single trusted entry per the contract auth binding (loopback / SSH tunnel).

## Test / typecheck

```sh
npm test         # node --import tsx --test test/*.test.ts
npm run typecheck  # tsc --noEmit (strict)
```

The state machine is loaded from `docs/contracts/runner-lifecycle.json` at
startup — the transition table is never duplicated in TypeScript code, and
refuse-on-unknown applies. The capacity-admission API edge answers 429 per the
divergence note in `control-plane-api.json` (runner-side 409 reconciles at #11).

## Remaining local-skeleton limitations

- No rate limiter, request-body size ceiling, or idempotency retention cleanup.
  Keep this service on trusted local interfaces; it is not trial-ready.
- Terminal tickets are placeholders: predictable IDs are stored but not consumed,
  validated or expired; the advertised TTL is a contract value, not enforcement.
  Never use these as real attach credentials before #13 implements that boundary.
- `runtime_deadline_at` is recorded only; deadline updates and enforcement are
  not implemented. Do not rely on it to stop a process or spending.
- MemoryAdapter timestamps are logical counters, not wall-clock resource metering.
- HTTP smoke proves API responses and checks its log for the test token only;
  it does not verify runtime isolation, credential injection or broader secret handling.
