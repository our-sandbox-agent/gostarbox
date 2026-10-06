// Contract tests: port of scripts/test_control_plane_contract.py (same
// behaviors, node:test + app.request() instead of the Python double's
// request()). Flow coverage is the required #76/#123 flow; guard coverage
// breaks the service at the same deliberate mutation points and asserts the
// flow-test invariant flips (CONTRIBUTING mutate-and-fail idiom).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createApp, ControlPlane, type Capacity, type ControlPlaneApp } from '../src/app.js'
import {
  MemoryAdapter,
  PostgresAdapter,
  type IdempotencyEntry,
  type OperationRecord,
  type PersistenceAdapter,
  type SandboxRecord,
  type Snapshot,
} from '../src/store.js'

const AUTH = { Authorization: 'Bearer test-token' }
const CREATE_BODY = {
  agent: 'claude',
  resources: { milli_cpu: 1000, memory_bytes: 1 << 30, volume_bytes: 10 * 2 ** 30 },
}

interface Double {
  app: ControlPlaneApp
  cp: ControlPlane
  store: PersistenceAdapter
}

function makeDouble(capacity?: Capacity): Double {
  const store = new MemoryAdapter()
  const app = createApp(store, { token: 'test-token', capacity })
  return { app, cp: app.cp, store }
}

async function raw(
  app: ControlPlaneApp,
  method: string,
  path: string,
  opts: { body?: unknown; headers?: Record<string, string> } = {},
): Promise<{ status: number; body: any }> {
  const headers = { ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...opts.headers }
  const res = await app.request(path, {
    method,
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  })
  return { status: res.status, body: await res.json() }
}

async function req(
  app: ControlPlaneApp,
  method: string,
  path: string,
  opts: { body?: unknown; key?: string; headers?: Record<string, string> } = {},
): Promise<{ status: number; body: any }> {
  return raw(app, method, path, {
    body: opts.body,
    headers: { ...AUTH, ...(opts.key ? { 'Idempotency-Key': opts.key } : {}), ...opts.headers },
  })
}

async function create(d: Double, body: Record<string, unknown> = CREATE_BODY, key = 'c1') {
  return req(d.app, 'POST', '/v1/sandboxes', { body, key })
}

async function view(d: Double, sid: string) {
  return (await req(d.app, 'GET', `/v1/sandboxes/${sid}`)).body
}

async function versionOf(d: Double, sid: string) {
  return (await view(d, sid)).version as number
}

function opCount(d: Double): number {
  return Object.keys(d.store.snapshot().operations).length
}

async function state(d: Double, sid: string, body: Record<string, unknown>, key: string) {
  return req(d.app, 'POST', `/v1/sandboxes/${sid}/state`, { body, key })
}

async function destroy(d: Double, sid: string, expectedVersion: number, key: string) {
  return req(d.app, 'DELETE', `/v1/sandboxes/${sid}`, {
    body: { expected_version: expectedVersion, confirm_scope: 'workspace_and_home_volumes' },
    key,
  })
}

async function makeActive(d: Double, key = 'c1', body: Record<string, unknown> = CREATE_BODY): Promise<string> {
  const { status, body: resp } = await create(d, body, key)
  assert.equal(status, 202)
  d.cp.confirm(resp.operation.operation_id)
  return resp.sandbox_id as string
}

async function suspendConfirmed(d: Double, sid: string, key = 's1'): Promise<string> {
  const { status, body } = await state(
    d,
    sid,
    { state: 'Suspend', expected_version: await versionOf(d, sid), suspend_mode: 'cold' },
    key,
  )
  assert.equal(status, 202)
  d.cp.confirm(body.operation.operation_id)
  return body.operation.operation_id as string
}

