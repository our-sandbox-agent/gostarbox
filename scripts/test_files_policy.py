"""Tests for the #20 files API policy rules (scripts/files_policy.py).

Coverage per the issue acceptance (in-repo rules slice; real volume IO
blocked, so all filesystem facts enter through injected hooks):
  - traversal battery: .., absolute, encoded/backslash, mixed, interior
    dotdot that stays inside, symlink/hardlink via the injected stat hook,
    TOCTOU (the stat snapshot changing between the two walks -> reject)
  - suspend matrix: Suspend read-only (suspend_read_only), Active/Idle
    writable, transitional/unknown states conservative, warm-checkpoint-
    unverified read-only via the state flag
  - hash verify: mismatch is an explicit failure and NEVER overwrites the
    existing artifact; invalid digests rejected before comparison
  - quota reservation: atomic under a threaded race — the race test FAILS
    if the lock is removed (the in-suite NoLockQuotaGate mutant makes that
    regression permanently visible)
  - ignore list: the exact founder list, returning (kept, excluded) so the
    UI can display exclusions (trial G07)
  - tar battery: path traversal, symlink/hardlink/device/special entries,
    total-size and entry-count caps, no partial commit on rejection
  - scope discipline: cross-workspace 404 via the delegated tenant rule,
    volume/workspace-scope mismatch 404, missing scope ValueError
  - mutation guards (CONTRIBUTING mutate-and-fail idiom): each guard
    breaks one safeguard at a deliberate point and asserts the invariant
    flips on the mutant and holds on the real rules.
"""
import hashlib
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import files_policy  # noqa: E402
from files_policy import (MUTABLE_STATES, TAR_MAX_ENTRIES,  # noqa: E402
                          TAR_MAX_TOTAL_BYTES, TarPolicy, UploadQuotaGate,
                          Verdict, assert_mutable, authorize_file_op,
                          commit_after_verify, ignore_filter,
                          register_sandbox_volume, resolve_path,
                          verify_upload)
from tenant_authz import (ALLOW, Authorize, Decision, FORBIDDEN,  # noqa: E402
                          NOT_FOUND, TokenRegistry, UNAUTHORIZED,
                          run_concurrently)

ROOT = "/vol/ws-a"


def snap(ino=1, is_symlink=False, is_hardlink=False):
    """A stat-hook snapshot: nlink>1 is modeled as is_hardlink."""
    return {"st_ino": ino, "is_symlink": is_symlink,
            "is_hardlink": is_hardlink}


def const_hook(mapping):
    return lambda path: mapping.get(path)


class ChangingStat:
    """Stat hook whose final-path snapshot changes on every call: the file
    is being swapped under us while resolution runs (TOCTOU)."""

    def __init__(self, final):
        self.final = final
        self.calls = 0

    def __call__(self, path):
        if path != self.final:
            return snap(ino=7)
        self.calls += 1
        return snap(ino=100 + self.calls)


