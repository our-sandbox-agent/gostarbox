// Cross-tab consistency for one localStorage key: same-origin storage-event broadcast,
// a monotonic revision guard on writes, and wholesale adoption of newer stored states.
// Old records (a bare array, or a wrapper without revision) load as revision 0.
const parse = raw => {
  let data;
  try { data = JSON.parse(raw); } catch { return null; }
  if (Array.isArray(data)) return { revision: 0, boxes: data };
  if (data && Array.isArray(data.boxes)) return { revision: Number.isFinite(data.revision) ? data.revision : 0, boxes: data.boxes };
  return null;
};

export function readState(storage, key) {
  const raw = storage.getItem(key);
  return raw == null ? null : parse(raw);
}

// get() returns the current memory copy; adopt(boxes) replaces it wholesale
// (the adopter keeps the stored clock baselines so elapsed time is never counted twice).
export function createTabSync({ storage, key, revision = 0, get, adopt }) {
  let rev = revision;
  const stored = () => readState(storage, key);
  const take = state => { rev = state.revision; adopt(state.boxes); return true; };
  const refresh = () => { const s = stored(); return s && s.revision > rev ? take(s) : false; };
  const write = () => {
    // ponytail: no CAS — two tabs passing refresh() at the same revision in the
    // sub-ms before either setItem both write rev+1; add a tab-id tiebreak if
    // that race ever matters beyond this demo.
    if (refresh()) return false;
    storage.setItem(key, JSON.stringify({ revision: rev + 1, boxes: get() }));
    rev += 1;
    return true;
  };
  const onStorage = event => {
    if (event.key !== key || event.newValue == null) return false;
    const s = parse(event.newValue);
    return s && s.revision > rev ? take(s) : false;
  };
  return { refresh, write, onStorage };
}
