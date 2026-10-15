import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { randomUUID, createHash } from 'node:crypto'
import { Pool } from 'pg'
import { PostgresStore, StoreConflict, type PostgresTransaction } from '../src/postgres.js'
import type { SandboxRecord, OperationRecord, IdempotencyEntry } from '../src/store.js'

const url = process.env.GOSTARBOX_TEST_DATABASE_URL
if (!url) throw new Error('GOSTARBOX_TEST_DATABASE_URL required: real PostgreSQL tests must not skip')
const schema = 'test_' + randomUUID().replaceAll('-', '')
const pool = new Pool({connectionString: url, max: 4})
const store = new PostgresStore(pool, schema)
const sb = (ws: string, id = 's1'): SandboxRecord => ({
  sandbox_id: id, workspace_id: ws, desired_state: 'Active', observed_state: 'Creating',
  version: 1, generation: 1, resources: {milli_cpu: 1000, memory_bytes: 4096, volume_bytes: 8192},
  repo_url: null, runtime_deadline_at: null, volumes: {workspace: 'v1', home: 'v2'},
  session: null, pending_operation: 'op1', last_confirmed_at: null, error: null,
  last_confirmed_state: null, reconciled: false, destroy_operation: null, has_credential: true, created_at: 1,
})
const op = (id = 'op1', sandbox = 's1'): OperationRecord => ({
  operation_id: id, sandbox_id: sandbox, type: 'create', trigger: 'create', target: {agent: 'claude'},
  state: 'pending', expected_version: null, generation: 1, result: null, error: null, created_at: 1, updated_at: 1,
})
const idem = (): IdempotencyEntry => ({key: 'request', method: 'POST', path: '/v1/sandboxes',
  fingerprint: '{"agent":"claude"}', status: 202, operation_id: 'op1', sandbox_id: 's1'})
before(async () => { await Promise.all([store.migrate(), store.migrate()]) })
after(async () => {
  await pool.query(`DROP SCHEMA IF EXISTS "${schema}" CASCADE`)
  await pool.end()
})

test('records survive pool restart and repeated concurrent migrations', async () => {
  const ws = 'restart'
  const initialPool = new Pool({connectionString: url})
  const initial = new PostgresStore(initialPool, schema)
  try { await initial.transaction(ws, async tx => {
    await tx.putSandbox({...sb(ws), credential: 'fake-secret'} as SandboxRecord, null)
    await tx.putOperation(op())
    await tx.putResource({resource_id: 'v1', sandbox_id: 's1', kind: 'workspace_volume', generation: 1,
      quantity: {milli_cpu: 0, memory_bytes: 0, volume_bytes: 8192}})
    await tx.putIdempotency(idem())
  })
  } finally { await initialPool.end() }
  const secondPool = new Pool({connectionString: url})
  try {
    const reopened = new PostgresStore(secondPool, schema)
    await Promise.all([reopened.migrate(), store.migrate()])
    await reopened.transaction(ws, async tx => {
      assert.deepEqual(await tx.getSandbox('s1'), {...sb(ws), has_credential: false})
      assert.deepEqual(await tx.getOperation('op1'), op())
      assert.equal((await tx.listResources('s1'))[0].quantity.volume_bytes, 8192)
      const entry = await tx.getIdempotency('request')
      assert.equal(entry?.fingerprintHash, createHash('sha256').update(idem().fingerprint).digest('hex'))
      assert.equal(entry?.operation_id, 'op1')
      assert.equal((await tx.listSandboxes()).length, 1)
    })
  } finally { await secondPool.end() }
  const dump = await pool.query(`SELECT record::text FROM "${schema}".sandboxes WHERE workspace_id=$1`, [ws])
  assert.ok(!JSON.stringify(dump.rows).includes('fake-secret'))
})

