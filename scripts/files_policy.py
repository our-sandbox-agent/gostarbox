#!/usr/bin/env python3
"""Files-API policy rules for issue #20 (in-repo stdlib slice).

Rules-only library for the #20 files API: the path-confinement, suspend
read-only, upload-verification, quota-reservation, ignore-list and tar
extraction disciplines the real control plane must enforce before any
byte touches a sandbox volume. Real volume IO, real tar streaming and
the HTTP endpoints are runtime TODOs listed in
docs/contracts/files-policy.md; nothing here is deployed, and the browser
demo layer (file-upload.js) is untouched and stays client-side only.

Per the 2026-10-02 delivery order this slice is package 1: the
read/download `sandbox cp` and path-authorization rules FIRST, plus the
upload guards as pure policy (package 2, real upload transport, is
blocked on volume/IO).

Components:
  resolve_path(volume_root, requested, stat_hook=None) — volume-confined
      path resolution: rejects absolute paths, `..` escapes, NUL bytes,
      symlinks/hardlinks (via an injected stat hook) and TOCTOU swaps
      (re-stat after resolution; any change rejects).
  assert_mutable(sandbox_state) — lifecycle write gate: cold Suspend is
      read/download ONLY (reason suspend_read_only); write/mkdir/delete/
      upload require Active/Idle; an unverified warm checkpoint forces
      read-only via the state flag.
  verify_upload / commit_after_verify — sha256 comparison gate; a mismatch
      is an explicit failure and NEVER overwrites the existing artifact
      (失敗不覆寫既有成品).
  UploadQuotaGate.reserve_quota(pending_bytes) — atomic byte reservation
      under one lock so N concurrent uploads cannot jointly exceed the
      volume cap (並发上傳預留容量).
  ignore_filter(paths, include_git=False) — founder ignore list
      (.env*, *.pem, id_rsa*, node_modules, optional .git); returns
      (kept, excluded) so the UI can list exclusions (trial G07).
  TarPolicy.check(entry) — per-entry extraction gate: no absolute/..
      names, no symlink/hardlink/device/special entries, total-size and
      entry-count caps (first-version constants below).
  authorize_file_op — scope discipline: every operation carries
      (workspace_id, sandbox_id, volume); the authorization itself
      DELEGATES to scripts/tenant_authz.py Authorize with endpoint_class
      'file' (cross-workspace -> 404, no probe signal; not a second rule
      table).

Trust note: every sandbox runs as the same shared uid 1000 — that uid is
NOT isolation. The confinement and workspace-scoping rules in this module
are the actual boundary.

Tests: scripts/test_files_policy.py (mutate-and-fail guards included).
"""
import fnmatch
import hashlib
import hmac
import posixpath
import threading

from tenant_authz import NOT_FOUND

# ----------------------------------------------------------------- constants

# First-version folder-upload (tar) limits — issue acceptance 設定明確第一版
# 大小上限. These are the documented defaults; change them here AND in
# docs/contracts/files-policy.md together.
TAR_MAX_TOTAL_BYTES = 512 * 1024 * 1024  # 512 MiB per uploaded folder
TAR_MAX_ENTRIES = 10_000                 # entries per uploaded folder

# Sandbox observed_states in which write/mkdir/delete/upload are allowed
# (lifecycle ADR: Active and Idle both allow 讀寫檔案; Suspend only 讀取/下載).
MUTABLE_STATES = ("Active", "Idle")

# Founder ignore list (2026-09-22 note): basename globs always excluded
# during folder upload; directory segments excluded with everything under
# them. `.git` is optional (include_git=True); node_modules always.
IGNORE_BASENAME_GLOBS = (".env*", "*.pem", "id_rsa*")
IGNORE_SEGMENTS = ("node_modules",)
OPTIONAL_IGNORE_SEGMENTS = (".git",)

FILE_ENDPOINT_CLASS = "file"


# ------------------------------------------------------------------- verdicts

class Verdict(tuple):
    """(allowed, reason): the answer vocabulary for the file-op gates."""

    __slots__ = ()

    def __new__(cls, allowed, reason):
        return super().__new__(cls, (allowed, reason))

    allowed = property(lambda self: self[0])
    reason = property(lambda self: self[1])