// ------------------------------------------------------------------- flow
test('create -> inspect -> suspend -> resume -> destroy (required flow)', async () => {
  const d = makeDouble()
  const { status, body } = await create(d, undefined, 'c1')
  assert.equal(status, 202)
  const sid = body.sandbox_id as string
  const op = body.operation
  assert.equal(op.expected_version, null) // create carries no fence
  assert.ok(op.state === 'pending' || op.state === 'running')
  let v = await view(d, sid)
  assert.equal(v.observed_state, 'Creating') // never optimistic Active
  assert.equal(v.desired_state, 'Active')
  assert.equal(v.generation, 1)
  assert.equal(v.pending_operation.operation_id, op.operation_id)
  assert.equal(v.session, null)

  d.cp.advance(op.operation_id)
  const polled = await req(d.app, 'GET', `/v1/operations/${op.operation_id}`)
  assert.equal(polled.body.state, 'running')
  d.cp.confirm(op.operation_id)
  v = await view(d, sid)
  assert.equal(v.observed_state, 'Active')
  assert.deepEqual(v.session, { id: `claude-session-${sid}`, cwd: '/workspace' })
  assert.notEqual(v.last_confirmed_at, null)
  assert.equal(v.pending_operation, null)

  let r = await state(d, sid, { state: 'Suspend', expected_version: await versionOf(d, sid), suspend_mode: 'cold' }, 's1')
  assert.equal(r.status, 202)
  const sop = r.body.operation
  v = await view(d, sid)
  assert.equal(v.observed_state, 'Suspending') // 202 != confirmed stop
  assert.equal(v.desired_state, 'Suspend')
  assert.equal(v.pending_operation.operation_id, sop.operation_id)
  d.cp.confirm(sop.operation_id)
  assert.equal((await view(d, sid)).observed_state, 'Suspend')

  // resume without a credential: 409, no operation, stays Suspend
  const opsBefore = opCount(d)
  r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid) }, 'r1')
  assert.equal(r.status, 409)
  assert.equal(r.body.error.code, 'credentials_required')
  assert.equal(opCount(d), opsBefore)
  v = await view(d, sid)
  assert.equal(v.observed_state, 'Suspend')
  assert.equal(v.pending_operation, null)
  assert.equal(v.generation, 1)

  // resume with the credential, same Idempotency-Key and body (credential
  // excluded from the body comparison)
  r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: 'sk-test' } }, 'r1')
  assert.equal(r.status, 202)
  const rop = r.body.operation
  v = await view(d, sid)
  assert.equal(v.observed_state, 'Resuming')
  assert.equal(v.generation, 2) // cold resume -> new generation
  d.cp.confirm(rop.operation_id)
  v = await view(d, sid)
  assert.equal(v.observed_state, 'Active')
  assert.equal(v.generation, 2)

  r = await destroy(d, sid, await versionOf(d, sid), 'd1')
  assert.equal(r.status, 202)
  const dop = r.body.operation
  assert.equal((await view(d, sid)).observed_state, 'Destroying')
  d.cp.confirm(dop.operation_id)
  v = await view(d, sid)
  assert.equal(v.observed_state, 'Destroyed')
  assert.equal(v.session, null)

  // repeat destroy after Destroyed: existing operation, not a second one
  r = await destroy(d, sid, await versionOf(d, sid), 'd2')
  assert.equal(r.status, 202)
  assert.equal(r.body.operation.operation_id, dop.operation_id)
  assert.equal(opCount(d), 4)
})

test('idempotency: same key + same body -> no double create', async () => {
  const d = makeDouble()
  const r1 = await create(d, undefined, 'k')
  const r2 = await create(d, undefined, 'k')
  assert.equal(r1.status, 202)
  assert.equal(r2.status, 202)
  assert.equal(r1.body.operation.operation_id, r2.body.operation.operation_id)
  assert.equal(r1.body.sandbox_id, r2.body.sandbox_id)
  const listing = await req(d.app, 'GET', '/v1/sandboxes')
  assert.equal(listing.body.sandboxes.length, 1)
  d.cp.confirm(r1.body.operation.operation_id)
  const r3 = await create(d, undefined, 'k') // replay after terminal op
  assert.equal(r3.status, 202)
  assert.equal(r3.body.operation.operation_id, r1.body.operation.operation_id)
  assert.equal(r3.body.operation.state, 'succeeded')
})

test('idempotency: same key + different body -> 409 body_conflict', async () => {
  const d = makeDouble()
  await create(d, undefined, 'k')
  const other = { ...CREATE_BODY, resources: { ...CREATE_BODY.resources, milli_cpu: 2000 } }
  const { status, body } = await create(d, other, 'k')
  assert.equal(status, 409)
  assert.equal(body.error.code, 'body_conflict')
  const listing = await req(d.app, 'GET', '/v1/sandboxes')
  assert.equal(listing.body.sandboxes.length, 1)
})