test('rollback removes all writes, including newly created workspace', async () => {
  await assert.rejects(store.transaction('rollback', async tx => {
    await tx.putSandbox(sb('rollback'), null)
    await tx.putOperation(op())
    await tx.putResource({resource_id: 'v', sandbox_id: 's1', kind: 'runtime', generation: 1, quantity: sb('rollback').resources})
    await tx.putIdempotency(idem())
    throw new Error('injected abort')
  }), /injected abort/)
  for (const table of ['workspaces', 'sandboxes', 'operations', 'resources', 'idempotency_keys']) {
    const column = table === 'workspaces' ? 'id' : 'workspace_id'
    const r = await pool.query(`SELECT count(*)::int AS count FROM "${schema}".${table} WHERE ${column}=$1`, ['rollback'])
    assert.equal(r.rows[0].count, 0)
  }
})

test('concurrent same-key writers reserve exactly once and rollback duplicate sandbox', async () => {
  const ws = 'concurrent'
  const results = await Promise.allSettled([1, 2].map(n => store.transaction(ws, async tx => {
    await tx.putSandbox(sb(ws, `s${n}`), null)
    await tx.putOperation(op(`op${n}`, `s${n}`))
    await tx.putIdempotency({...idem(), sandbox_id: `s${n}`, operation_id: `op${n}`})
  })))
  assert.equal(results.filter(r => r.status === 'fulfilled').length, 1)
  const rejected = results.find(r => r.status === 'rejected') as PromiseRejectedResult
  assert.ok(rejected.reason instanceof StoreConflict)
  await store.transaction(ws, async tx => {assert.equal((await tx.listSandboxes()).length, 1)})
})

test('optimistic version updates reject stale writers and backwards generation', async () => {
  const ws = 'version'; await store.transaction(ws, tx => tx.putSandbox(sb(ws), null))
  const results = await Promise.allSettled([1, 2].map(() => store.transaction(ws,
    tx => tx.putSandbox({...sb(ws), version: 2, generation: 2}, 1))))
  assert.equal(results.filter(r => r.status === 'fulfilled').length, 1)
  await assert.rejects(store.transaction(ws, tx => tx.putSandbox({...sb(ws), version: 3, generation: 1}, 2)), StoreConflict)
  await store.transaction(ws, async tx => {assert.equal((await tx.getSandbox('s1'))?.generation, 2)})
})

test('workspace scoping and foreign keys prevent cross-workspace and cross-sandbox associations', async () => {
  await store.transaction('owner', async tx => {await tx.putSandbox(sb('owner'), null); await tx.putOperation(op())})
  await store.transaction('other', async tx => {
    assert.equal(await tx.getSandbox('s1'), undefined)
    assert.equal(await tx.getOperation('op1'), undefined)
    assert.deepEqual(await tx.listResources('s1'), [])
    assert.equal(await tx.getIdempotency('request'), undefined)
  })
  await assert.rejects(store.transaction('other', tx => tx.putSandbox(sb('owner'), null)), /workspace mismatch/)
  await assert.rejects(store.transaction('other', tx => tx.putOperation(op())), {code: '23503'})
  await assert.rejects(store.transaction('other', tx => tx.putResource({resource_id: 'v', sandbox_id: 's1', kind: 'runtime', generation: 1, quantity: sb('other').resources})), {code: '23503'})
  await assert.rejects(store.transaction('owner', async tx => {
    await tx.putSandbox(sb('owner', 's2'), null)
    await tx.putIdempotency({...idem(), sandbox_id: 's2'})
  }), {code: '23503'})
})

test('200 replay survives reconnect without storing request text or runtime credentials', async () => {
  const ws = 'noop'; const response = {sandbox: {sandbox_id: 's1', observed_state: 'Active'}}
  await store.transaction(ws, async tx => {
    await tx.putSandbox(sb(ws), null)
    await tx.putIdempotency({...idem(), status: 200, operation_id: null, response, fingerprint: 'fake-secret-request-text'})
  })
  const secondPool = new Pool({connectionString: url})
  try {
    await new PostgresStore(secondPool, schema).transaction(ws, async tx => {
      const saved = await tx.getIdempotency('request')
      assert.deepEqual(saved?.response, response)
      assert.equal(saved?.operation_id, null)
      assert.ok(!JSON.stringify(saved).includes('fake-secret-request-text'))
    })
  } finally { await secondPool.end() }
})

