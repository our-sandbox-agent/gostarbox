// Data source layer between the console UI and wherever sandbox state lives.
// Demo mode keeps today's localStorage behavior exactly — same keys, same
// wrapper format, tab-sync revision guard — so there is zero data migration.
// Real mode lands with #76 (control-plane contract) and is deliberately NOT
// implemented here: its factory throws instead of ever mocking a successful
// API (issue #14 2026-10-02 note).
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

export function createApiDatasource() {
  throw new Error('real API datasource pending #76 control-plane contract implementation');
}
