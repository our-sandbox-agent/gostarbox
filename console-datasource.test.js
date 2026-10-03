import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDemoDatasource, createApiDatasource, resolveMode, MODE_DEMO, MODE_REAL, ApiError, PollTimeoutError } from './console-datasource.js';

const box = (id = 'a') => ({ id, name: 'box-' + id, agent: 'Claude', status: 'Active', cpu: 2, seconds: { Active: 0, Idle: 0, Suspend: 0 }, logs: [], snapshots: [], repo: '', accountedAt: 0, lastActivity: 0, stateSince: 0 });

// A localStorage-shaped spy that records every key ever touched.
function spyStorage(initial = new Map()) {
  const map = new Map(initial), touched = new Set();
  return {
    getItem: k => { touched.add(k); return map.has(k) ? map.get(k) : null; },
    setItem: (k, v) => { touched.add(k); map.set(k, String(v)); },
    get touched() { return [...touched]; }
  };
}

// Mirrors how src.js drives one datasource: a memory copy behind get/adopt.
function open(storage, seedBoxes) {
  const loaded = createDemoDatasource({ storage }).read();
  let boxes = Array.isArray(loaded?.boxes) && loaded.boxes.length ? loaded.boxes : seedBoxes;
  const ds = createDemoDatasource({ storage });
  const sync = ds.createSync({ revision: loaded?.revision || 0, get: () => boxes, adopt: next => { boxes = next; } });
  return { ds, sync, get boxes() { return boxes; } };
}

test('demo datasource roundtrip: write then read equals, revision increments', () => {
  const storage = spyStorage();
  const tab = open(storage, [box('a')]);
  assert.equal(tab.ds.read(), null);
  assert.equal(tab.sync.write(), true);
  const stored = tab.ds.read();
  assert.equal(stored.revision, 1);
  assert.deepEqual(stored.boxes, [box('a')]);
  assert.equal(tab.sync.write(), true);
  assert.equal(tab.ds.read().revision, 2);
});

test('legacy bare-array data loads as revision 0 and gains a revision on write', () => {
  const storage = spyStorage(new Map([['sandbox-v1', JSON.stringify([{ id: 'legacy', seconds: { Active: 10, Idle: 0, Suspend: 0 }, logs: [] }])]]));
  const loaded = createDemoDatasource({ storage }).read();
  assert.equal(loaded.revision, 0);
  assert.equal(loaded.boxes[0].seconds.Active, 10);
  const tab = open(storage, [box()]);
  tab.sync.write();
  assert.equal(tab.ds.read().revision, 1);
});

test('two datasources on one storage stay consistent via the revision guard', () => {
  const storage = spyStorage();
  const a = open(storage, [box('a')]), b = open(storage, [box('a')]);
  a.boxes[0].seconds.Active = 5;
  a.sync.write();
  assert.equal(b.sync.refresh(), true); // B adopts A's newer stored state wholesale
  assert.equal(b.boxes[0].seconds.Active, 5);
  b.boxes[0].seconds.Active = 7;
  b.sync.write();
  const stored = b.ds.read();
  assert.equal(stored.revision, 2);
  assert.equal(stored.boxes[0].seconds.Active, 7);
});

test('mode resolution: no apiBase → demo, apiBase set → real, unknown mode throws', () => {
  assert.equal(resolveMode(), MODE_DEMO);
  assert.equal(resolveMode({}), MODE_DEMO);
  assert.equal(resolveMode({ apiBase: '' }), MODE_DEMO);
  assert.equal(resolveMode({ apiBase: 'https://api.example.com' }), MODE_REAL);
  assert.equal(resolveMode({ mode: MODE_DEMO }), MODE_DEMO);
  assert.equal(resolveMode({ mode: MODE_REAL }), MODE_REAL);
  assert.throws(() => resolveMode({ mode: 'fake' }), /unknown datasource mode/);
});

test('createApiDatasource requires explicit apiBase and token — real mode is never accidental', () => {
  assert.throws(() => createApiDatasource(), /apiBase/);
  assert.throws(() => createApiDatasource({ apiBase: 'https://cp.test' }), /token/);
});

test('policy roundtrip through the same storage object', () => {
  const storage = spyStorage();
  const ds = createDemoDatasource({ storage });
  assert.equal(ds.readPolicy(), null);
  ds.writePolicy({ idleAfter: 60, suspendAfter: 300 });
  assert.deepEqual(ds.readPolicy(), { idleAfter: 60, suspendAfter: 300 });
});

