import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readState, createTabSync } from './tab-sync.js';
import { advance, restoreClock } from './lifecycle.js';

const KEY = 'sandbox-v1';
const policy = { idleAfter: 120, suspendAfter: 300 };
const seed = () => ['a', 'b'].map(id => ({ id, name: 'box-' + id, agent: 'Claude', status: 'Active', cpu: 2, seconds: { Active: 0, Idle: 0, Suspend: 0 }, logs: [], snapshots: [], repo: '', accountedAt: 0, lastActivity: 0, stateSince: 0 }));

// A localStorage-shaped shim shared by every "tab"; a write notifies all listeners
// like the same-origin storage event. The writer's own echo is ignored by onStorage
// because its revision is already current.
function sharedStorage() {
  const map = new Map(), listeners = new Set();
  return {
    getItem: k => (map.has(k) ? map.get(k) : null),
    setItem(k, v) { map.set(k, String(v)); for (const listen of listeners) listen({ key: k, newValue: String(v) }); },
    onChange(listen) { listeners.add(listen); }
  };
}

// A "tab" mirrors src.js: load once (restoreClock), tick like the 5 s interval
// (advance + guarded save), and act like a mutating user action (refresh, mutate, guarded save).
function openTab(storage, { listen = true } = {}) {
  const loaded = readState(storage, KEY);
  let boxes = Array.isArray(loaded?.boxes) && loaded.boxes.length ? loaded.boxes : seed();
  boxes.forEach(b => restoreClock(b, 0));
  const sync = createTabSync({ storage, key: KEY, revision: loaded?.revision || 0, get: () => boxes, adopt: next => { boxes = next; } });
  if (listen) storage.onChange(event => sync.onStorage(event));
  return {
    get boxes() { return boxes; },
    tick(now) { for (const b of boxes) advance(b, now, policy); sync.write(); },
    act(mutate) { sync.refresh(); mutate(boxes); sync.write(); },
    sync
  };
}

test('two tabs ticking in turns match the single-tab result within one tick period', () => {
  const storage = sharedStorage();
  const tabs = [openTab(storage), openTab(storage)];
  for (let i = 1; i <= 60; i++) tabs[i % 2].tick(i * 5000);
  const solo = seed();
  for (const b of solo) advance(b, 300000, policy);
  for (const tab of tabs) for (const box of tab.boxes) {
    const expected = solo.find(s => s.id === box.id);
    for (const state of ['Active', 'Idle', 'Suspend']) assert.ok(Math.abs(box.seconds[state] - expected.seconds[state]) <= 5, state + ' drifts by more than one tick');
    assert.equal(box.status, expected.status);
  }
});

test('a tab that missed the broadcast does not double-count: the write guard adopts the newer state', () => {
  const storage = sharedStorage();
  const a = openTab(storage), b = openTab(storage, { listen: false });
  a.tick(5000);
  b.tick(5000); // its own 0→5 delta is discarded, not added to the stored one
  assert.equal(b.boxes[0].seconds.Active, 5);
  assert.equal(b.boxes[0].accountedAt, 5000); // local baseline resets to the stored one; the gap is not shifted out again
  b.tick(10000);
  const stored = readState(storage, KEY);
  assert.equal(stored.revision, 2);
  assert.equal(stored.boxes[0].seconds.Active, 10);
});

test('a sandbox deleted in tab A disappears from tab B and never resurrects', () => {
  const storage = sharedStorage();
  const a = openTab(storage), b = openTab(storage, { listen: false }); // B stale: worst case
  a.act(boxes => boxes.splice(boxes.findIndex(x => x.id === 'a'), 1));
  b.act(boxes => boxes.unshift({ id: 'c', name: 'box-c', agent: 'Claude', status: 'Active', cpu: 2, seconds: { Active: 0, Idle: 0, Suspend: 0 }, logs: [], snapshots: [], repo: '', accountedAt: 0, lastActivity: 0, stateSince: 0 }));
  const stored = readState(storage, KEY).boxes;
  assert.deepEqual(stored.map(x => x.id).sort(), ['b', 'c']);
  assert.ok(!b.boxes.some(x => x.id === 'a')); // B adopted the delete before applying its own action
});

test('old bare-array data loads unchanged and gains a revision on the next write', () => {
  const storage = sharedStorage();
  storage.setItem(KEY, JSON.stringify([{ id: 'legacy', name: 'old', agent: 'Codex', status: 'Idle', cpu: 4, seconds: { Active: 10, Idle: 5, Suspend: 0 }, logs: ['hi'] }]));
  const tab = openTab(storage);
  assert.equal(tab.boxes[0].seconds.Active, 10);
  tab.tick(1000);
  const stored = readState(storage, KEY);
  assert.equal(stored.revision, 1);
  assert.equal(stored.boxes[0].seconds.Idle, 6);
});

test('readState treats missing revisions as 0 and malformed data as absent', () => {
  const load = raw => readState({ getItem: () => raw }, KEY);
  assert.equal(load(null), null);
  assert.equal(load('garbage'), null);
  assert.deepEqual(load('[]'), { revision: 0, boxes: [] });
  assert.deepEqual(load(JSON.stringify({ boxes: [{ id: 'x' }] })), { revision: 0, boxes: [{ id: 'x' }] });
});

test('storage events only adopt newer revisions of the tracked key', () => {
  const storage = sharedStorage();
  const a = openTab(storage), b = openTab(storage);
  assert.equal(b.sync.onStorage({ key: 'sandbox-policy', newValue: '{}' }), false);
  assert.equal(b.sync.onStorage({ key: KEY, newValue: null }), false);
  assert.equal(b.sync.onStorage({ key: KEY, newValue: '{oops' }), false);
  a.tick(5000); // the broadcast was already adopted by B
  const adopted = b.boxes;
  assert.equal(b.sync.onStorage({ key: KEY, newValue: JSON.stringify({ revision: 1, boxes: [] }) }), false); // own or stale revision
  assert.equal(b.boxes, adopted);
  assert.equal(b.boxes[0].seconds.Active, 5);
});
