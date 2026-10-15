# Postgres store — #135, first part of #76

Status: async storage implementation, **not wired into the HTTP server**.
The server still uses MemoryAdapter. #136 owns async API transaction wiring,
real process restart tests, capacity admission and request concurrency.
Database preparation does not require #8 GO; Docker/runsc Runner integration
and completion of #76 still require #11/#8.

## Durable boundary

`server/src/postgres.ts` exports `PostgresStore` and transaction-scoped methods.
It does not implement the synchronous `PersistenceAdapter`, provide a fake
snapshot-backed database, or connect to a database on import. The obsolete
throw-only PostgresAdapter placeholder was removed.

- `workspaces`: tenant scope and per-workspace write lock.
- `sandboxes`: composite workspace/id key; indexed version and generation;
  explicitly projected public record in JSONB.
- `operations`: identity, sandbox FK, generation and public operation record.
  Updates cannot rebind identity, target or expected version.
- `resources`: sandbox FK, kind, generation and quantity vector. Workspace/home
  volumes and runtime are explicit separate resources; callers choose their
  actual quantities, the store does not infer a volume split.
- `idempotency_keys`: workspace/key uniqueness, method/path, SHA-256 fingerprint,
  sandbox/operation references and optional original 200 response. A composite
  FK prevents associating an operation with another sandbox. 202 references an
  operation; 200 stores its original public response without an operation.
- `schema_migrations`: immutable migration identifier, checksum and applied time.

The JSON record format preserves the current contract without creating a second
lifecycle model. SQL protects identities and associations; lifecycle transitions,
public error text and payload validation remain the service's responsibility.
No delete/prune operation is provided. Tombstones and retention remain future work.

## Transactions and concurrency

```ts
const store = new PostgresStore(pool) // caller owns and closes pg.Pool
await store.migrate() // explicit startup/deployment step
await store.transaction(workspaceID, async tx => {
  await tx.putSandbox(sandbox, null) // insert, reject existing id
  await tx.putOperation(operation)
  await tx.putResource(resource)
  await tx.putIdempotency(entry)
})
```

Each callback has one dedicated connection and transaction. **Await every store
call**; do not retain or use the transaction after the callback. Failed callbacks
roll back every write. A PostgreSQL-aborted transaction cannot report successful
commit even if the callback swallowed its SQL exception.

For updates, `putSandbox(record, expectedVersion)` requires the next version to
be exactly expectedVersion+1 and rejects backward generation. `StoreConflict`
is a storage exception; mapping it to an HTTP conflict belongs to #136. Errors
are never silently retried. A retry should rerun the entire logical transaction.

Workspace writes serialize via a row lock, including across independent pools.
This is deliberately coarse for an internal single-workspace first version.
It is **not a global host-capacity lock**. #136 must make capacity admission
atomic across all workspaces, not just assume these locks prevent overselling.
No Runner/DB distributed atomicity is claimed. Ledger outbox belongs to #77.

## Secrets and ephemeral data

Unknown top-level sandbox/operation fields are not serialized. `has_credential`
is always persisted false; credential presence must be re-established externally.
There is no durable terminal ticket table. Public nested payloads reject known
credential key names; canonical request fingerprints are hashed before storage.
This is not arbitrary-text secret detection: public messages, URLs and identifiers
must already be validated/redacted by callers. Do not put credentials in them.
Never pass raw request objects as trusted operation targets or public responses.

`getIdempotency()` returns `fingerprintHash`, not the original fingerprint string.
The API integration must hash its canonical credential-excluded request using
SHA-256 before comparing. Original 200 responses are intentionally historical
replays, not fresh sandbox views.

## Migration and local verification

Use a dedicated local/test database. Set DATABASE_URL through local environment
configuration, never commit it. From the repository root:

```sh
npm --prefix server ci
npm --prefix server run db:migrate   # requires DATABASE_URL
npm --prefix server run test:postgres # requires GOSTARBOX_TEST_DATABASE_URL
```

Migration runs in one transaction with an advisory lock. Concurrent initializers
serialize, rerunning preserves data, checksum drift fails closed. Application SQL
uses a transaction-local search_path, so pooled connections do not retain it.
DDL needs schema/table privileges; production roles and deployment are not set up.

Tests use PostgreSQL 17, create a random `test_<uuid>` schema and drop only that
schema in cleanup. They never target the default application schema `sandbox_cp`.
Missing test DB configuration fails rather than skips. CI provisions PostgreSQL
and runs this suite separately from the fast memory-only contract tests.

Tests cover reconnect, concurrent migration, all-record rollback, stale versions,
workspace/FK boundaries, duplicate keys, immutable operation/resource identity,
no-op replay, secret fields, swallowed SQL errors, and expired transaction handles.
They do not claim API process recovery, real runtime state, provider calls or
multi-tenant authorization; those stay with #136/#11/#16.