class Resolution(tuple):
    """(ok, path, reason): the answer vocabulary of resolve_path."""

    __slots__ = ()

    def __new__(cls, ok, path, reason):
        return super().__new__(cls, (ok, path, reason))

    ok = property(lambda self: self[0])
    path = property(lambda self: self[1])
    reason = property(lambda self: self[2])


# ------------------------------------------------------------ path resolution

def _lexical_confine(segments):
    """Apply `.` and `..` to a relative segment list; (stack, None) or a
    rejection reason.

    A `..` with nothing left to pop would leave the base directory: that
    is the escapes_volume rejection. An interior `..` that stays inside
    (a/../b) passes — the same semantics as a chroot-style walk.
    """
    stack = []
    for segment in segments:
        if segment in ("", "."):
            continue
        if segment == "..":
            if not stack:
                return None, "escapes_volume"
            stack.pop()
        else:
            stack.append(segment)
    return stack, None


def _stat_tree(stat_hook, root, stack):
    """Snapshot every component from the volume root down to the target."""
    snapshots = [stat_hook(root)]
    probe = root
    for segment in stack:
        probe = posixpath.join(probe, segment)
        snapshots.append(stat_hook(probe))
    return snapshots


def resolve_path(volume_root, requested, stat_hook=None):
    """Resolve `requested` to a path confined inside `volume_root`.

    Rejections (Resolution with ok=False and the reason):
      not a str / empty           -> not_a_string / empty_path
      NUL byte                    -> nul_byte
      absolute (leading /)        -> absolute_path; backslash counts as a
                                     separator, so ..\\..\\etc is traversal
      `..` escaping the volume    -> escapes_volume (`..`, a/../../x, ...);
                                     interior a/../b stays inside and passes
      a component's stat says
        symlink / hardlink        -> symlink / hardlink
      re-stat after resolution
      differs from the first walk -> toctou_changed
      stat_hook raises OSError    -> stat_error

    `stat_hook(path)` returns a snapshot mapping (at least
    {"is_symlink": bool, "is_hardlink": bool}; extra keys such as st_ino
    make swaps visible) or None for a nonexistent path. The runtime MUST
    inject an lstat-based hook; without one this check is lexical only
    (the tar entry-name checks reuse exactly that lexical part).
    """
    if not isinstance(requested, str) or not isinstance(volume_root, str):
        return Resolution(False, None, "not_a_string")
    if "\0" in requested:
        return Resolution(False, None, "nul_byte")
    if requested == "":
        return Resolution(False, None, "empty_path")
    replaced = requested.replace("\\", "/")
    if replaced.startswith("/"):
        return Resolution(False, None, "absolute_path")
    stack, reason = _lexical_confine(replaced.split("/"))
    if reason:
        return Resolution(False, None, reason)
    root = posixpath.normpath(volume_root)
    confined = posixpath.join(root, *stack) if stack else root
    if stat_hook is None:
        return Resolution(True, confined, "ok")
    try:
        before = _stat_tree(stat_hook, root, stack)
        # resolution is the pure string work above; the re-stat IS the
        # TOCTOU guard — anything that changed between the two walks rejects
        after = _stat_tree(stat_hook, root, stack)
    except OSError:
        return Resolution(False, None, "stat_error")
    for snapshot in before + after:
        if snapshot is None:
            continue
        if snapshot.get("is_symlink"):
            return Resolution(False, None, "symlink")
        if snapshot.get("is_hardlink"):
            return Resolution(False, None, "hardlink")
    if before != after:
        return Resolution(False, None, "toctou_changed")
    return Resolution(True, confined, "ok")


# ----------------------------------------------------------- suspend read-only

def assert_mutable(sandbox_state):
    """Write/mkdir/delete/upload gate per the lifecycle ADR.

    cold Suspend keeps the volume but allows ONLY read/download
    (suspend_read_only); writes require Active/Idle; transitional and
    unknown states are conservatively read-only too (不確定時保守拒絕);
    a warm checkpoint that has not passed consistency verification pins
    the sandbox read-only via the state flag (warm_checkpoint_unverified).
    Returns Verdict(allowed, reason).
    """
    if not isinstance(sandbox_state, dict):
        return Verdict(False, "not_mutable_state")
    state = sandbox_state.get("state")
    if state == "Suspend":
        return Verdict(False, "suspend_read_only")
    if state not in MUTABLE_STATES:
        return Verdict(False, "not_mutable_state")
    if sandbox_state.get("warm_checkpoint_verified") is False:
        return Verdict(False, "warm_checkpoint_unverified")
    return Verdict(True, "ok")