test('expected_version mismatch -> 409 version_conflict', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  const stale = (await versionOf(d, sid)) - 1
  let r = await state(d, sid, { state: 'Suspend', expected_version: stale }, 's1')
  assert.equal(r.status, 409)
  assert.equal(r.body.error.code, 'version_conflict')
  r = await destroy(d, sid, stale, 'd1')
  assert.equal(r.status, 409)
  assert.equal(r.body.error.code, 'version_conflict')
  // the correct version still works afterwards
  r = await state(d, sid, { state: 'Suspend', expected_version: await versionOf(d, sid) }, 's2')
  assert.equal(r.status, 202)
})

test('missing Idempotency-Key -> 422 invalid', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  let r = await req(d.app, 'POST', '/v1/sandboxes', { body: CREATE_BODY })
  assert.deepEqual([r.status, r.body.error.code], [422, 'invalid'])
  r = await req(d.app, 'POST', `/v1/sandboxes/${sid}/state`, {
    body: { state: 'Suspend', expected_version: 2 },
  })
  assert.deepEqual([r.status, r.body.error.code], [422, 'invalid'])
  r = await req(d.app, 'DELETE', `/v1/sandboxes/${sid}`, {
    body: { expected_version: 2, confirm_scope: 'workspace_and_home_volumes' },
  })
  assert.deepEqual([r.status, r.body.error.code], [422, 'invalid'])
})

test('401 unauthorized on every endpoint (missing and wrong token)', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  const opId = Object.keys(d.store.snapshot().operations)[0]
  const probes: [string, string, unknown][] = [
    ['POST', '/v1/sandboxes', CREATE_BODY],
    ['GET', '/v1/sandboxes', undefined],
    ['GET', `/v1/sandboxes/${sid}`, undefined],
    ['POST', `/v1/sandboxes/${sid}/state`, { state: 'Suspend', expected_version: 2 }],
    ['DELETE', `/v1/sandboxes/${sid}`, { expected_version: 2, confirm_scope: 'x' }],
    ['GET', `/v1/operations/${opId}`, undefined],
    ['POST', `/v1/sandboxes/${sid}/terminal-ticket`, {}],
    ['GET', '/v1/not-an-endpoint', undefined], // auth runs before routing, as in the double
  ]
  for (const [method, path, body] of probes) {
    for (const headers of [{}, { Authorization: 'Bearer wrong' }] as Record<string, string>[]) {
      const r = await raw(d.app, method, path, { body, headers })
      assert.equal(r.status, 401, `${method} ${path}`)
      assert.equal(r.body.error.code, 'unauthorized')
    }
  }
})

test('unknown ids and unknown endpoint -> 404 not_found', async () => {
  const d = makeDouble()
  const probes: [string, string, unknown][] = [
    ['GET', '/v1/sandboxes/sbx_999999', undefined],
    ['POST', '/v1/sandboxes/sbx_999999/state', { state: 'Active', expected_version: 1 }],
    ['DELETE', '/v1/sandboxes/sbx_999999', { expected_version: 1, confirm_scope: 'workspace_and_home_volumes' }],
    ['POST', '/v1/sandboxes/sbx_999999/terminal-ticket', {}],
    ['GET', '/v1/operations/op_999999', undefined],
    ['GET', '/v1/not-an-endpoint', undefined],
  ]
  for (const [method, path, body] of probes) {
    const r = await req(d.app, method, path, { body, key: 'k' })
    assert.equal(r.status, 404, `${method} ${path}`)
    assert.equal(r.body.error.code, 'not_found')
  }
})

test('capacity admission: create and resume -> 429 capacity_exceeded', async () => {
  const cap = { milli_cpu: 2000, memory_bytes: 2 * 2 ** 30, volume_bytes: 20 * 2 ** 30 }
  const d = makeDouble(cap)
  const small = { ...CREATE_BODY, resources: { milli_cpu: 1500, memory_bytes: 1 << 30, volume_bytes: 1 << 30 } }
  let r = await create(d, small, 'a')
  assert.equal(r.status, 202)
  r = await create(d, small, 'b')
  assert.equal(r.status, 429)
  assert.equal(r.body.error.code, 'capacity_exceeded')
  assert.equal(opCount(d), 1)
  let listing = await req(d.app, 'GET', '/v1/sandboxes')
  assert.equal(listing.body.sandboxes.length, 1)

  // resume-side admission: compute freed by a confirmed suspend is taken
  const sid = await makeActive(d, 'a', small) // key 'a' + same body -> replay of the first create
  await suspendConfirmed(d, sid)
  r = await create(d, small, 'b2')
  assert.equal(r.status, 202)
  r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: 'sk-test' } }, 'r')
  assert.equal(r.status, 429)
  assert.equal(r.body.error.code, 'capacity_exceeded')
  listing = await req(d.app, 'GET', '/v1/sandboxes')
  assert.equal(listing.body.sandboxes.length, 2)
})