test('nested credential fields reject transaction; resource and operation identity cannot be rebound', async () => {
  const ws = 'identity'
  await store.transaction(ws, async tx => {await tx.putSandbox(sb(ws), null); await tx.putOperation(op())})
  await assert.rejects(store.transaction(ws, tx => tx.putOperation({...op(), target: {nested: {credential: 'fake-secret'}}})), /credential field forbidden/)
  await assert.rejects(store.transaction(ws, tx => tx.putOperation({...op(), generation: 2})), StoreConflict)
  await assert.rejects(store.transaction(ws, tx => tx.putOperation({...op(), target: {agent: 'different'}})), StoreConflict)
  await assert.rejects(store.transaction(ws, tx => tx.putIdempotency({...idem(), status: 200, operation_id: null, response: {token: 'fake-secret'}})), /credential field forbidden/)
  const resource = {resource_id: 'v', sandbox_id: 's1', kind: 'runtime' as const, generation: 1, quantity: sb(ws).resources}
  await store.transaction(ws, tx => tx.putResource(resource))
  await assert.rejects(store.transaction(ws, tx => tx.putResource({...resource, generation: 2})), StoreConflict)
})

test('caught SQL errors still abort instead of reporting commit success', async () => {
  await assert.rejects(store.transaction('sql-abort', async tx => {
    await tx.putSandbox(sb('sql-abort'), null)
    try { await tx.putOperation(op('missing-owner', 'absent')) } catch { /* caller catches FK error */ }
  }), /transaction aborted/)
  await store.transaction('sql-abort', async tx => {assert.deepEqual(await tx.listSandboxes(), [])})
})

test('invalid numeric values, identity rebind and malformed replay rows are refused', async () => {
  assert.throws(() => new PostgresStore(pool, 'unsafe;schema'), /invalid database schema/)
  await assert.rejects(store.transaction('', async () => {}), /identifier/)
  for (const version of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
    await assert.rejects(store.transaction('invalid', tx => tx.putSandbox({...sb('invalid'), version}, null)), /safe integer/)
  }
  await store.transaction('invalid', tx => tx.putSandbox(sb('invalid'), null))
  await assert.rejects(store.transaction('invalid', tx => tx.putSandbox({...sb('invalid'), version: 4}, 1)), /advance exactly once/)
  await assert.rejects(store.transaction('invalid', tx => tx.putSandbox(sb('invalid'), null)), StoreConflict)
  for (const field of ['milli_cpu', 'memory_bytes', 'volume_bytes']) {
    await assert.rejects(store.transaction('invalid', tx => tx.putSandbox({
      ...sb('invalid', 's2'), resources: {...sb('invalid').resources, [field]: -1},
    }, null)), /resource quantity/)
  }
  await assert.rejects(store.transaction('invalid', tx => tx.putIdempotency({...idem(), status: 200, operation_id: null})), {code: '23514'})
  await store.transaction('invalid', async tx => {
    await tx.putOperation(op())
    await tx.putOperation({...op(), state: 'running', updated_at: 2})
    assert.equal((await tx.getOperation('op1'))?.state, 'running')
  })
})

test('migration tampering fails closed and retained transaction handles cannot be reused', async () => {
  let retained: PostgresTransaction | undefined
  await store.transaction('expired', async tx => {retained = tx})
  await assert.rejects(retained!.listSandboxes(), /no longer active/)
  await pool.query(`UPDATE "${schema}".schema_migrations SET checksum='tampered'`)
  await assert.rejects(store.migrate(), /checksum mismatch/)
  // Keep this isolated schema tampered: cleanup drops only this run's random schema.
})