class TestResolvePath(unittest.TestCase):
    def resolve(self, requested, hook=None):
        return resolve_path(ROOT, requested, hook)

    def test_traversal_battery_rejected(self):
        cases = {
            "..": "escapes_volume",
            "../x": "escapes_volume",
            "a/../../x": "escapes_volume",
            "a/../..": "escapes_volume",
            "./..": "escapes_volume",
            "..\\..\\etc": "escapes_volume",  # windows separators
            "a/../../../vol/ws-b/secret": "escapes_volume",
            "/etc/passwd": "absolute_path",
            "//etc/passwd": "absolute_path",
            "/..": "absolute_path",
            "\\/etc/passwd": "absolute_path",  # backslash is a separator
            "a\0b": "nul_byte",
            "": "empty_path",
        }
        for requested, reason in cases.items():
            with self.subTest(requested=requested):
                resolution = self.resolve(requested)
                self.assertFalse(resolution.ok)
                self.assertIsNone(resolution.path)
                self.assertEqual(resolution.reason, reason)

    def test_not_a_string(self):
        for bad in (None, 123, b"bytes", ["a"]):
            with self.subTest(bad=bad):
                resolution = resolve_path(ROOT, bad)
                self.assertEqual((resolution.ok, resolution.reason),
                                 (False, "not_a_string"))
        self.assertEqual(resolve_path(None, "a").reason, "not_a_string")

    def test_normalization_stays_confined(self):
        ok_cases = {
            "readme.md": f"{ROOT}/readme.md",
            "src/app.py": f"{ROOT}/src/app.py",
            "src/./lib/x.js": f"{ROOT}/src/lib/x.js",
            "src//lib//x.js": f"{ROOT}/src/lib/x.js",
            "src/old/../app.py": f"{ROOT}/src/app.py",  # interior .. stays in
            ".": ROOT,
            "./": ROOT,
            "src/": f"{ROOT}/src",
        }
        for requested, expected in ok_cases.items():
            with self.subTest(requested=requested):
                resolution = self.resolve(requested)
                self.assertTrue(resolution.ok, resolution.reason)
                self.assertEqual(resolution.path, expected)
                # the confinement invariant itself
                self.assertTrue(resolution.path == ROOT
                                or resolution.path.startswith(ROOT + "/"))

    def test_symlink_on_any_component_rejected(self):
        link = f"{ROOT}/link"
        for requested, hit in (("link", link), ("link/data.txt", link),
                               ("a", ROOT)):  # last one: the root itself
            hook = const_hook({hit: snap(ino=1, is_symlink=True)})
            with self.subTest(requested=requested):
                resolution = self.resolve(requested, hook)
                self.assertEqual((resolution.ok, resolution.reason),
                                 (False, "symlink"))

    def test_hardlink_rejected(self):
        hook = const_hook({f"{ROOT}/data.bin": snap(ino=1, is_hardlink=True)})
        resolution = self.resolve("data.bin", hook)
        self.assertEqual((resolution.ok, resolution.reason),
                         (False, "hardlink"))

    def test_toctou_changing_stat_rejected(self):
        hook = ChangingStat(final=f"{ROOT}/out.bin")
        resolution = self.resolve("out.bin", hook)
        self.assertEqual((resolution.ok, resolution.reason),
                         (False, "toctou_changed"))
        self.assertGreaterEqual(hook.calls, 2)  # re-stat really happened

    def test_stable_stat_passes_and_nonexistent_is_none(self):
        hook = const_hook({ROOT: snap(ino=1)})  # target simply absent
        resolution = self.resolve("new/file.txt", hook)
        self.assertTrue(resolution.ok, resolution.reason)
        self.assertEqual(resolution.path, f"{ROOT}/new/file.txt")

    def test_stat_error_rejected(self):
        def raising_hook(path):
            raise OSError("stat failed")
        resolution = self.resolve("a", raising_hook)
        self.assertEqual((resolution.ok, resolution.reason),
                         (False, "stat_error"))

    def test_without_hook_is_lexical_only(self):
        # documented: no hook -> no symlink defense possible; the runtime
        # MUST inject an lstat-based hook (spec, runtime TODO)
        resolution = self.resolve("link/data.txt")
        self.assertTrue(resolution.ok)


class TestAssertMutable(unittest.TestCase):
    def test_mutable_states_allow_writes(self):
        for state in MUTABLE_STATES:
            with self.subTest(state=state):
                self.assertEqual(assert_mutable({"state": state}),
                                 Verdict(True, "ok"))

    def test_suspend_is_read_only(self):
        self.assertEqual(assert_mutable({"state": "Suspend"}),
                         Verdict(False, "suspend_read_only"))

    def test_transitional_and_unknown_states_conservative(self):
        for state in ("Suspending", "Resuming", "Creating", "Destroying",
                      "Destroyed", "Error", "Lost", "Paused", None):
            with self.subTest(state=state):
                self.assertEqual(assert_mutable({"state": state}),
                                 Verdict(False, "not_mutable_state"))
        self.assertEqual(assert_mutable({}),
                         Verdict(False, "not_mutable_state"))
        self.assertEqual(assert_mutable("Active"),
                         Verdict(False, "not_mutable_state"))

    def test_warm_checkpoint_unverified_is_read_only(self):
        self.assertEqual(
            assert_mutable({"state": "Active",
                            "warm_checkpoint_verified": False}),
            Verdict(False, "warm_checkpoint_unverified"))
        self.assertEqual(
            assert_mutable({"state": "Idle",
                            "warm_checkpoint_verified": False}),
            Verdict(False, "warm_checkpoint_unverified"))
        # verified or flag absent -> writable
        self.assertEqual(
            assert_mutable({"state": "Active",
                            "warm_checkpoint_verified": True}),
            Verdict(True, "ok"))
        self.assertEqual(assert_mutable({"state": "Active"}),
                         Verdict(True, "ok"))
        # cold Suspend keeps its own (primary) reason either way
        self.assertEqual(
            assert_mutable({"state": "Suspend",
                            "warm_checkpoint_verified": False}),
            Verdict(False, "suspend_read_only"))


