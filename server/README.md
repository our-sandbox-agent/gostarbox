# Control-plane service (v0.1 skeleton)

TypeScript + Hono implementation of `docs/contracts/control-plane-api.json`
(issue #123 = issue #76's sanctioned package 1: API/schema + single-tenant
trusted entry + contract tests). Same contract as
`scripts/control_plane_double.py`; the Python suite stays the executable spec
and the node:test suite in `test/contract.test.ts` is its port.

## Status — what is and is not real

- **HTTP still uses in-memory persistence** (`MemoryAdapter`, with
  `snapshot()/restore()` simulating an API restart). A separate async
  `PostgresStore` now provides real transactions and migrations; #136 will wire
  the HTTP service into it. See [Postgres store](../docs/contracts/postgres-store.md).
- **No Runner integration.** observed_state moves only through the
  Runner-evidence hooks (`cp.advance` / `cp.confirm` / `cp.expireLease` /
  `cp.markReconciled`), which are test stand-ins until #11 lands. HTTP 202 is
  acceptance, never success.
- Database API integration (#136) can proceed without #8 GO. Real Runner
  integration and full #76 acceptance still wait on #11/#8; deployment and
  multi-tenant authorization remain separate work.

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
npm run test:postgres # real Postgres; requires GOSTARBOX_TEST_DATABASE_URL
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