test('unknown runtime outcome stays pending/Lost, never optimistic Active', async () => {
  const d = makeDouble()
  const { body } = await create(d, undefined, 'c1')
  const sid = body.sandbox_id as string
  assert.equal((await view(d, sid)).observed_state, 'Creating')
  d.cp.expireLease(sid)
  let v = await view(d, sid)
  assert.equal(v.observed_state, 'Lost') // unknown -> Lost, not Active
  let r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: 'sk-test' } }, 'r')
  assert.equal(r.status, 409) // fencing/reconciliation first
  r = await destroy(d, sid, await versionOf(d, sid), 'd')
  assert.equal(r.status, 409)
  d.cp.markReconciled(sid)
  r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: 'sk-test' } }, 'r')
  assert.equal(r.status, 202)
  assert.equal((await view(d, sid)).observed_state, 'Resuming')

  // a requested state change stays unconfirmed until Runner evidence
  const sid2 = await makeActive(d, 'c2')
  r = await state(d, sid2, { state: 'Suspend', expected_version: await versionOf(d, sid2) }, 's')
  assert.equal(r.status, 202)
  assert.equal((await view(d, sid2)).observed_state, 'Suspending')
})

test('API restart via snapshot/restore keeps state, operations, idempotency', async () => {
  const d = makeDouble()
  const { body } = await create(d, undefined, 'c1')
  const sid = body.sandbox_id as string
  d.cp.confirm(body.operation.operation_id)
  await suspendConfirmed(d, sid)
  const before = await view(d, sid)
  const op1 = Object.keys(d.store.snapshot().operations)[0]

  const blob = JSON.parse(JSON.stringify(d.store.snapshot())) as Snapshot // must be plain JSON
  const store2 = new MemoryAdapter()
  store2.restore(blob)
  const d2: Double = { app: createApp(store2, { token: 'test-token' }), cp: null as never, store: store2 }
  d2.cp = d2.app.cp
  const after = await view(d2, sid)
  assert.equal(after.observed_state, before.observed_state)
  assert.equal(after.version, before.version)
  assert.equal(after.generation, before.generation)
  const opView = await req(d2.app, 'GET', `/v1/operations/${op1}`)
  assert.equal(opView.status, 200)
  assert.equal(opView.body.state, 'succeeded')
  // idempotency survives the restart: replay does not double-create
  const replay = await create(d2, undefined, 'c1')
  assert.equal(replay.status, 202)
  assert.equal(replay.body.sandbox_id, sid)
  assert.equal(replay.body.operation.operation_id, op1)
  const listing = await req(d2.app, 'GET', '/v1/sandboxes')
  assert.equal(listing.body.sandboxes.length, 1)
})

test('credential never in operation payload, sandbox view, or snapshot', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  await suspendConfirmed(d, sid)
  const secret = 'sk-SECRET-123'
  const { status, body } = await state(
    d,
    sid,
    { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: secret } },
    'r',
  )
  assert.equal(status, 202)
  const opJson = JSON.stringify(body.operation)
  assert.ok(!opJson.includes(secret))
  assert.ok(!('credential' in body.operation))
  assert.ok(!JSON.stringify(await view(d, sid)).includes(secret))
  assert.ok(!JSON.stringify(d.store.snapshot()).includes(secret))
  d.cp.confirm(body.operation.operation_id)
  assert.ok(!JSON.stringify(d.store.snapshot()).includes(secret))
})

test('opposite operation refused; Resuming locks changes and destroy', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  let r = await state(d, sid, { state: 'Suspend', expected_version: await versionOf(d, sid) }, 's1')
  assert.equal(r.status, 202)
  const suspendOp = r.body.operation.operation_id
  r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid) }, 'x')
  assert.equal(r.status, 409)
  assert.equal(r.body.error.code, 'opposite_operation')
  // replay of the same target returns the same pending operation
  r = await state(d, sid, { state: 'Suspend', expected_version: await versionOf(d, sid) }, 's2')
  assert.equal(r.status, 202)
  assert.equal(r.body.operation.operation_id, suspendOp)
  assert.equal(opCount(d), 2)
  d.cp.confirm(suspendOp)
  r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: 'sk-test' } }, 'r')
  assert.equal(r.status, 202)
  assert.equal((await view(d, sid)).observed_state, 'Resuming')
  // while Resuming: any change or destroy is refused
  r = await state(d, sid, { state: 'Suspend', expected_version: await versionOf(d, sid) }, 'z')
  assert.equal(r.status, 409)
  r = await destroy(d, sid, await versionOf(d, sid), 'd')
  assert.equal(r.status, 409)
})

