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
npm start        # tsx src/main.ts -> http://127.0.0.1:8787
```

- `SANDBOX_TOKEN` — the single bearer token required on every endpoint
  (default `dev-insecure-token` for local development only; never set a real
  one through the default, never logged by the server).
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
