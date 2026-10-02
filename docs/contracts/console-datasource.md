# Console datasource contract

Status: **Interface delivered (demo mode only); real integration NOT delivered here.**
The real-API datasource is blocked on #76 (control-plane API) and #77 (minimal event
dependency); its factory `createApiDatasource` throws with a message naming #76 and
must never be replaced by a mock that succeeds (issue #14 2026-10-02 note: API gaps
answer explicit unsupported, never a mocked success). Source of truth:
`console-datasource.js`, tested by `console-datasource.test.js` (node --test).
Owner: issue #15 (first acceptance item's preparation).

## Scope

The datasource is the only layer through which the console reads and writes sandbox
state and auto-tiering policy. `src.js` holds no direct `localStorage` state access.
Demo mode preserves today's behavior exactly — same keys (`sandbox-v1`,
`sandbox-policy`), same `{revision, boxes}` wrapper format, same tab-sync revision
guard — so there is **zero data migration**. This slice does not touch the terminal
mount (xterm/WebSocket survival across search/tab-switch/redraw is separate work
under #15, blocked on #13/#77 runtime).

## Interface (demo implements)

| Member | Behavior |
|---|---|
| `mode` | Always `'demo'` (`MODE_DEMO`). |
| `read()` | `readState(storage, 'sandbox-v1')`: `{revision, boxes}` or `null`; legacy bare-array records load as revision 0. |
| `createSync({revision, get, adopt})` | `createTabSync` bound to `sandbox-v1`: revision-guarded `write()`, `refresh()`, `onStorage(event)` with wholesale adoption of newer stored states. |
| `readPolicy()` | Parsed `sandbox-policy` JSON, or `null` when absent/malformed (caller normalizes). |
| `writePolicy(policy)` | JSON-serializes to `sandbox-policy`; quota errors are swallowed (change lives until page close). |

Mode selection: `resolveMode({apiBase, mode})` → `MODE_DEMO` (default, no API
configured) or `MODE_REAL` (`apiBase` non-empty); an explicit `mode` must be one of
the two known values, anything else throws.

## Real-mode obligations when #76 lands

- **Authoritative server state**: the server is the source of truth for sandbox
  state and usage. The localStorage revision guard and any client-side timing
  inference (restore-clock heuristics) must not be reused for multi-client state —
  after #76, multi-client consistency means the server's answer, not a localStorage
  race (issue #15 2026-10-02 note re #55).
- **Banner separation**: demo and real modes must be clearly distinguished in the
  UI (visible mode banner/copy); a real-mode session must never display the
  "本機模擬 / browser-local demo" framing, and vice versa.
- **Cold-resume copy**: real-mode suspend/resume and reconnect notices follow the
  persistence contract semantics (workspace preserved, new terminal generation
  announced explicitly — see the terminal protocol's `wrong_generation` / NEW
  session notice rule), not the demo "關閉頁面後計時暫停" copy.
- **No partial fallback**: once `resolveMode` returns `'real'`, the console must
  not silently fall back to localStorage for state it failed to fetch; failures
  surface as errors.

## What it does NOT cover (non-goals here)

- No real API calls, no WebSocket, no xterm mount refactor — those belong to the
  remaining #15 acceptance items once #76/#77 land.
- No UI redesign, no public signup or real billing (#15 本票不含).