test('warm suspend mode -> 422 unsupported_suspend_mode', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  const { status, body } = await state(
    d,
    sid,
    { state: 'Suspend', expected_version: await versionOf(d, sid), suspend_mode: 'warm' },
    's1',
  )
  assert.equal(status, 422)
  assert.equal(body.error.code, 'unsupported_suspend_mode')
  assert.equal(opCount(d), 1)
  assert.equal((await view(d, sid)).observed_state, 'Active')
})

test('Error state requires reconciliation before changes', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  const { body } = await state(d, sid, { state: 'Suspend', expected_version: await versionOf(d, sid) }, 's1')
  const failed = d.cp.confirm(body.operation.operation_id, {
    code: 'recovery_retry_exhausted',
    message: 'bounded retries exhausted',
  })
  assert.equal(failed.state, 'failed')
  assert.equal(failed.error?.code, 'recovery_retry_exhausted')
  const v = await view(d, sid)
  assert.equal(v.observed_state, 'Error')
  assert.notEqual(v.last_confirmed_state, null)
  assert.equal(v.error.code, 'recovery_retry_exhausted')
  let r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: 'sk-test' } }, 'r')
  assert.equal(r.status, 409) // error-before-reconcile
  d.cp.markReconciled(sid)
  r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid), credential: { api_key: 'sk-test' } }, 'r2')
  assert.equal(r.status, 202)
  assert.equal((await view(d, sid)).observed_state, 'Resuming')
})

test('terminal ticket binds sandbox + generation with TTL; 404 unknown/destroyed', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  let r = await req(d.app, 'POST', `/v1/sandboxes/${sid}/terminal-ticket`, { body: {} })
  assert.equal(r.status, 200)
  assert.ok(r.body.ticket.startsWith('tt_'))
  assert.equal(r.body.sandbox_id, sid)
  assert.equal(r.body.generation, (await view(d, sid)).generation)
  assert.equal(r.body.expires_in_ms, 60000)
  r = await req(d.app, 'POST', '/v1/sandboxes/sbx_999999/terminal-ticket', { body: {} })
  assert.equal(r.status, 404)
  await destroy(d, sid, await versionOf(d, sid), 'd')
  const destroyOp = Object.values(d.store.snapshot().operations).find((op) => op.type === 'destroy') as OperationRecord
  d.cp.confirm(destroyOp.operation_id)
  r = await req(d.app, 'POST', `/v1/sandboxes/${sid}/terminal-ticket`, { body: {} })
  assert.equal(r.status, 404)
})

test('same target already achieved -> 200 sandbox view, no second operation', async () => {
  const d = makeDouble()
  const sid = await makeActive(d)
  const ops = opCount(d)
  let r = await state(d, sid, { state: 'Active', expected_version: await versionOf(d, sid) }, 'a')
  assert.equal(r.status, 200)
  assert.equal(r.body.sandbox.observed_state, 'Active')
  assert.equal(opCount(d), ops)
  await suspendConfirmed(d, sid)
  r = await state(d, sid, { state: 'Suspend', expected_version: await versionOf(d, sid) }, 'b')
  assert.equal(r.status, 200)
})

test('PostgresAdapter is a placeholder that refuses all use (no fake persistence)', () => {
  const pg = new PostgresAdapter()
  assert.throws(() => pg.listSandboxes(), /PostgresAdapter is a placeholder/)
  assert.throws(() => pg.snapshot(), /PostgresAdapter is a placeholder/)
})

// ----------------------------------------------------------------- guards
// Each mutant subclass breaks one safeguard at a deliberate mutation point;
// each guard proves the flow-test invariant flips on the mutant and holds on
// the real service (non-vacuity, CONTRIBUTING mutate-and-fail rule).

class OptimisticCreateMutant extends ControlPlane {
  protected applyCreateObservation(sb: SandboxRecord): void {
    sb.observed_state = 'Active' // the dishonesty the ADR forbids
  }
}

class AuthBypassMutant extends ControlPlane {
  authorized(_authorization: string | undefined): boolean {
    return true
  }
}

