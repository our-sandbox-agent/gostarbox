# Files API policy (#20)

Status: **Rules delivered as a stdlib library + tests; runtime (real volume
IO, real tar streaming, control-plane endpoints) not started.** This is the
in-repo slice of issue #20 ([T24] 提供基本檔案 API 與工作區傳輸): the
pure policy/validation rules the real control plane must enforce before any
byte touches a sandbox volume. Per the 2026-10-02 delivery order this is
**package 1** — the read/download `sandbox cp` and path-authorization rules
FIRST, plus the upload guards as policy; package 2 (real single-file and
folder upload transport over the control plane) is blocked on volume/IO and
is NOT claimed here.

Sources of truth: issue #20 acceptance + the 2026-09-22 folder-upload MVP
note (ignore list) + the 2026-10-02 two-package order,
[workspace-persistence.json](workspace-persistence.json) (cold Suspend keeps
the workspace volume, drops the runtime),
[sandbox-lifecycle ADR](../adr/sandbox-lifecycle.md) §3 (Suspend allows
讀取／下載 only), [tenant-authz.md](tenant-authz.md) (the `file` endpoint
class this delegates to), [cli-surface.json](cli-surface.json) (`sandbox cp`
— download-only M1, implementation lands with this integration),
[limited-release G07](../trial/limited-release.md) (cp retrieval + folder
upload with size cap and ignore list), #21 (憑證不落一般儲存).

Implementation: `scripts/files_policy.py` (stdlib only, no server, no fs);
tests: `scripts/test_files_policy.py` (49 tests incl. 8 mutate-and-fail
guards, CONTRIBUTING idiom). The browser demo layer (`file-upload.js`) is
deliberately untouched and stays client-side only.

## First-version limits (explicit numbers)

| limit | value | constant / owner |
|---|---|---|
| folder tar upload, total uncompressed size | **512 MiB** (536,870,912 B) | `TAR_MAX_TOTAL_BYTES` |
| folder tar upload, entry count | **10,000** | `TAR_MAX_ENTRIES` |
| single-file upload (demo layer, unchanged) | 10 MiB | `file-upload.js` `UPLOAD_LIMIT_BYTES` |
| concurrent-upload reservation | per-volume cap, atomic under one lock | `UploadQuotaGate` |

The tar caps are pinned by a test (`test_first_version_constants_are_the_
documented_defaults`) so this document and the code cannot drift apart
silently. No resumable upload (不做續傳) — first version is fixed-cap,
one-shot.

## Path confinement and TOCTOU

`resolve_path(volume_root, requested, stat_hook=None)` confines every file
operation to its volume. Confinement is lexical, chroot-style:

- absolute requested paths (leading `/`) → `absolute_path`; **backslash
  counts as a separator**, so `..\..\etc` is traversal, not a filename;
- NUL bytes → `nul_byte`; empty / non-string → rejected;
- a `..` that would pop above the volume root → `escapes_volume` (`..`,
  `a/../../x`, mixed and Windows-separated forms); an interior `..` that
  stays inside (`a/../b`) passes — same semantics as a chroot walk;
- the returned path is always `volume_root` itself or below it.

Symlinks and hardlinks are rejected through an injected `stat_hook(path)`
returning a snapshot mapping (`{"is_symlink", "is_hardlink", ...}`, `None`
for nonexistent) — the runtime MUST inject an lstat-based hook; without one
the check is lexical only (tar name checks reuse exactly that part).

**TOCTOU policy: re-stat after resolution.** The component walk is statted,
the path is resolved, and every component is statted again; any snapshot
that changed between the two walks → `toctou_changed`, reject. The hook
abstraction exists so this rule is testable offline: a hook whose snapshot
changes between calls models a file swapped under us. A `stat` error →
`stat_error`, reject (conservative). Hardlinks are `nlink > 1` as reported
by the hook — both names of a hardlinked inode are rejected; conservative
by design.

## Suspend read-only (lifecycle)

`assert_mutable(sandbox_state)` gates every write/mkdir/delete/upload:

| observed state / flag | write ops | reason |
|---|---|---|
| `Active`, `Idle` | allowed | `ok` |
| `Suspend` (cold) | **denied — read/download only** | `suspend_read_only` |
| `Suspending`, `Resuming`, `Error`, `Lost`, unknown, missing | denied (conservative) | `not_mutable_state` |
| `warm_checkpoint_verified: false` (any mutable state) | denied — read-only until consistency verified | `warm_checkpoint_unverified` |

This matches the lifecycle ADR table (Suspend → 讀取／下載), the
workspace-persistence contract (cold suspend preserves volumes, not the
runtime), and the demo layer's client-side Suspend pre-check in
`file-upload.js` — same rule, two layers, server policy authoritative.
Reads and downloads are NOT gated by this function (that is the package-1
`cp` path: Suspend must still allow retrieval).

## Upload verification: hash first, never overwrite on failure