class TestVerifyUpload(unittest.TestCase):
    def test_matching_hash_allowed_case_insensitive(self):
        digest = hashlib.sha256(b"payload").hexdigest()
        self.assertEqual(verify_upload(digest, digest), Verdict(True, "ok"))
        self.assertEqual(verify_upload(digest.upper(), digest.lower()),
                         Verdict(True, "ok"))

    def test_mismatch_is_explicit_failure(self):
        actual = hashlib.sha256(b"payload").hexdigest()
        expected = hashlib.sha256(b"other").hexdigest()
        self.assertEqual(verify_upload(actual, expected),
                         Verdict(False, "sha256_mismatch"))

    def test_invalid_digest_shapes_rejected(self):
        digest = hashlib.sha256(b"x").hexdigest()
        for bad in ("z" * 64, digest[:63], digest + "a", None, 123, b"x"):
            with self.subTest(bad=bad):
                self.assertEqual(verify_upload(bad, digest).reason,
                                 "invalid_digest")
                self.assertEqual(verify_upload(digest, bad).reason,
                                 "invalid_digest")


class TestCommitAfterVerify(unittest.TestCase):
    def test_mismatch_leaves_existing_artifact_untouched(self):
        store = {"/vol/a/out.bin": b"old-bytes"}
        verdict, now = commit_after_verify(
            store, "/vol/a/out.bin", b"new-bytes",
            hashlib.sha256(b"neither").hexdigest())
        self.assertEqual(verdict.reason, "sha256_mismatch")
        self.assertFalse(verdict.allowed)
        self.assertEqual(now, b"old-bytes")
        self.assertEqual(store["/vol/a/out.bin"], b"old-bytes")

    def test_mismatch_writes_nothing_at_all(self):
        store = {}
        verdict, now = commit_after_verify(
            store, "/vol/a/out.bin", b"new-bytes",
            hashlib.sha256(b"other").hexdigest())
        self.assertFalse(verdict.allowed)
        self.assertIsNone(now)
        self.assertNotIn("/vol/a/out.bin", store)

    def test_match_writes_verified_bytes(self):
        store = {"/vol/a/out.bin": b"old-bytes"}
        verdict, now = commit_after_verify(
            store, "/vol/a/out.bin", b"new-bytes",
            hashlib.sha256(b"new-bytes").hexdigest())
        self.assertTrue(verdict.allowed)
        self.assertEqual(now, b"new-bytes")
        self.assertEqual(store["/vol/a/out.bin"], b"new-bytes")