class BlindFingerprintMutant extends ControlPlane {
  protected fingerprint(_body: Record<string, unknown>): string {
    return 'constant' // same key + different body would replay silently
  }
}

class NoDedupMutant extends ControlPlane {
  protected idempotentLookup(_key: string): IdempotencyEntry | undefined {
    return undefined // replays double-create
  }
}

class UnfencedVersionMutant extends ControlPlane {
  protected versionOk(_sb: SandboxRecord, _expected: number): boolean {
    return true // ignores expected_version
  }
}

class LeakyCredentialMutant extends ControlPlane {
  protected storeCredential(_sb: SandboxRecord, op: OperationRecord, credential: unknown): void {
    op.target = { ...op.target, credential: credential as object }
  }
}

class UnfencedLostMutant extends ControlPlane {
  protected stateRefusal(sb: SandboxRecord, target: string) {
    if (sb.observed_state === 'Lost') return undefined // accepts changes before fencing
    return super.stateRefusal(sb, target)
  }
}

class NoCapacityAccountingMutant extends ControlPlane {
  protected reservedCompute(): { milli_cpu: number; memory_bytes: number } {
    return { milli_cpu: 0, memory_bytes: 0 }
  }
}

type CpConstructor = new (store: PersistenceAdapter, opts?: { token?: string; capacity?: Capacity }) => ControlPlane

function mutantApp(cls: CpConstructor, capacity?: Capacity): ControlPlaneApp {
  return createApp(new cls(new MemoryAdapter(), { token: 'test-token', capacity }))
}

test('guard: optimistic create breaks the honesty probe', async () => {
  const real = makeDouble()
  let r = await create(real)
  assert.equal((await view(real, r.body.sandbox_id)).observed_state, 'Creating')
  const broken = mutantApp(OptimisticCreateMutant)
  r = await req(broken, 'POST', '/v1/sandboxes', { body: CREATE_BODY, key: 'c1' })
  assert.equal(r.status, 202)
  assert.equal((await req(broken, 'GET', `/v1/sandboxes/${r.body.sandbox_id}`)).body.observed_state, 'Active')
})

test('guard: auth bypass breaks the 401 probe', async () => {
  const real = makeDouble()
  assert.equal((await raw(real.app, 'GET', '/v1/sandboxes')).status, 401)
  const broken = mutantApp(AuthBypassMutant)
  assert.notEqual((await raw(broken, 'GET', '/v1/sandboxes')).status, 401)
})

test('guard: blind fingerprint breaks the body-conflict probe', async () => {
  const other = { ...CREATE_BODY, resources: { ...CREATE_BODY.resources, milli_cpu: 2000 } }
  const real = makeDouble()
  await create(real, undefined, 'k')
  let r = await create(real, other, 'k')
  assert.equal(r.status, 409)
  const broken = mutantApp(BlindFingerprintMutant)
  await req(broken, 'POST', '/v1/sandboxes', { body: CREATE_BODY, key: 'k' })
  r = await req(broken, 'POST', '/v1/sandboxes', { body: other, key: 'k' })
  assert.notEqual(r.status, 409)
})

test('guard: no dedup breaks the single-sandbox probe', async () => {
  const real = makeDouble()
  await create(real, undefined, 'k')
  await create(real, undefined, 'k')
  assert.equal(Object.keys(real.store.snapshot().sandboxes).length, 1)
  const broken = mutantApp(NoDedupMutant)
  await req(broken, 'POST', '/v1/sandboxes', { body: CREATE_BODY, key: 'k' })
  await req(broken, 'POST', '/v1/sandboxes', { body: CREATE_BODY, key: 'k' })
  const cp = broken.cp
  assert.equal(Object.keys(cp.snapshot().sandboxes).length, 2)
})

test('guard: unfenced version breaks the 409 probe', async () => {
  const real = makeDouble()
  let sid = await makeActive(real)
  let r = await state(real, sid, { state: 'Suspend', expected_version: 999 }, 's')
  assert.equal(r.status, 409)
  const broken = mutantApp(UnfencedVersionMutant)
  const { body } = await req(broken, 'POST', '/v1/sandboxes', { body: CREATE_BODY, key: 'c1' })
  broken.cp.confirm(body.operation.operation_id)
  const sid2 = body.sandbox_id as string
  r = await req(broken, 'POST', `/v1/sandboxes/${sid2}/state`, {
    body: { state: 'Suspend', expected_version: 999 },
    key: 's',
  })
  assert.notEqual(r.status, 409)
})