test('demo datasource only ever touches sandbox-v1 and sandbox-policy', () => {
  const storage = spyStorage();
  const tab = open(storage, [box('a'), box('b')]);
  tab.sync.write();
  tab.ds.readPolicy();
  tab.ds.writePolicy({ idleAfter: 60, suspendAfter: 300 });
  tab.ds.read();
  for (const key of storage.touched) assert.ok(key === 'sandbox-v1' || key === 'sandbox-policy', 'unexpected key touched: ' + key);
  assert.deepEqual([...storage.touched].sort(), ['sandbox-policy', 'sandbox-v1']);
});

// ---------------------------------------------------------------------------
// Real-API client: every fixture below is hand-written to match
// docs/contracts/control-plane-api.json exactly — these tests check the
// client's fidelity to the contract, not a simulated product success.

const BASE = 'https://cp.test';
const T0 = 1760000000000;
const RES = { milli_cpu: 2000, memory_bytes: 2 * 1024 ** 3, volume_bytes: 10 * 1024 ** 3 };
const CREATE_OP = (state = 'pending') => ({
  operation_id: 'op_000001', sandbox_id: 'sbx_000001', type: 'create',
  target: { agent: 'claude', resources: RES },
  state, expected_version: null, generation: 1, result: null, error: null,
  retryable: false, created_at: T0, updated_at: T0
});
const SET_STATE_OP = {
  operation_id: 'op_000002', sandbox_id: 'sbx_000001', type: 'set_state',
  target: { state: 'Suspend', suspend_mode: 'cold' },
  state: 'pending', expected_version: 2, generation: 1, result: null, error: null,
  retryable: false, created_at: T0, updated_at: T0
};
const DESTROY_OP = {
  operation_id: 'op_000003', sandbox_id: 'sbx_000001', type: 'destroy',
  target: { scope: 'workspace_and_home_volumes' },
  state: 'pending', expected_version: 3, generation: 1, result: null, error: null,
  retryable: false, created_at: T0, updated_at: T0
};
const SANDBOX_VIEW = {
  sandbox_id: 'sbx_000001', workspace_id: 'ws_default', desired_state: 'Active',
  observed_state: 'Creating', generation: 1, version: 1, pending_operation: CREATE_OP(),
  last_confirmed_at: null, volumes: { workspace: 'vol_000001', home: 'vol_000002' },
  runtime_deadline_at: null, session: null
};

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

// A fetch stand-in serving FIFO queues of hand-written contract responses.
function contractFetch(routes) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    const method = init.method ?? 'GET';
    calls.push({ url: String(url), method, headers: init.headers ?? {}, body: init.body });
    const route = routes.find(r => r.method === method && r.path === String(url) && r.responses.length);
    if (!route) return jsonResponse(404, { error: { code: 'not_found', message: 'no fixture route' } });
    const [status, body] = route.responses.shift();
    return jsonResponse(status, body);
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

const mk = routes => {
  const f = contractFetch(routes);
  return { ds: createApiDatasource({ apiBase: BASE, token: 'tok-1', fetchImpl: f }), f };
};
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

test('real client: create 202 returns the contract {sandbox_id, operation}', async () => {
  const { ds, f } = mk([{ method: 'POST', path: BASE + '/v1/sandboxes', responses: [[202, { sandbox_id: 'sbx_000001', operation: CREATE_OP() }]] }]);
  const out = await ds.create({ agent: 'claude', resources: RES });
  assert.equal(out.sandbox_id, 'sbx_000001');
  assert.equal(out.operation.type, 'create');
  assert.equal(out.operation.state, 'pending'); // 202 is acceptance, not success
  assert.deepEqual(JSON.parse(f.calls[0].body), { agent: 'claude', resources: RES });
});

test('real client: list and inspect return parsed contract views', async () => {
  const LIST = { sandboxes: [{ sandbox_id: 'sbx_000001', observed_state: 'Creating', desired_state: 'Active', generation: 1 }] };
  const { ds } = mk([
    { method: 'GET', path: BASE + '/v1/sandboxes', responses: [[200, LIST]] },
    { method: 'GET', path: BASE + '/v1/sandboxes/sbx_000001', responses: [[200, SANDBOX_VIEW]] }
  ]);
  assert.deepEqual(await ds.list(), LIST);
  assert.deepEqual(await ds.get('sbx_000001'), SANDBOX_VIEW);
});

