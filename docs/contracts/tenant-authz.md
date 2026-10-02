# Tenant authorization rules (in-repo slice)

Status: **Rules delivered as a stdlib library + tests; runtime (OAuth, cookies,
Postgres, deployment) not started.** This is the in-repo slice of issue #16
([T20] 登入、workspace 與完整租戶授權): the authorization / quota / terminal-ticket
**rules** the real control plane must enforce, in the repo's
fixture+verifier idiom. 沿用 #76 schema，不另起 API —
`control-plane-api.json` stays the single API contract; this document adds no
endpoints. Nothing here is deployed, running, or integrated.

Sources of truth: issue #16 acceptance criteria + the 2026-10-02 盤點 note
(invite list/login and tokens first; #79 G02 — GitHub OAuth login alone is not
authorization), [control-plane-api.json](control-plane-api.json) (the #76
schema these rules plug into; its `security` note hands multi-tenant
authorization to #16), [terminal-protocol.json](terminal-protocol.json) (the
`hello` frame the terminal ticket is presented at),
[sandbox-lifecycle ADR](../adr/sandbox-lifecycle.md) §3 (workspace-scoped
queries; unscoped resources answer 404 to avoid cross-tenant probing).

Implementation: `scripts/tenant_authz.py` (stdlib only, no server, no DB);
tests: `scripts/test_tenant_authz.py` (37 tests incl. 8 mutate-and-fail
guards, CONTRIBUTING idiom).

## Authorization rule table

`Authorize(token_hash, workspace_of_token, request)` decides for the seven
endpoint classes. `workspace_of_token` comes from the token registry lookup —
a revoked or unknown token resolves to no workspace. The rule is uniform
across classes; a class row exists because the issue enumerates them:

| endpoint class | same workspace | other workspace / unknown id | cross-user, same workspace |
|---|---|---|---|
| api            | 200 allow | 404 `not_found` | 403 `forbidden` (user-scoped resource) |
| exec           | 200 allow | 404 `not_found` | 403 `forbidden` |
| terminal_ticket| 200 allow | 404 `not_found` | 403 `forbidden` |
| file           | 200 allow | 404 `not_found` | 403 `forbidden` |
| snapshot       | 200 allow | 404 `not_found` | 403 `forbidden` |
| secrets        | 200 allow | 404 `not_found` | 403 `forbidden` |
| operation      | 200 allow | 404 `not_found` | 403 `forbidden` |

Token states: missing, unknown or revoked token → 401 `unauthorized` for every
class (revocation is enforced where `workspace_of_token` is resolved: a
revoked token looks up to nothing). Unknown `endpoint_class` is a caller bug
(`ValueError`), never an HTTP answer.

404 vs 403 rationale (the issue's A/B 互訪回 404/403):

- **404** for anything outside the caller's workspace, deliberately identical
  to the answer for a forged id — no cross-tenant probe signal. This matches
  `control-plane-api.json` (`unknown ids answer 404`) and
  `terminal-protocol.json` (`reconnect_after_destroy`: destroyed, unknown and
  cross-tenant ids all answer the same).
- **403** for cross-user actions on user-scoped resources *inside one
  workspace* (shared endpoints): existence is already visible to the tenant,
  so the mask would protect nothing — the action is simply denied. The acting
  principal is resolved **only from the verified token record**
  (`user_of_token`); a `user` field inside the request body is never trusted,
  and a token with no resolved user is denied by default on owned resources.
- A workspace-scoped (ownerless) resource in one's own workspace stays
  allowed for every workspace member.

## Token discipline

- Tokens are issued opaque (`secrets.token_urlsafe`), stored **hashed only**
  (sha256 hex); the clear form exists once, in the issuance response.
  `verify_token` uses `hmac.compare_digest` — no plaintext ever sits in the
  store, so store dumps cannot leak credentials.
- Revocation list keyed by hash; `is_revoked` / `revoke`; a revoked token
  authenticates as nothing (401), same answer as unknown — no distinguishable
  probe. Hashing authenticates; revocation authorizes.

## Terminal ticket discipline

`TerminalTicketPolicy(clock, ttl_s=60.0)` — clock is injected so tests control
time:

- A ticket binds **(user, workspace, sandbox_id, generation)** at issue time;
  `redeem` re-checks the full tuple. A generation bump (cold resume) or any
  rebind attempt → `binding_mismatch`, rejected.
- **Single-use**: a successful redeem sets `consumed`; presenting the same id
  again → `replay`, rejected. Reconnect means minting a NEW ticket.
- **Short TTL**: default 60 s, configurable per policy; past TTL → `expired`.
  This is the credential presented in the terminal protocol `hello` frame's
  `token` field.
- **Never logged in clear**: the store is keyed by sha256(ticket_id); the only
  loggable form is `redact(ticket_id)` → `tt_` + 12 hex of the hash.

## Quota atomicity requirement

Issue acceptance: 工作區配額與建立限流以交易或鎖防並發超額，**不能只有先
count 再 insert**.

- `QuotaGate.try_admit(workspace, count, quota)` runs check-then-commit as
  ONE critical section (a lock here; a transaction or unique constraint in
  the real Postgres — see runtime TODOs). Naive count-then-insert reads the
  count, then inserts, unlocked — concurrent readers all pass the check and
  over-admit.
- `--race` demonstrates it: 50 barrier-synchronized threads against quota 25
  admit 50/25 the naive way and exactly 25 through the gate
  (`python3 scripts/tenant_authz.py --race`).
- The threaded test fails if the lock is removed (verified against a
  lock-stripped mutant; the in-suite `NoLockQuotaGate` guard makes that
  regression visible permanently). The `stall` hook is a test seam that
  widens the check→commit window; it is not a feature.
- `try_create(workspace, now, limit, window_s)` is the create rate-limit:
  at most `limit` creates per sliding window per workspace, under the same
  lock.

## Invite allowlist (邀請名單)

`InviteList`: `invite` / `revoke` / `is_invited` — the login gate. Only
invited accounts can log in; there is **no open registration** (本票不含公開
自由註冊, no team sharing, no SSO). Absence from the list means "no login",
not a pending signup.

## What the real #16 adds (runtime TODO — not claims of this slice)

- OAuth login flow: state/callback binding, session issuance on top of
  `TokenRegistry`; #79 G02 must hold — a GitHub OAuth identity alone never
  authorizes workspace access without the invite check.
- Session CSRF token + Origin checking on the web surface; `Secure`,
  `HttpOnly`, `SameSite` cookies.
- Postgres migration on the #76 schema (hashed-token column, revocation
  table, ticket table keyed by hash, quota counters with a transaction or
  unique constraint replacing `QuotaGate`'s lock) and same-origin deployment
  of API/console/terminal behind one `console.<domain>`.
- First-version CLI token issuance (issue: 第一版即可發 CLI token，不再推出
  另一個假 runner 產品階段).
- Enforcement wiring inside the #76 Hono control plane (extend, don't fork
  `scripts/control_plane_double.py` when the double grows multi-workspace).

## Explicitly not done here

- No deployment, no OAuth provider integration, no cookies, no Postgres, no
  HTTP server — rules and tests only.
- No team sharing, SSO, or public registration (issue 本票不含).
- The #76 test double stays single-tenant M1; multi-workspace doubling is
  future work on the same schema.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m compileall -q scripts
python3 scripts/tenant_authz.py --race
```

All pure-stdlib and offline.