class TestUploadQuotaGate(unittest.TestCase):
    def test_reservation_bounded_by_cap(self):
        gate = UploadQuotaGate(100)
        self.assertTrue(gate.reserve_quota(60))
        self.assertFalse(gate.reserve_quota(50))
        self.assertTrue(gate.reserve_quota(40))
        self.assertEqual(gate.reserved(), 100)
        gate.release(60)
        self.assertEqual(gate.reserved(), 40)
        self.assertTrue(gate.reserve_quota(60))
        gate.release(999)  # over-release floors at zero
        self.assertEqual(gate.reserved(), 0)

    def test_invalid_pending_bytes_is_a_caller_bug(self):
        gate = UploadQuotaGate(100)
        for bad in (-1, "10", 1.5, None, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gate.reserve_quota(bad)

    def test_threaded_race_never_exceeds_cap(self):
        """The over-reservation guard: 60 barrier-synced threads each
        reserve 10 bytes against a 200-byte cap. Removing the lock in
        reserve_quota (NoLockQuotaGate below) makes this FAIL: the gate
        would over-reserve exactly like a naive count-then-commit."""
        threads, chunk, cap, stall = 60, 10, 200, 0.005
        gate = UploadQuotaGate(cap, stall=lambda: time.sleep(stall))
        admitted = []

        def reserve():
            if gate.reserve_quota(chunk):
                admitted.append(chunk)

        run_concurrently(threads, reserve)
        self.assertEqual(sum(admitted), cap)  # bounded exactly, not "at most"
        self.assertEqual(gate.reserved(), cap)


class TestIgnoreFilter(unittest.TestCase):
    def test_founder_list_exact_defaults(self):
        paths = ["src/a.py", ".env", ".env.local", "config/.env.prod",
                 "cert.pem", "keys/server.pem", "id_rsa", "id_rsa.pub",
                 "ssh/id_rsa.example", "node_modules/pkg/x.js",
                 "a/node_modules", ".git/HEAD", "env.txt", "pem.txt",
                 "my_id_rsa_notes.md", "README.md"]
        kept, excluded = ignore_filter(paths)
        self.assertEqual(kept, ["src/a.py", ".git/HEAD", "env.txt",
                                "pem.txt", "my_id_rsa_notes.md", "README.md"])
        self.assertEqual(excluded, [".env", ".env.local", "config/.env.prod",
                                    "cert.pem", "keys/server.pem", "id_rsa",
                                    "id_rsa.pub", "ssh/id_rsa.example",
                                    "node_modules/pkg/x.js", "a/node_modules"])

    def test_git_segment_optional(self):
        paths = [".git/HEAD", "proj/.git/config", "src/a.py"]
        self.assertEqual(ignore_filter(paths)[0],
                         [".git/HEAD", "proj/.git/config", "src/a.py"])
        kept, excluded = ignore_filter(paths, include_git=True)
        self.assertEqual(kept, ["src/a.py"])
        self.assertEqual(excluded, [".git/HEAD", "proj/.git/config"])

    def test_partition_returns_every_path_for_ui_display(self):
        paths = [".env", "a.py", "node_modules/x", "b.js"]
        kept, excluded = ignore_filter(paths)
        self.assertEqual(sorted(kept + excluded), sorted(paths))  # G07 list


class TestTarPolicy(unittest.TestCase):
    def test_ok_entries(self):
        policy = TarPolicy()
        for entry in ({"name": "a.txt", "size": 10, "kind": "file"},
                      {"name": "src", "kind": "dir"},
                      {"name": "./x.txt", "size": 1},
                      {"name": "src/old/../a.txt", "size": 2},  # interior ..
                      {"name": "src//lib/", "kind": "dir"}):
            with self.subTest(entry=entry):
                self.assertEqual(policy.check(entry), Verdict(True, "ok"))

    def test_name_battery_rejected(self):
        policy = TarPolicy()
        cases = {"/etc/passwd": "absolute_path",
                 "/abs/x": "absolute_path",
                 "..": "escapes_volume",
                 "../x": "escapes_volume",
                 "a/../../x": "escapes_volume",
                 "..\\..\\etc": "escapes_volume",
                 "a\0b": "nul_byte",
                 "": "invalid_entry"}
        for name, reason in cases.items():
            with self.subTest(name=name):
                verdict = policy.check({"name": name, "size": 0})
                self.assertEqual((verdict.allowed, verdict.reason),
                                 (False, reason))
        self.assertEqual(policy.check({"name": 5, "size": 0}).reason,
                         "invalid_entry")
        self.assertEqual(policy.check("not-a-dict").reason, "invalid_entry")

    def test_kind_battery_rejected(self):
        policy = TarPolicy()
        for kind, reason in (("symlink", "symlink_entry"),
                             ("hardlink", "hardlink_entry"),
                             ("device", "device_entry"),
                             ("special", "special_entry"),
                             ("fifo", "invalid_entry")):
            with self.subTest(kind=kind):
                verdict = policy.check({"name": "x", "size": 0, "kind": kind})
                self.assertEqual((verdict.allowed, verdict.reason),
                                 (False, reason))

    def test_invalid_size_rejected(self):
        policy = TarPolicy()
        for size in (-1, "10", 1.5, None, True):
            with self.subTest(size=size):
                self.assertEqual(
                    policy.check({"name": "x", "size": size}).reason,
                    "invalid_entry")

    def test_total_size_cap_and_no_partial_commit(self):
        policy = TarPolicy(max_total_bytes=100)
        self.assertTrue(policy.check({"name": "a", "size": 60}).allowed)
        self.assertEqual(
            policy.check({"name": "b", "size": 60}).reason, "total_size_cap")
        self.assertEqual((policy.entry_count, policy.total_bytes), (1, 60))
        # the rejected entry committed nothing: a fitting one still passes
        self.assertTrue(policy.check({"name": "c", "size": 40}).allowed)
        self.assertEqual(policy.total_bytes, 100)
        self.assertEqual(
            policy.check({"name": "d", "size": 1}).reason, "total_size_cap")

    def test_entry_count_cap(self):
        policy = TarPolicy(max_entries=2)
        self.assertTrue(policy.check({"name": "a"}).allowed)
        self.assertTrue(policy.check({"name": "b", "size": 5}).allowed)
        self.assertEqual(policy.check({"name": "c"}).reason, "entry_count_cap")

    def test_first_version_constants_are_the_documented_defaults(self):
        # pinned so the spec (docs/contracts/files-policy.md) and the code
        # cannot drift apart silently
        self.assertEqual(TAR_MAX_TOTAL_BYTES, 512 * 1024 * 1024)
        self.assertEqual(TAR_MAX_ENTRIES, 10_000)


class TestAuthorizeFileOp(unittest.TestCase):
    def setUp(self):
        self.authz = Authorize()
        register_sandbox_volume(self.authz, "sbx-a", "ws_a", "vol-a1")
        self.registry = TokenRegistry()
        self.hash_a = self.registry.register("token-alice", "ws_a",
                                             user="alice")
        self.hash_b = self.registry.register("token-bob", "ws_b", user="bob")

    def scope(self, **overrides):
        scope = {"workspace_id": "ws_a", "sandbox_id": "sbx-a",
                 "volume": "vol-a1"}
        scope.update(overrides)
        return scope

    def test_same_workspace_allowed(self):
        self.assertEqual(
            authorize_file_op(self.authz, self.hash_a, "ws_a", "alice",
                              self.scope()), ALLOW)

    def test_cross_workspace_404(self):
        decision = authorize_file_op(self.authz, self.hash_b, "ws_b", "bob",
                                     self.scope())
        self.assertEqual(decision, NOT_FOUND)
        self.assertEqual(decision.status, 404)

    def test_cross_workspace_indistinguishable_from_unknown_id(self):
        cross = authorize_file_op(self.authz, self.hash_b, "ws_b", "bob",
                                  self.scope())
        unknown = authorize_file_op(self.authz, self.hash_b, "ws_b", "bob",
                                    self.scope(sandbox_id="sbx-z"))
        self.assertEqual(cross, unknown)  # no probe signal

    def test_volume_mismatch_404_like_unknown(self):
        decision = authorize_file_op(self.authz, self.hash_a, "ws_a",
                                     "alice", self.scope(volume="vol-b9"))
        self.assertEqual(decision, NOT_FOUND)
        self.assertEqual(
            decision,
            authorize_file_op(self.authz, self.hash_a, "ws_a", "alice",
                              self.scope(sandbox_id="no-such")))

    def test_workspace_scope_claim_mismatch_404(self):
        decision = authorize_file_op(self.authz, self.hash_a, "ws_a",
                                     "alice", self.scope(workspace_id="ws_b"))
        self.assertEqual(decision, NOT_FOUND)

    def test_sandbox_without_registered_volume_not_file_addressable(self):
        self.authz.register_resource("sbx-plain", "ws_a")
        decision = authorize_file_op(self.authz, self.hash_a, "ws_a",
                                     "alice",
                                     self.scope(sandbox_id="sbx-plain",
                                                volume="vol-a1"))
        self.assertEqual(decision, NOT_FOUND)

    def test_unknown_token_401_via_delegation(self):
        decision = authorize_file_op(self.authz, None, None, None,
                                     self.scope())
        self.assertEqual(decision, UNAUTHORIZED)

    def test_owner_scoped_resource_403_via_delegation(self):
        register_sandbox_volume(self.authz, "sbx-alice", "ws_a", "vol-a2",
                                owner="alice")
        hash_c = self.registry.register("token-carol", "ws_a", user="carol")
        decision = authorize_file_op(self.authz, hash_c, "ws_a", "carol",
                                     self.scope(sandbox_id="sbx-alice",
                                                volume="vol-a2"))
        self.assertEqual(decision, FORBIDDEN)

    def test_missing_scope_key_is_a_caller_bug(self):
        for key in ("workspace_id", "sandbox_id", "volume"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                authorize_file_op(self.authz, self.hash_a, "ws_a", "alice",
                                  self.scope(**{key: ""}))
        with self.assertRaises(ValueError):
            authorize_file_op(self.authz, self.hash_a, "ws_a", "alice", None)


# --------------------------------------------------------------------- guards
# Each mutant breaks one safeguard at a deliberate mutation point; each
# guard proves the invariant flips on the mutant and holds on the real
# rules (non-vacuity, CONTRIBUTING mutate-and-fail idiom).


class NoLockQuota(UploadQuotaGate):
    def reserve_quota(self, pending_bytes):
        used = self._reserved  # the lock removed
        self._stall()
        if used + pending_bytes > self.limit:
            return False
        self._reserved = used + pending_bytes
        return True


def _permissive_confine(segments):
    # the confinement guard removed: ".." is kept as a literal segment
    return [s for s in segments if s not in ("", ".")], None


class SingleStat:
    """Runtime mutant: lstat each path once and reuse the snapshot (the
    re-stat after resolution never happens)."""

    def __init__(self, hook):
        self._hook = hook
        self._cache = {}

    def __call__(self, path):
        if path not in self._cache:
            self._cache[path] = self._hook(path)
        return self._cache[path]


def always_mutable(sandbox_state):
    return Verdict(True, "ok")  # the suspend read-only gate removed


def write_regardless(store, path, payload, expected_sha256):
    verdict = verify_upload(hashlib.sha256(payload).hexdigest(),
                            expected_sha256)
    store[path] = payload  # the bug: writes before the hash gate decides
    return verdict


class UncappedTar(TarPolicy):
    def __init__(self):
        super().__init__(max_total_bytes=10 ** 18, max_entries=10 ** 9)


def keep_everything(paths, include_git=False):
    return list(paths), []  # the ignore filter removed


class ProbeLeakFileAuthz(Authorize):
    def __call__(self, token_hash, workspace_of_token, user_of_token,
                 request):
        decision = super().__call__(token_hash, workspace_of_token,
                                    user_of_token, request)
        if decision.status == 404:  # the leak: distinct cross-workspace answer
            return Decision(False, 403, "forbidden")
        return decision


class MutationGuards(unittest.TestCase):
    def setUp(self):
        self.authz = Authorize()
        register_sandbox_volume(self.authz, "sbx-a", "ws_a", "vol-a1")
        self.registry = TokenRegistry()
        self.hash_a = self.registry.register("token-alice", "ws_a",
                                             user="alice")
        self.hash_b = self.registry.register("token-bob", "ws_b", user="bob")
        self.scope = {"workspace_id": "ws_a", "sandbox_id": "sbx-a",
                      "volume": "vol-a1"}

    def test_guard_no_lock_breaks_quota_race(self):
        threads, chunk, cap, stall = 60, 10, 200, 0.005

        def reserved_total(gate):
            admitted = []

            def reserve():
                if gate.reserve_quota(chunk):
                    admitted.append(chunk)

            run_concurrently(threads, reserve)
            return sum(admitted)

        real = UploadQuotaGate(cap, stall=lambda: time.sleep(stall))
        self.assertEqual(reserved_total(real), cap)
        broken = NoLockQuota(cap, stall=lambda: time.sleep(stall))
        self.assertGreater(reserved_total(broken), cap)  # over-reserves

    def test_guard_permissive_confine_breaks_traversal_probe(self):
        real = resolve_path(ROOT, "../x")
        self.assertEqual((real.ok, real.reason), (False, "escapes_volume"))
        with patch.object(files_policy, "_lexical_confine",
                          _permissive_confine):
            broken = resolve_path(ROOT, "../x")
        self.assertTrue(broken.ok)  # the escape walks straight out
        self.assertEqual(broken.path, f"{ROOT}/../x")

    def test_guard_no_restat_breaks_toctou_probe(self):
        hook = ChangingStat(final=f"{ROOT}/out.bin")
        real = resolve_path(ROOT, "out.bin", ChangingStat(
            final=f"{ROOT}/out.bin"))
        self.assertEqual((real.ok, real.reason),
                         (False, "toctou_changed"))
        broken = resolve_path(ROOT, "out.bin", SingleStat(hook))
        self.assertTrue(broken.ok)  # the swap goes undetected

    def test_guard_always_mutable_breaks_suspend_probe(self):
        self.assertEqual(assert_mutable({"state": "Suspend"}),
                         Verdict(False, "suspend_read_only"))
        self.assertEqual(always_mutable({"state": "Suspend"}),
                         Verdict(True, "ok"))  # writes into a suspended box

    def test_guard_write_regardless_breaks_no_overwrite_probe(self):
        expected = hashlib.sha256(b"neither").hexdigest()
        real_store = {"/vol/a/out.bin": b"old-bytes"}
        commit_after_verify(real_store, "/vol/a/out.bin", b"new-bytes",
                            expected)
        self.assertEqual(real_store["/vol/a/out.bin"], b"old-bytes")
        broken_store = {"/vol/a/out.bin": b"old-bytes"}
        write_regardless(broken_store, "/vol/a/out.bin", b"new-bytes",
                         expected)
        self.assertEqual(broken_store["/vol/a/out.bin"],
                         b"new-bytes")  # the artifact was overwritten

    def test_guard_uncapped_tar_breaks_cap_probe(self):
        real = TarPolicy(max_total_bytes=100)
        real.check({"name": "a", "size": 60})
        self.assertEqual(real.check({"name": "b", "size": 60}).reason,
                         "total_size_cap")
        broken = UncappedTar()
        self.assertTrue(broken.check({"name": "a", "size": 60}).allowed)
        self.assertTrue(broken.check({"name": "b", "size": 60}).allowed)
        count_real = TarPolicy(max_entries=2)
        count_real.check({"name": "a"})
        count_real.check({"name": "b"})
        self.assertEqual(count_real.check({"name": "c"}).reason,
                         "entry_count_cap")
        broken_count = UncappedTar()
        for name in ("a", "b", "c"):
            self.assertTrue(broken_count.check({"name": name}).allowed)

    def test_guard_keep_everything_breaks_ignore_probe(self):
        paths = ["src/a.py", ".env", "id_rsa", "node_modules/x.js"]
        kept, excluded = ignore_filter(paths)
        self.assertEqual(kept, ["src/a.py"])
        self.assertEqual(len(excluded), 3)
        # a removed filter would upload .env straight into general storage
        # and breach the #21 secret-storage rule
        self.assertEqual(keep_everything(paths), (paths, []))

    def test_guard_probe_leak_breaks_404_rule(self):
        real = authorize_file_op(self.authz, self.hash_b, "ws_b", "bob",
                                 self.scope)
        self.assertEqual(real.status, 404)
        leaking = ProbeLeakFileAuthz(dict(self.authz.resources))
        broken = authorize_file_op(leaking, self.hash_b, "ws_b", "bob",
                                   self.scope)
        self.assertEqual(broken.status, 403)  # the cross-tenant probe signal


if __name__ == "__main__":
    unittest.main()