test('real client: requestState 202 operation and 200 already-achieved sandbox', async () => {
  const { ds } = mk([
    { method: 'POST', path: BASE + '/v1/sandboxes/sbx_000001/state', responses: [[202, { operation: SET_STATE_OP }]] },
    { method: 'POST', path: BASE + '/v1/sandboxes/sbx_000001/state', responses: [[200, { sandbox: SANDBOX_VIEW }]] }
  ]);
  assert.deepEqual(await ds.requestState('sbx_000001', { state: 'Suspend', expected_version: 2 }), { operation: SET_STATE_OP });
  assert.deepEqual(await ds.requestState('sbx_000001', { state: 'Suspend', expected_version: 2 }), { sandbox: SANDBOX_VIEW });
});

test('real client: destroy 202 returns the destroy operation', async () => {
  const { ds, f } = mk([{ method: 'DELETE', path: BASE + '/v1/sandboxes/sbx_000001', responses: [[202, { operation: DESTROY_OP }]] }]);
  const out = await ds.destroy('sbx_000001', { expected_version: 3, confirm_scope: 'workspace_and_home_volumes' });
  assert.equal(out.operation.type, 'destroy');
  assert.deepEqual(JSON.parse(f.calls[0].body), { expected_version: 3, confirm_scope: 'workspace_and_home_volumes' });
});

test('real client: getOperation returns the operation resource', async () => {
  const { ds } = mk([{ method: 'GET', path: BASE + '/v1/operations/op_000001', responses: [[200, CREATE_OP('running')]] }]);
  assert.equal((await ds.getOperation('op_000001')).state, 'running');
});

test('real client: contract error bodies surface as ApiError {status, code} — no operation faked', async () => {
  const cases = [
    // [method, path, [status, body], expectedStatus, expectedCode] — fixtures copied from control-plane-api.json's error model
    ['POST', '/v1/sandboxes/sbx_000001/state', [409, { error: { code: 'credentials_required', message: 'resume needs the credential re-sent; no operation created' } }], 409, 'credentials_required'],
    ['POST', '/v1/sandboxes/sbx_000001/state', [409, { error: { code: 'version_conflict', message: 'expected_version mismatch; re-read the sandbox' } }], 409, 'version_conflict'],
    ['POST', '/v1/sandboxes', [409, { error: { code: 'body_conflict', message: 'Idempotency-Key replayed with a different body' } }], 409, 'body_conflict'],
    ['GET', '/v1/sandboxes', [401, { error: { code: 'unauthorized', message: 'missing or invalid bearer token' } }], 401, 'unauthorized'],
    ['GET', '/v1/sandboxes/sbx_999999', [404, { error: { code: 'not_found', message: 'sandbox not found' } }], 404, 'not_found'],
    ['POST', '/v1/sandboxes', [429, { error: { code: 'capacity_exceeded', message: 'admission rejected: insufficient host capacity' } }], 429, 'capacity_exceeded'],
    ['POST', '/v1/sandboxes/sbx_000001/state', [409, { error: { code: 'opposite_operation', message: 'no state change accepted while Creating' } }], 409, 'opposite_operation']
  ];
  for (const [method, path, response, status, code] of cases) {
    const { ds } = mk([{ method, path: BASE + path, responses: [response] }]);
    const call = method === 'GET' && path === '/v1/sandboxes' ? ds.list()
      : method === 'GET' ? ds.get('sbx_999999')
      : method === 'POST' && path === '/v1/sandboxes' ? ds.create({ agent: 'claude', resources: RES })
      : ds.requestState('sbx_000001', { state: 'Suspend', expected_version: 2 });
    await assert.rejects(call, err => err instanceof ApiError && err.status === status && err.code === code,
      `${method} ${path} must surface ${status}/${code}`);
  }
});