test('guard: leaky credential breaks the no-secret probe', async () => {
  const secret = 'sk-LEAK-123'
  for (const [cls, present] of [
    [ControlPlane, false],
    [LeakyCredentialMutant, true],
  ] as [CpConstructor, boolean][]) {
    const app = mutantApp(cls)
    const cp = app.cp
    const { body } = await req(app, 'POST', '/v1/sandboxes', { body: CREATE_BODY, key: 'c1' })
    cp.confirm(body.operation.operation_id)
    const sid = body.sandbox_id as string
    const sv = await req(app, 'POST', `/v1/sandboxes/${sid}/state`, {
      body: { state: 'Suspend', expected_version: 2 },
      key: 's1',
    })
    cp.confirm(sv.body.operation.operation_id)
    const ver = (await req(app, 'GET', `/v1/sandboxes/${sid}`)).body.version
    const r = await req(app, 'POST', `/v1/sandboxes/${sid}/state`, {
      body: { state: 'Active', expected_version: ver, credential: { api_key: secret } },
      key: 'r',
    })
    assert.equal(JSON.stringify(r.body.operation).includes(secret), present)
  }
})

test('guard: unfenced Lost breaks the reconciliation probe', async () => {
  const real = makeDouble()
  let sid = await makeActive(real)
  real.cp.expireLease(sid)
  let r = await state(real, sid, { state: 'Active', expected_version: await versionOf(real, sid), credential: { api_key: 'sk' } }, 'r')
  assert.equal(r.status, 409)
  const broken = mutantApp(UnfencedLostMutant)
  const { body } = await req(broken, 'POST', '/v1/sandboxes', { body: CREATE_BODY, key: 'c1' })
  broken.cp.confirm(body.operation.operation_id)
  sid = body.sandbox_id
  broken.cp.expireLease(sid)
  const v = await req(broken, 'GET', `/v1/sandboxes/${sid}`)
  r = await req(broken, 'POST', `/v1/sandboxes/${sid}/state`, {
    body: { state: 'Active', expected_version: v.body.version, credential: { api_key: 'sk' } },
    key: 'r',
  })
  assert.notEqual(r.status, 409)
})

test('guard: no capacity accounting breaks the 429 probe', async () => {
  const cap = { milli_cpu: 2000, memory_bytes: 2 * 2 ** 30, volume_bytes: 20 * 2 ** 30 }
  const body = { ...CREATE_BODY, resources: { milli_cpu: 1500, memory_bytes: 1 << 30, volume_bytes: 1 << 30 } }
  const real = makeDouble(cap)
  await create(real, body, 'a')
  let r = await create(real, body, 'b')
  assert.equal(r.status, 429)
  const broken = mutantApp(NoCapacityAccountingMutant, cap)
  await req(broken, 'POST', '/v1/sandboxes', { body, key: 'a' })
  r = await req(broken, 'POST', '/v1/sandboxes', { body, key: 'b' })
  assert.notEqual(r.status, 429)
})

// #129 regressions: evidence ordering, identity and successful no-op replay.
test('resume response, replay, poll and confirmation share the new generation', async () => {
  const d = makeDouble(); const sid = await makeActive(d)
  await suspendConfirmed(d, sid)
  const body = { state: 'Active', expected_version: await versionOf(d, sid), credential: 'dummy' }
  const r = await state(d, sid, body, 'resume')
  const generation = (await view(d, sid)).generation
  assert.equal(r.body.operation.generation, generation)
  assert.equal((await state(d, sid, { ...body, credential: 'replacement' }, 'resume')).body.operation.generation, generation)
  assert.equal((d.cp.pollOperation(r.body.operation.operation_id).body as any).generation, generation)
  assert.equal(d.cp.confirm(r.body.operation.operation_id).result?.generation, generation)
})

test('destroy supersedes pending create/idle and late success or error cannot change it', async () => {
  for (const creating of [true, false]) for (const error of [undefined, { code: 'late_error' }]) {
    const d = makeDouble(); const c = await create(d); const sid = c.body.sandbox_id
    let old = c.body.operation.operation_id
    if (!creating) {
      d.cp.confirm(old)
      old = (await state(d, sid, { state: 'Idle', expected_version: await versionOf(d, sid) }, 'idle')).body.operation.operation_id
    }
    const r = await destroy(d, sid, await versionOf(d, sid), 'destroy')
    assert.equal(r.status, 202)
    const before = d.cp.snapshot()
    assert.equal(d.cp.confirm(old, error).state, 'failed')
    assert.deepEqual(d.cp.snapshot(), before)
    assert.equal(d.cp.confirm(r.body.operation.operation_id).result?.observed_state, 'Destroyed')
    assert.equal((await view(d, sid)).observed_state, 'Destroyed')
  }
})