# ----------------------------------------------------------- upload hash gate

def _is_sha256_hex(digest):
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in digest)


def verify_upload(actual_sha256, expected_sha256):
    """Hash gate for a completed upload; constant-time, case-insensitive.

    On mismatch the ONLY compliant caller behavior is an explicit failure
    that does not touch the existing artifact (commit_after_verify models
    that; 失敗不覆寫既有成品). Invalid digest shapes are rejected before
    any comparison.
    """
    if (not _is_sha256_hex(actual_sha256)
            or not _is_sha256_hex(expected_sha256)):
        return Verdict(False, "invalid_digest")
    if not hmac.compare_digest(actual_sha256.lower(),
                               expected_sha256.lower()):
        return Verdict(False, "sha256_mismatch")
    return Verdict(True, "ok")


def commit_after_verify(store, path, payload, expected_sha256):
    """Runtime commit-step model: verify first, write only on a match.

    `store` is a {path: bytes} stand-in for the volume artifact store.
    Returns (verdict, bytes_now_at_path): a mismatch leaves any existing
    bytes untouched and writes nothing at all.
    """
    verdict = verify_upload(hashlib.sha256(payload).hexdigest(),
                            expected_sha256)
    if verdict.allowed:
        store[path] = payload
    return verdict, store.get(path)


# --------------------------------------------------------- quota reservation

class UploadQuotaGate:
    """Concurrent-upload byte reservation under one lock (volume cap).

    reserve_quota(pending_bytes) is the atomic check-then-commit the issue
    demands: N concurrent uploads can never jointly reserve more than the
    volume cap. One gate per volume; the real runtime backs this with the
    usage-ledger volume accounting (a transaction there, the lock here).
    The `stall` hook runs inside the critical section purely to widen the
    check->commit window so tests can observe concurrency; it is not a
    feature (QuotaGate idiom from tenant_authz).
    """

    def __init__(self, limit, stall=None):
        self.limit = limit
        self._lock = threading.Lock()
        self._stall = stall or (lambda: None)
        self._reserved = 0

    def reserve_quota(self, pending_bytes):
        """Atomically reserve pending_bytes iff the cap still holds."""
        if not isinstance(pending_bytes, int) or isinstance(pending_bytes, bool) \
                or pending_bytes < 0:
            raise ValueError("pending_bytes must be a non-negative int")
        with self._lock:
            self._stall()
            if self._reserved + pending_bytes > self.limit:
                return False
            self._reserved += pending_bytes
            return True

    def release(self, released_bytes):
        """Return reserved bytes to the pool (upload finished or failed)."""
        with self._lock:
            self._stall()
            self._reserved = max(0, self._reserved - released_bytes)

    def reserved(self):
        return self._reserved


# -------------------------------------------------------------- ignore list

def ignore_filter(paths, include_git=False):
    """Split upload paths into (kept, excluded) per the founder ignore list.

    Always excluded: basenames matching .env* / *.pem / id_rsa*, and any
    path containing a node_modules segment. `.git` segments are excluded
    only when include_git=True (2026-09-22 note: 「.git/」可選). The
    excluded list is returned so the UI can show it (trial G07); letting
    these through would put credentials into general storage and breach
    the #21 secret-storage rule (憑證不落一般儲存).
    """
    dir_segments = (IGNORE_SEGMENTS + OPTIONAL_IGNORE_SEGMENTS
                    if include_git else IGNORE_SEGMENTS)
    kept, excluded = [], []
    for path in paths:
        parts = [part for part in str(path).split("/") if part]
        name = parts[-1] if parts else ""
        if (any(part in dir_segments for part in parts)
                or any(fnmatch.fnmatchcase(name, pattern)
                       for pattern in IGNORE_BASENAME_GLOBS)):
            excluded.append(path)
        else:
            kept.append(path)
    return kept, excluded


# ----------------------------------------------------------------- tar policy