test('real client: bearer token on every request; fresh UUID Idempotency-Key per mutating call, explicit key honored', async () => {
  const { ds, f } = mk([
    { method: 'POST', path: BASE + '/v1/sandboxes', responses: [[202, { sandbox_id: 'sbx_000001', operation: CREATE_OP() }], [202, { sandbox_id: 'sbx_000001', operation: CREATE_OP() }]] },
    { method: 'GET', path: BASE + '/v1/sandboxes', responses: [[200, { sandboxes: [] }]] },
    { method: 'GET', path: BASE + '/v1/operations/op_000001', responses: [[200, CREATE_OP()]] },
    { method: 'POST', path: BASE + '/v1/sandboxes/sbx_000001/state', responses: [[202, { operation: SET_STATE_OP }]] },
    { method: 'DELETE', path: BASE + '/v1/sandboxes/sbx_000001', responses: [[202, { operation: DESTROY_OP }]] }
  ]);
  await ds.create({ agent: 'claude', resources: RES });
  await ds.list();
  await ds.getOperation('op_000001');
  await ds.requestState('sbx_000001', { state: 'Suspend', expected_version: 2 });
  await ds.destroy('sbx_000001', { expected_version: 3, confirm_scope: 'workspace_and_home_volumes' });
  await ds.create({ agent: 'claude', resources: RES }, { idempotencyKey: 'retry-same-key' }); // caller-controlled replay after e.g. unreachable

  for (const call of f.calls) assert.equal(call.headers.Authorization, 'Bearer tok-1');
  const mutating = f.calls.filter(c => c.method !== 'GET');
  assert.equal(mutating.length, 4);
  const keys = mutating.map(c => c.headers['Idempotency-Key']);
  assert.ok(keys.slice(0, 3).every(k => UUID_RE.test(k)), 'auto keys must be UUIDs: ' + keys.join(','));
  assert.equal(new Set(keys.slice(0, 3)).size, 3, 'each mutating call generates its own key');
  assert.equal(keys[3], 'retry-same-key');
  for (const call of f.calls.filter(c => c.method === 'GET')) assert.equal(call.headers['Idempotency-Key'], undefined);
});

test('real client: network failure rejects ApiError unreachable (no HTTP answer)', async () => {
  const boom = async () => { throw new TypeError('fetch failed'); };
  const ds = createApiDatasource({ apiBase: BASE, token: 'tok-1', fetchImpl: boom });
  await assert.rejects(ds.list(), err => err instanceof ApiError && err.code === 'unreachable' && err.status === null);
});

test('real client: pollOperation resolves once the operation is terminal', async () => {
  const { ds } = mk([{ method: 'GET', path: BASE + '/v1/operations/op_000001', responses: [
    [200, CREATE_OP('pending')], [200, CREATE_OP('running')],
    [200, { ...CREATE_OP('succeeded'), result: { observed_state: 'Active', generation: 1 } }]
  ] }]);
  const op = await ds.pollOperation('op_000001', { timeoutMs: 1000, intervalMs: 1 });
  assert.equal(op.state, 'succeeded');
  assert.deepEqual(op.result, { observed_state: 'Active', generation: 1 });
});

test('real client: pollOperation timeout rejects with the last operation state — timeout is not a failure verdict', async () => {
  const { ds } = mk([{ method: 'GET', path: BASE + '/v1/operations/op_000001', responses: Array.from({ length: 50 }, () => [200, CREATE_OP('running')]) }]);
  await assert.rejects(ds.pollOperation('op_000001', { timeoutMs: 20, intervalMs: 1 }), err =>
    err instanceof PollTimeoutError && err.code === 'poll_timeout' && err.operation.state === 'running' && err.operation.operation_id === 'op_000001');
});

test('real client: server-authoritative surface — no demo members, localStorage never touched', async () => {
  const previous = globalThis.localStorage;
  const spy = spyStorage();
  globalThis.localStorage = spy;
  try {
    const { ds } = mk([
      { method: 'GET', path: BASE + '/v1/sandboxes', responses: [[200, { sandboxes: [] }]] },
      { method: 'POST', path: BASE + '/v1/sandboxes', responses: [[202, { sandbox_id: 'sbx_000001', operation: CREATE_OP() }]] }
    ]);
    assert.equal(ds.mode, MODE_REAL);
    for (const member of ['read', 'createSync', 'readPolicy', 'writePolicy']) assert.equal(ds[member], undefined, 'real mode must not fake demo member ' + member);
    await ds.list();
    await ds.create({ agent: 'claude', resources: RES });
    assert.equal(spy.touched.length, 0, 'real mode must never touch storage');
  } finally {
    if (previous === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = previous;
  }
});
