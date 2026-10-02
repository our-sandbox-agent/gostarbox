import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDemoDatasource, createApiDatasource, resolveMode, MODE_DEMO, MODE_REAL } from './console-datasource.js';

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

test('createApiDatasource throws pending #76 — never a mocked success', () => {
  assert.throws(() => createApiDatasource(), /#76/);
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