`verify_upload(actual_sha256, expected_sha256)` is a constant-time,
case-insensitive sha256 comparison; malformed digests → `invalid_digest`
before any comparison. On mismatch the ONLY compliant caller behavior is
explicit failure that leaves the existing artifact untouched
(失敗不覆寫既有成品). `commit_after_verify(store, path, payload, expected)`
models the runtime commit step — verify first, write only on a match, a
mismatch writes nothing — and its tests (plus a mutate-and-fail guard) pin
that semantics.

## Concurrent-upload reservation

`UploadQuotaGate.reserve_quota(pending_bytes)` runs check-then-commit as
ONE critical section under a lock, so N concurrent uploads can never
jointly reserve more than the volume cap (並发上傳預留容量). The threaded
test (60 barrier-synced threads vs a 200-byte cap) admits exactly the cap
and FAILS if the lock is removed — the in-suite `NoLockQuotaGate` mutant
keeps that regression permanently visible. `release(bytes)` returns bytes
when an upload finishes or fails. One gate per volume; the real runtime
backs this with usage-ledger volume accounting (#77: a transaction there,
the lock here).

## Ignore list (founder list, G07, #21)

`ignore_filter(paths, include_git=False)` returns `(kept, excluded)` so the
UI can display the exclusions (trial G07: `.env` 等被忽略且 UI 列出):

- always excluded: basenames matching `.env*`, `*.pem`, `id_rsa*` (the
  founder's exact list, case-sensitive), and any path with a
  `node_modules` segment (everything under it);
- `.git` segments excluded only when `include_git=True` (2026-09-22 note:
  「.git/」可選 — e.g. when the user wants to upload a work tree without
  history);
- everything else is kept.

**A violation of this list would upload credentials into general storage
and breach #21's 憑證不落一般儲存 rule** — that is why the filter ships in
package 1 even though the transport does not.

## Tar extraction rules

`TarPolicy.check(entry)` gates every entry BEFORE extraction; the first
rejection aborts the stream and nothing partial is kept as success:

- name: absolute → `absolute_path`; `..` escaping the extraction root →
  `escapes_volume` (same lexical walk as `resolve_path`); NUL → `nul_byte`;
- kind: `symlink` / `hardlink` / `device` / `special` (fifo, socket) entries
  are rejected outright — the archive may not create links or device nodes;
- caps: running total size ≤ `TAR_MAX_TOTAL_BYTES` (512 MiB), entry count
  ≤ `TAR_MAX_ENTRIES` (10,000); a rejected entry commits nothing (the
  counters only advance on accept).

## Scope discipline and the trust boundary

Every file operation carries **(workspace_id, sandbox_id, volume)**;
`authorize_file_op` refuses a missing key as a caller bug (`ValueError`).
The authorization itself DELEGATES to the #16 `Authorize`
(scripts/tenant_authz.py) with `endpoint_class: "file"` — same rule table,
not a fork: cross-workspace or unknown id → **404** (identical answers, no
cross-tenant probe signal), cross-user owned resource → 403, bad token →
401. A scope naming a workspace or volume other than the sandbox's
registered ones answers the same 404 as an unknown id; a sandbox registered
without a volume is not file-addressable.

**Trust note: the shared uid 1000 is NOT isolation itself** (issue
acceptance: 不要讓共用 uid 1000 被當成跨租戶隔離本身). Every sandbox runs as
the same uid; the confinement and workspace-scoping rules in this module
are the actual tenant boundary, and the runtime must keep enforcing them
below any uid-based mechanism.

## CLI / Web parity

The same module answers both surfaces: the `sandbox cp` download path
(cli-surface.json: download-only M1, implementation lands with this #20
integration, never a mocked success) and the web console's file operations.
The demo layer (`file-upload.js`) keeps its client-side planning checks
(10 MB, duplicate targets, Suspend pre-check) — user-facing fast failures —
while this policy is the server-side authority. One policy, two shells; no
second rule table for either.

## Runtime TODO (blocked; NOT claimed)

- Real fs wiring: the lstat-based `stat_hook`, openat2 / `RESOLVE_BENEATH`-
  style opening so the re-stat policy maps onto real fd-based operations,
  and volume mount conventions for `volume_root`.
- Real tar streaming through the control plane (tar 串流經控制平面到沙盒
  volume): server-side entry iteration feeding `TarPolicy.check`, byte
  accounting per entry, abort-and-cleanup on first rejection.
- HTTP endpoints on the #76 schema (`control-plane-api.json`) for
  list/usage/mkdir/upload/download/tar; #14's Go CLI `sandbox cp`
  implementation over them.
- Per-volume quota source of truth from the usage ledger (#77) behind
  `UploadQuotaGate`, reservation release on failed/GC'd uploads.
- Web UI consumption of `(kept, excluded)` for the G07 exclusion display.
- Cross-ticket integration with #12–#14, #17 operations (a pending
  operation also blocks writes), #16 enforcement wiring.

## Explicitly not done here

- No deployment, no HTTP server, no real fs or tar IO, no CLI binary —
  rules and tests only.
- No 500 MB resumable upload, no online editor, no snapshot fork (issue
  本票不含).
- No new endpoints or schema changes — #76 stays the single API contract.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m compileall -q scripts
```

All pure-stdlib and offline.