class TarPolicy:
    """Per-entry extraction gate for folder upload (tar stream).

    Every entry is checked BEFORE extraction; the first rejection aborts
    the stream (the runtime stops and reports, nothing partial is kept):
      name: not a str / empty -> invalid_entry; NUL -> nul_byte; absolute
            (leading /, backslash counted as separator) -> absolute_path;
            `..` escaping the extraction root -> escapes_volume (interior
            a/../b passes, same walk as resolve_path)
      kind: symlink / hardlink / device / special entries rejected
      caps: running total size and entry count must stay within
            max_total_bytes / max_entries (first-version constants
            TAR_MAX_TOTAL_BYTES = 512 MiB, TAR_MAX_ENTRIES = 10,000).

    Entry shape: {"name": str, "size": int, "kind": "file"|"dir"|"symlink"|
    "hardlink"|"device"|"special"}; kind defaults to "file".
    A rejected entry commits nothing: the counters only advance on accept.
    """

    REJECT_KINDS = {"symlink": "symlink_entry",
                    "hardlink": "hardlink_entry",
                    "device": "device_entry",
                    "special": "special_entry"}
    OK_KINDS = ("file", "dir")

    def __init__(self, max_total_bytes=TAR_MAX_TOTAL_BYTES,
                 max_entries=TAR_MAX_ENTRIES):
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self.total_bytes = 0
        self.entry_count = 0

    def check(self, entry):
        if not isinstance(entry, dict):
            return Verdict(False, "invalid_entry")
        name = entry.get("name")
        size = entry.get("size", 0)
        kind = entry.get("kind", "file")
        if not isinstance(name, str) or not name:
            return Verdict(False, "invalid_entry")
        if "\0" in name:
            return Verdict(False, "nul_byte")
        replaced = name.replace("\\", "/")
        if replaced.startswith("/"):
            return Verdict(False, "absolute_path")
        stack, reason = _lexical_confine(replaced.split("/"))
        if reason:
            return Verdict(False, reason)
        if kind in self.REJECT_KINDS:
            return Verdict(False, self.REJECT_KINDS[kind])
        if kind not in self.OK_KINDS:
            return Verdict(False, "invalid_entry")
        if (not isinstance(size, int) or isinstance(size, bool)
                or size < 0):
            return Verdict(False, "invalid_entry")
        if self.entry_count + 1 > self.max_entries:
            return Verdict(False, "entry_count_cap")
        if self.total_bytes + size > self.max_total_bytes:
            return Verdict(False, "total_size_cap")
        self.entry_count += 1
        self.total_bytes += size
        return Verdict(True, "ok")


# -------------------------------------------------------------- file-op scope

def register_sandbox_volume(authz, sandbox_id, workspace_id, volume,
                            owner=None):
    """Register a sandbox — with its owning workspace AND volume — in the
    tenant resource table so file operations can be scoped to the triple."""
    authz.register_resource(sandbox_id, workspace_id, owner=owner)
    authz.resources[sandbox_id]["volume"] = volume


def authorize_file_op(authz, token_hash, workspace_of_token, user_of_token,
                      scope):
    """Workspace/sandbox/volume scope check for ONE file operation.

    `scope` must carry workspace_id, sandbox_id and volume — every files
    operation is scoped to all three (#20: 所有操作帶 workspace/sandbox
    範圍). The authorization itself DELEGATES to the #16 Authorize with
    endpoint_class 'file' (same rule table: cross-workspace or unknown id
    -> 404 with no probe signal; cross-user owned resource -> 403; bad
    token -> 401 — this function adds no second rule table). A scope
    naming a workspace or volume other than the sandbox's registered ones
    answers the same 404 as an unknown id. A missing scope key is a caller
    bug (ValueError), never an HTTP answer.
    """
    for key in ("workspace_id", "sandbox_id", "volume"):
        if not scope or not scope.get(key):
            raise ValueError(f"file operation scope missing {key}")
    resource = authz.resources.get(scope["sandbox_id"])
    if resource is not None:
        if resource["workspace"] != scope["workspace_id"]:
            return NOT_FOUND
        if resource.get("volume") != scope["volume"]:
            return NOT_FOUND
    request = {"endpoint_class": FILE_ENDPOINT_CLASS,
               "resource_id": scope["sandbox_id"]}
    return authz(token_hash, workspace_of_token, user_of_token, request)
