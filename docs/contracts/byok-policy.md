# BYOK credential policy (#21)

Status: **Rules delivered as a stdlib library + tests; runtime (real
credential proxy, streaming, envelope crypto, tmpfs injection) not
started.** Per the 2026-10-02 delivery note this slice is the **G03
sub-scope FIRST**: ephemeral injection, missing-key rejection, revocation
and backup exclusion as pure policy. The **full proxy — Claude traffic
streamed through a trusted proxy so the sandbox can never read the key —
remains the FINAL GOAL of #21 and is NOT claimed here.**

Sources of truth: issue #21 + its 2026-10-02 盤點 note,
[workspace-persistence.json](workspace-persistence.json) (invariant
`credentials_never_persisted_in_volumes`; `credentials_required` on
resume), [control-plane-api.json](control-plane-api.json)
(`security.secrets`, `security.missing_credential_on_resume`),
[sandbox-lifecycle ADR](../adr/sandbox-lifecycle.md) §5 (Claude API key
row) and §6 (缺 key 不啟動 Claude), [files-policy.md](files-policy.md)
(ignore list = the #21 secret-storage rule for uploads),
`scripts/gvisor_redact.py` + `scripts/tenant_authz.py` (redaction /
digest-prefix idioms this module reuses).

Implementation: `scripts/byok_policy.py` (stdlib only, no server, no fs,
no network); tests: `scripts/test_byok_policy.py` (37 tests incl. 7
mutate-and-fail guards, CONTRIBUTING idiom).

## Secret store (G03 core)

`SecretStore` holds credentials for the **trusted proxy context only**:

- **At rest** (`at_rest()`, the form a persisted store would take) each
  key is `{name, digest, sealed_blob, created_at, last_used_at,
  revoked_at}` — an enveloped model: sha256 digest plus a sealed-blob
  **stand-in** bound to a store-local salt. NO plaintext field name and
  NO plaintext value ever appear in the persisted dict.
- **In memory**, plaintext exists only in a private map and leaves it
  only through `use(key_id, purpose)` — the audited accessor that
  refuses unknown/revoked keys and records `last_used_at`. Revocation
  drops the plaintext immediately.
- **GET / list return metadata ONLY** (`name`, `created_at`,
  `last_used_at`, `digest_prefix`, `revoked_at`); the read API cannot
  leak a value it does not hold. `key_id` is itself the digest prefix
  (`key_` + 12 hex chars, same idiom as `tenant_authz.redact`), so
  **logs and audit lines identify keys by digest prefix only** — the
  tests assert value-absence over every view (grep-style, JSON-dumped).
- Re-sending the same value re-registers the key (the CLI resume
  re-send flow); a different value is a different `key_id` and must go
  through injection rotate.

## Missing key

`require_credential(state)` — semantics DELEGATED to the contracts, not
re-invented:

| situation | verdict | mapping |
|---|---|---|
| Suspend → resume, no key | `credentials_required` | **409**, no operation created, stays Suspend (`control-plane-api.json` `missing_credential_on_resume`) |
| create, no key | `no_key_refuse_start` | not an HTTP error: the sandbox may be created but the Claude agent process is not started (ADR §6 缺 key 不啟動) |
| any context with a key | `ok` | — |

## Ephemeral injection lifecycle

`plan_injection(sandbox_id, generation, key_id, rotate=False)` → an
injection descriptor bound to one execution instance:

- **tmpfs-only** `/run/credentials/<name>`, mode **0600**, owner the
  **runtime uid** (not the tenant user); lifetime = **instance
  lifetime**, cleared on **stop** (persistence contract: runtime tmpfs
  credentials cleared; resume re-sends the key).
- lifecycle: `pending` (planned) → `active`
  (`confirm_injection`, runtime confirms the mount) → `revoked`
  (stop / rotate / key revoke / superseded by a new generation).
- rejections: `unknown_key`, `key_revoked` (this is the "pending
  injections rejected" gate after revocation), `generation_mismatch`
  (stale **or unregistered** generation — conservative; a new generation
  from resume/create supersedes any surviving record automatically),
  `already_injected` (same key twice), `key_conflict` (a second key
  without explicit rotate).
- **rotate** replaces the injection atomically: the old record is
  revoked at the **same timestamp** the new plan carries, and at most
  one injection per sandbox is ever live.

## Revocation semantics

`revoke(key_id)` takes effect immediately:

- pending injections of the key are **rejected** (action
  `reject_pending_injection`), active ones are **marked for teardown**
  (`teardown_injection`, listed per sandbox + generation with the tmpfs
  path);
- the plaintext leaves proxy memory at once; later `use()` returns
  `None` (audited `use_denied`); new plans for the key answer
  `key_revoked`;
- **the sandbox is NEVER auto-killed** — `kill_sandbox` is `false` and
  no teardown action touches processes; teardown/kill stays a
  human/policy decision (same isolation stance as the watchdog lease
  work). A mutation guard pins this.

## Egress guard (G03-minimal, preparation for the full proxy)

`upstream_rule(url, is_redirect=False)` is a **pure URL check** for the
FIXED Anthropic upstream — allow-list, not block-list:

| input | verdict | reason |
|---|---|---|
| `https://api.anthropic.com/v1/...` (no query, default port) | allow | `allow` |
| `http://` scheme | deny | `scheme_not_https` |
| any other host (incl. suffix tricks), or a non-default port | deny | `host_not_allowed` |
| path outside `/v1/` | deny | `path_not_allowed` |
| ANY query string (incl. `?api_key=...`) | deny | `query_not_allowed` |
| userinfo in the authority | deny | `userinfo_present` |
| any redirect target, even same-host | deny | `redirect_not_followed` |
| unparseable URL / bad port | deny | `invalid_url` |

The key travels (when the proxy exists) only as a header on the allow
listed origin; a redirect can never bounce it to another host because
redirects are never followed.

## Backup exclusion

`backup_plan()` returns the exclusion list for swap-core/backup:
the secret store namespace (`secret-store/`) and `/run/credentials/`.
Neither plaintext NOR sealed blobs are backed up — the sealed form is
bound to the store-local key stand-in, and a backup carrying both would
move the key with the data. Together with the files-policy ignore list
(`.env*`, `*.pem`, `id_rsa*`, `node_modules`) this covers the upload
side of the #21 憑證不落一般儲存 rule.

## Trust boundary (`trust_report()`)

Machine-readable statement, honest per the 2026-10-02 note:

- **Delivered (G03):** the key lives in trusted proxy memory only
  (audited `use()`); at rest only in enveloped form; never in DB, logs,
  images, workspace/home volumes or backups; logs use digest prefixes;
  injection is tmpfs/0600/runtime-uid, instance-lifetime, cleared on
  stop; transport (upstream_rule) is https to the fixed upstream only.
- **Disclosed limitation:** in the limited trial the injected tmpfs key
  IS readable by in-sandbox user/agent processes (ADR §5: 受限期必須揭
  露). `key_readable_by_sandbox_processes: true` — stated, not hidden.
- **Final goal, NOT claimed:** full proxy streaming so the sandbox never
  reads the key (`final_goal_full_proxy.claimed: false`).

## Runtime TODO (blocked; NOT claimed)

- **Real proxy streaming**: the Claude固定上游代理 with streaming —
  request/response piping, timeout and retry policy, per-tenant
  accounting. This module only supplies the URL allow-list it will
  enforce.
- **Real crypto envelope**: the `sealed_blob` is a stand-in; real
  envelope encryption (KMS/KEK, key rotation of the store key) plus the
  backup/decrypt **restore drill** (issue acceptance: 備份與解密金鑰的
  還原演練) are runtime work.
- **IP reuse / rotate / revoke timing tests**: 停權与已連線撤銷時限 —
  real-network behavior of a revoked key on established connections;
  needs the real proxy.
- **Legacy snapshot scanning strategy**: 移除首版容器 key 檔及歷史設定;
  if old snapshots exist they must not be assumed clean — a scan/invalida
  tion policy is still to be designed (issue acceptance row 4).
- CLI hidden input for `put` (no echo), control-plane wiring of
  `require_credential` into the #76 state endpoint, and secret-store
  persistence behind the enveloped form.

## Explicitly not done here

- No proxy process, no HTTP server, no real tmpfs mount, no fs or
  network IO of any kind — rules and tests only.
- No schema changes — #76 stays the single API contract; the 409
  `credentials_required` mapping is quoted from it, not redefined.
- No multi-provider support (issue 本票不含: GitHub/Codex/OAuth).

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m compileall -q scripts
```

All pure-stdlib and offline.
