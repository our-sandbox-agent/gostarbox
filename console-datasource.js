// Data source layer between the console UI and wherever sandbox state lives.
// Demo mode keeps today's localStorage behavior exactly — same keys, same
// wrapper format, tab-sync revision guard — so there is zero data migration.
// Real mode is the async control-plane client of docs/contracts/
// control-plane-api.json (served by server/, issue #123 = #76 package 1).
import { readState, createTabSync } from './tab-sync.js';

export const MODE_DEMO = 'demo';
export const MODE_REAL = 'real';
const STATE_KEY = 'sandbox-v1';
const POLICY_KEY = 'sandbox-policy';

// No apiBase configured → demo. apiBase non-empty → real. An explicit mode,
// when given, must be one of the two known values; anything else throws.
export function resolveMode({ apiBase = '', mode } = {}) {
  if (mode !== undefined) {
    if (mode !== MODE_DEMO && mode !== MODE_REAL) throw new Error('unknown datasource mode: ' + String(mode));
    return mode;
  }
  return apiBase ? MODE_REAL : MODE_DEMO;
}

export function createDemoDatasource({ storage }) {
  return {
    mode: MODE_DEMO,
    read: () => readState(storage, STATE_KEY),
    createSync: ({ revision = 0, get, adopt }) => createTabSync({ storage, key: STATE_KEY, revision, get, adopt }),
    readPolicy: () => { try { const v = JSON.parse(storage.getItem(POLICY_KEY)); return v || null; } catch { return null; } },
    writePolicy: policy => { try { storage.setItem(POLICY_KEY, JSON.stringify(policy)); } catch {} }
  };
}

// Real mode is server-authoritative (issue #15 2026-10-02 note: the
// localStorage timing/revision machinery must not be reused), so this is an
// async client with the contract's own shape — NOT a mock of the demo
// interface. It has no read()/createSync()/readPolicy()/writePolicy(): no
// localStorage writes, no tab sync, no local timing. src.js stays demo-only
// until the #15 runtime wiring consumes this surface.
export class ApiError extends Error {
  constructor(status, code, message, options) {
    super(message, options);
    this.name = 'ApiError';
    this.status = status; // null when no HTTP answer was received
    this.code = code; // contract error code, or 'unreachable'
  }
}

// pollOperation timeout: carries the last observed operation state. A timeout
// is NOT a failure verdict (contract: unknown outcome resolves via lease
// expiry/reconciliation, never an assumption).
export class PollTimeoutError extends Error {
  constructor(operation) {
    super(`operation ${operation?.operation_id} still ${operation?.state} after timeout (timeout is not a failure verdict)`);
    this.name = 'PollTimeoutError';
    this.code = 'poll_timeout';
    this.operation = operation;
  }
}

export function createApiDatasource({ apiBase, token, fetchImpl = fetch }) {
  if (!apiBase) throw new Error('createApiDatasource requires apiBase (real mode is explicit, never accidental)');
  if (!token) throw new Error('createApiDatasource requires token (bearer on every endpoint)');
  const base = String(apiBase).replace(/\/+$/, '');

  async function request(path, { method = 'GET', body, idempotencyKey, signal } = {}) {
    const headers = { Authorization: `Bearer ${token}` }; // token is sent, never logged
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (idempotencyKey !== undefined) headers['Idempotency-Key'] = idempotencyKey;
    let res;
    try {
      res = await fetchImpl(base + path, { method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (cause) {
      throw new ApiError(null, 'unreachable', 'control plane unreachable', { cause });
    }
    let parsed = null;
    try { parsed = await res.json(); } catch { /* non-JSON body: fall through */ }
    if (!res.ok) {
      throw new ApiError(res.status, parsed?.error?.code ?? 'invalid_response', parsed?.error?.message ?? `HTTP ${res.status}`);
    }
    return parsed;
  }

  function mutating(path, method, body, opts) {
    return request(path, { method, body, idempotencyKey: opts?.idempotencyKey ?? crypto.randomUUID() });
  }

  async function pollOperation(id, { timeoutMs = 30000, intervalMs = 500 } = {}) {
    if (!Number.isFinite(timeoutMs) || timeoutMs < 0 || timeoutMs > 2147483647 ||
        !Number.isFinite(intervalMs) || intervalMs < 0) throw new RangeError('invalid polling timing');
    const controller = new AbortController();
    let lastOperation = null;
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(new PollTimeoutError(lastOperation));
        controller.abort();
      }, timeoutMs);
    });
    try {
      for (;;) {
        lastOperation = await Promise.race([
          request('/v1/operations/' + encodeURIComponent(id), {signal: controller.signal}), timeout,
        ]);
        if (lastOperation.state === 'succeeded' || lastOperation.state === 'failed') return lastOperation;
        let pause;
        try {
          await Promise.race([new Promise(resolve => { pause = setTimeout(resolve, intervalMs); }), timeout]);
        } finally { clearTimeout(pause); }
      }
    } finally { clearTimeout(timer); }
  }

  return {
    mode: MODE_REAL,
    list: () => request('/v1/sandboxes'),
    get: id => request('/v1/sandboxes/' + encodeURIComponent(id)),
    create: (body, opts) => mutating('/v1/sandboxes', 'POST', body, opts),
    requestState: (id, body, opts) => mutating('/v1/sandboxes/' + encodeURIComponent(id) + '/state', 'POST', body, opts),
    destroy: (id, body, opts) => mutating('/v1/sandboxes/' + encodeURIComponent(id), 'DELETE', body, opts),
    getOperation: id => request('/v1/operations/' + encodeURIComponent(id)),
    pollOperation
  };
}