test('confirmation rejects stale generation, wrong operation and wrong completion phase without writes', async () => {
  for (const change of ['generation', 'operation', 'phase']) {
    const d = makeDouble(); const c = await create(d); const sid = c.body.sandbox_id
    const snapshot = d.cp.snapshot()
    if (change === 'generation') snapshot.sandboxes[sid].generation++
    if (change === 'operation') snapshot.sandboxes[sid].pending_operation = null
    if (change === 'phase') snapshot.sandboxes[sid].observed_state = 'Lost'
    d.cp.restore(snapshot)
    for (const error of [undefined, {code: 'late_error'}]) {
      const before = d.cp.snapshot()
      assert.throws(() => d.cp.confirm(c.body.operation.operation_id, error))
      assert.deepEqual(d.cp.snapshot(), before)
    }
  }
})

test('200 no-op reserves its idempotency key and replays after state changes and restart', async () => {
  const d = makeDouble(); const sid = await makeActive(d)
  const body = { state: 'Active', expected_version: await versionOf(d, sid) }
  const first = await state(d, sid, body, 'noop')
  assert.equal(first.status, 200)
  assert.equal((await state(d, sid, { ...body, state: 'Suspend' }, 'noop')).body.error.code, 'body_conflict')
  await suspendConfirmed(d, sid)
  const restored = makeDouble(); restored.cp.restore(d.cp.snapshot())
  assert.deepEqual(await state(restored, sid, body, 'noop'), first)
})

test('authenticated schema refusals do not allocate operations or mutate state', async () => {
  const d = makeDouble(); const sid = await makeActive(d)
  const cases: [string, string, unknown][] = [
    ['DELETE', `/v1/sandboxes/${sid}`, { expected_version: await versionOf(d, sid), confirm_scope: 'wrong' }],
    ['POST', '/v1/sandboxes', { ...CREATE_BODY, agent: 'other' }],
    ['POST', '/v1/sandboxes', { ...CREATE_BODY, repo_url: 'http://example.test/repo' }],
    ['POST', '/v1/sandboxes', { ...CREATE_BODY, runtime_deadline_at: -1 }],
    ['POST', `/v1/sandboxes/${sid}/state`, { state: 'Idle', expected_version: '2' }],
    ['DELETE', `/v1/sandboxes/${sid}`, { expected_version: '2', confirm_scope: 'workspace_and_home_volumes' }],
    ['POST', '/v1/sandboxes', []],
    ['POST', '/v1/sandboxes', null],
  ]
  for (const resource of ['milli_cpu', 'memory_bytes', 'volume_bytes']) for (const bad of [0, -1, 1.5, '1', null]) {
    cases.push(['POST', '/v1/sandboxes', { ...CREATE_BODY, resources: { ...CREATE_BODY.resources, [resource]: bad } }])
  }
  for (const [method, path, body] of cases) {
    const before = d.cp.snapshot()
    const r = await req(d.app, method, path, {body, key: 'invalid'})
    assert.equal(r.status, 422, JSON.stringify(body)); assert.equal(r.body.error.code, 'invalid')
    assert.deepEqual(d.cp.snapshot(), before)
  }
  const r = await d.app.request('/v1/sandboxes', {method: 'POST', headers: {...AUTH, 'Idempotency-Key': 'bad-json'}, body: '{'})
  assert.equal(r.status, 422)
})

test('control plane requires an explicitly configured nonempty token', () => {
  const previous = process.env.SANDBOX_TOKEN
  delete process.env.SANDBOX_TOKEN
  try {
    assert.throws(() => createApp(new MemoryAdapter()), /SANDBOX_TOKEN/)
    assert.throws(() => createApp(new MemoryAdapter(), {token: ''}), /SANDBOX_TOKEN/)
    process.env.SANDBOX_TOKEN = 'test-env-token'
    assert.equal(createApp(new MemoryAdapter()).cp.authorized('Bearer test-env-token'), true)
  } finally {
    if (previous === undefined) delete process.env.SANDBOX_TOKEN
    else process.env.SANDBOX_TOKEN = previous
  }
})
