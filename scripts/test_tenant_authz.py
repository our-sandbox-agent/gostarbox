"""Tests for the #16 tenant authorization rules (scripts/tenant_authz.py).

Coverage per the issue acceptance (in-repo rules slice):
  - A/B negative matrix across all 7 endpoint classes (404 no-probe rule,
    403 cross-user within a shared workspace, 401 unknown/revoked token)
  - terminal ticket: TTL (injected clock), single-use, rebinding and
    generation-change rejection, hashed storage and log redaction
  - token discipline: sha256 hash/verify, revocation list, hashed-only storage
  - QuotaGate: admission bounded exactly under a threaded race — the test
    FAILS if the lock is removed (a NoLockQuotaGate mutant over-admits where
    the real gate does not); create rate-limit window
  - invite allowlist gate (no open registration)
  - mutation guards (CONTRIBUTING mutate-and-fail idiom): each guard breaks one
    safeguard at a deliberate point and asserts the invariant flips there and
    holds on the real implementation.
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tenant_authz import (ALLOW, ENDPOINT_CLASSES, FORBIDDEN, NOT_FOUND,  # noqa: E402
                          UNAUTHORIZED, Authorize, Decision, InviteList,
                          QuotaGate, TerminalTicketPolicy, TokenRegistry,
                          hash_token, new_token, redact, run_concurrently,
                          verify_token)


class Clock:
    """Injected monotonic clock: advance() is the only way time passes."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def world():
    """A/B workspaces with one resource per endpoint class in each."""
    authz = Authorize()
    for endpoint in ENDPOINT_CLASSES:
        authz.register_resource(f"{endpoint}-a", "ws_a", owner="alice")
        authz.register_resource(f"{endpoint}-b", "ws_b", owner="bob")
    return authz


def ab_tokens():
    registry = TokenRegistry()
    hash_a = registry.register("token-alice", "ws_a", user="alice")
    hash_b = registry.register("token-bob", "ws_b", user="bob")
    return registry, hash_a, hash_b


class TestAuthorizeMatrix(unittest.TestCase):
    def setUp(self):
        self.authz = world()
        self.registry, self.hash_a, self.hash_b = ab_tokens()

    def test_same_workspace_allowed_all_classes(self):
        for endpoint in ENDPOINT_CLASSES:
            request = {"endpoint_class": endpoint, "resource_id": f"{endpoint}-a",
                       "user": "alice"}
            self.assertEqual(
                self.authz(self.hash_a, "ws_a", "alice", request), ALLOW, endpoint)

    def test_cross_workspace_404_all_classes(self):
        for endpoint in ENDPOINT_CLASSES:
            request = {"endpoint_class": endpoint, "resource_id": f"{endpoint}-b",
                       "user": "alice"}
            decision = self.authz(self.hash_a, "ws_a", "alice", request)
            self.assertEqual(decision.status, 404, endpoint)
            self.assertFalse(decision.allowed)

    def test_unknown_resource_404_all_classes(self):
        for endpoint in ENDPOINT_CLASSES:
            request = {"endpoint_class": endpoint, "resource_id": "forged-id",
                       "user": "alice"}
            self.assertEqual(
                self.authz(self.hash_a, "ws_a", "alice", request), NOT_FOUND, endpoint)

    def test_cross_workspace_indistinguishable_from_unknown(self):
        """No probe signal: forged and cross-workspace ids answer identically."""
        for endpoint in ENDPOINT_CLASSES:
            cross = self.authz(self.hash_a, "ws_a", "alice",
                               {"endpoint_class": endpoint,
                                "resource_id": f"{endpoint}-b", "user": "alice"})
            unknown = self.authz(self.hash_a, "ws_a", "alice",
                                 {"endpoint_class": endpoint,
                                  "resource_id": "no-such-id", "user": "alice"})
            self.assertEqual(cross, unknown, endpoint)

    def test_unknown_token_401_all_classes(self):
        for endpoint in ENDPOINT_CLASSES:
            request = {"endpoint_class": endpoint, "resource_id": f"{endpoint}-a"}
            self.assertEqual(
                self.authz(None, None, None, request), UNAUTHORIZED, endpoint)

    def test_revoked_token_401_all_classes(self):
        # revocation is enforced where workspace_of_token is resolved: a
        # revoked token looks up to no workspace, and Authorize answers 401
        self.registry.revoke(self.hash_a)
        record = self.registry.lookup("token-alice")
        workspace = record["workspace"] if record else None
        for endpoint in ENDPOINT_CLASSES:
            request = {"endpoint_class": endpoint, "resource_id": f"{endpoint}-a",
                       "user": "alice"}
            self.assertEqual(
                self.authz(self.hash_a, workspace, "alice", request), UNAUTHORIZED,
                endpoint)

    def test_cross_user_same_workspace_403(self):
        # carol shares ws_a with alice; alice's user-scoped resource exists for
        # her, but acting on it as carol is forbidden — 403, not a 404 mask
        hash_c = self.registry.register("token-carol", "ws_a", user="carol")
        request = {"endpoint_class": "secrets", "resource_id": "secrets-a",
                   "user": "carol"}
        self.assertEqual(self.authz(hash_c, "ws_a", "carol", request), FORBIDDEN)
        # a workspace-scoped (ownerless) resource stays shared-readable
        self.authz.register_resource("ws-a-policy", "ws_a")
        self.assertEqual(self.authz(hash_c, "ws_a", "carol",
                                    {"endpoint_class": "api",
                                     "resource_id": "ws-a-policy"}), ALLOW)

    def test_unknown_endpoint_class_rejected(self):
        with self.assertRaises(ValueError):
            self.authz(self.hash_a, "ws_a", "alice",
                       {"endpoint_class": "admin", "resource_id": "x"})

    def test_spoofed_request_user_ignored(self):
        # carol's token claims user=alice inside the request body — the
        # principal comes from the token record only, so alice's user-scoped
        # secret stays forbidden (and no user field means the same).
        hash_c = self.registry.register("token-carol", "ws_a", user="carol")
        spoof = {"endpoint_class": "secrets", "resource_id": "secrets-a",
                 "user": "alice"}
        self.assertEqual(self.authz(hash_c, "ws_a", "carol", spoof), FORBIDDEN)

    def test_principalless_token_denied_on_owned_resource(self):
        # a token with no resolved user (deny-by-default) may not touch a
        # user-scoped resource even in its own workspace
        self.assertEqual(
            self.authz(self.hash_a, "ws_a", None,
                       {"endpoint_class": "secrets",
                        "resource_id": "secrets-a"}), FORBIDDEN)


class TestTokenDiscipline(unittest.TestCase):
    def test_hash_is_sha256_hex(self):
        digest = hash_token("token-alice")
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, hash_token("token-alice"))
        self.assertNotEqual(digest, hash_token("token-bob"))
        int(digest, 16)  # pure hex

    def test_verify_accepts_and_rejects(self):
        token_hash = hash_token("token-alice")
        self.assertTrue(verify_token("token-alice", token_hash))
        self.assertFalse(verify_token("token-bob", token_hash))
        self.assertFalse(verify_token("token-alice", "deadbeef"))

    def test_registry_lookup_and_workspace_of(self):
        registry, hash_a, _ = ab_tokens()
        self.assertEqual(registry.lookup("token-alice")["workspace"], "ws_a")
        self.assertIsNone(registry.lookup("token-bogus"))

    def test_revocation_kills_access_but_not_the_hash(self):
        registry, hash_a, _ = ab_tokens()
        registry.revoke(hash_a)
        self.assertTrue(registry.is_revoked(hash_a))
        self.assertIsNone(registry.lookup("token-alice"))
        # hashing is authentication, revocation is authorization: the hash
        # still verifies, which is why the 401 must come from the revocation
        self.assertTrue(verify_token("token-alice", hash_a))

    def test_registry_stores_hashes_only(self):
        registry = TokenRegistry()
        token = new_token()
        registry.register(token, "ws_a", user="alice")
        registry.revoke(hash_token(token))  # exercise the revocation set too
        self.assertNotIn(token, json.dumps(registry._by_hash))
        self.assertNotIn(token, repr(registry))
        self.assertNotIn(token, repr(registry._revoked))


class TestTerminalTicketPolicy(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.policy = TerminalTicketPolicy(clock=self.clock, ttl_s=60.0)

    def ticket(self, generation=3, **overrides):
        binding = {"user": "alice", "workspace": "ws_a",
                   "sandbox_id": "sbx-a", "generation": generation}
        binding.update(overrides)
        return self.policy.issue(**binding), binding

    def test_redeem_once_then_replay_rejected(self):
        ticket_id, binding = self.ticket()
        ok, code = self.policy.redeem(ticket_id, **binding)
        self.assertTrue(ok)
        ok, code = self.policy.redeem(ticket_id, **binding)
        self.assertFalse(ok)
        self.assertEqual(code, "replay")

    def test_reconnect_uses_a_new_ticket(self):
        ticket_id, binding = self.ticket()
        self.assertTrue(self.policy.redeem(ticket_id, **binding)[0])
        fresh, _ = self.ticket()
        self.assertNotEqual(fresh, ticket_id)
        self.assertTrue(self.policy.redeem(fresh, **binding)[0])

    def test_ttl_expiry_with_injected_clock(self):
        ticket_id, binding = self.ticket()
        self.clock.advance(61.0)
        ok, code = self.policy.redeem(ticket_id, **binding)
        self.assertFalse(ok)
        self.assertEqual(code, "expired")

    def test_within_ttl_and_configurable_ttl(self):
        ticket_id, binding = self.ticket()
        self.clock.advance(59.9)
        self.assertTrue(self.policy.redeem(ticket_id, **binding)[0])
        short = TerminalTicketPolicy(clock=self.clock, ttl_s=5.0)
        binding = ("alice", "ws_a", "sbx-a", 3)
        ticket_id = short.issue(*binding)
        self.clock.advance(4.9)
        self.assertTrue(short.redeem(ticket_id, *binding)[0])
        ticket_id = short.issue(*binding)
        self.clock.advance(5.1)
        self.assertEqual(short.redeem(ticket_id, *binding), (False, "expired"))

    def test_rebinding_rejected_user_workspace_sandbox(self):
        ticket_id, binding = self.ticket()
        for override in ({"user": "bob"}, {"workspace": "ws_b"},
                         {"sandbox_id": "sbx-b"}):
            with self.subTest(override=override):
                ok, code = self.policy.redeem(
                    ticket_id, **{**binding, **override})
                self.assertEqual((ok, code), (False, "binding_mismatch"))

    def test_generation_change_rejected(self):
        ticket_id, binding = self.ticket(generation=1)
        ok, code = self.policy.redeem(ticket_id, **{**binding, "generation": 2})
        self.assertEqual((ok, code), (False, "binding_mismatch"))
        self.assertTrue(
            self.policy.redeem(ticket_id, **binding)[0])  # right generation

    def test_unknown_ticket_rejected(self):
        ok, code = self.policy.redeem("tt-forged", "alice", "ws_a", "sbx-a", 1)
        self.assertEqual((ok, code), (False, "unknown_ticket"))

    def test_redaction_never_contains_clear_id(self):
        ticket_id, _ = self.ticket()
        shown = redact(ticket_id)
        self.assertTrue(shown.startswith("tt_"))
        self.assertNotIn(ticket_id, shown)
        self.assertEqual(shown, redact(ticket_id))  # stable across calls
        self.assertNotEqual(shown, redact("tt-other"))

    def test_store_never_contains_clear_id(self):
        ticket_id, _ = self.ticket()
        self.assertNotIn(ticket_id, json.dumps(self.policy._tickets))
        self.assertNotIn(ticket_id, repr(self.policy))


class TestQuotaGate(unittest.TestCase):
    def test_admission_bounded_by_quota(self):
        gate = QuotaGate()
        self.assertTrue(gate.try_admit("ws_a", 1, 3))
        self.assertTrue(gate.try_admit("ws_a", 1, 3))
        self.assertFalse(gate.try_admit("ws_a", 2, 3))
        self.assertTrue(gate.try_admit("ws_a", 1, 3))
        self.assertFalse(gate.try_admit("ws_a", 1, 3))
        self.assertEqual(gate.used("ws_a"), 3)

    def test_release_returns_budget(self):
        gate = QuotaGate()
        gate.try_admit("ws_a", 3, 3)
        gate.release("ws_a", 2)
        self.assertTrue(gate.try_admit("ws_a", 2, 3))
        gate.release("ws_a", 99)  # over-release floors at zero
        self.assertEqual(gate.used("ws_a"), 0)

    def test_workspaces_are_independent(self):
        gate = QuotaGate()
        self.assertTrue(gate.try_admit("ws_a", 5, 5))
        self.assertTrue(gate.try_admit("ws_b", 5, 5))
        self.assertFalse(gate.try_admit("ws_a", 1, 5))
        self.assertFalse(gate.try_admit("ws_b", 1, 5))

    def test_threaded_race_naive_over_admits_gate_does_not(self):
        """The over-admission guard: 60 barrier-synced threads, quota 20.

        The naive count-then-insert admits more than the quota; QuotaGate
        admits exactly the quota even with the check->commit window held open
        by the stall hook.  Deleting the lock in QuotaGate.try_admit makes the
        gate behave like the naive closure and FAILS this test.
        """
        threads, quota, stall = 60, 20, 0.005
        gate = QuotaGate(stall=lambda: time.sleep(stall))
        counters = {"naive": 0, "gate": 0}
        naive_used = {}

        def naive_admit():
            used = naive_used.get("ws", 0)  # count ...
            time.sleep(stall)  # ... then insert, unlocked: the forbidden pattern
            if used + 1 <= quota:
                naive_used["ws"] = used + 1
                counters["naive"] += 1

        def gated_admit():
            if gate.try_admit("ws", 1, quota):
                counters["gate"] += 1

        run_concurrently(threads, naive_admit)
        run_concurrently(threads, gated_admit)
        self.assertGreater(counters["naive"], quota)  # the bug demonstrated
        self.assertEqual(counters["gate"], quota)  # bounded exactly
        self.assertEqual(gate.used("ws"), quota)

    def test_create_rate_limit_window(self):
        gate = QuotaGate()
        for _ in range(3):
            self.assertTrue(gate.try_create("ws_a", now=1000.0, limit=3,
                                            window_s=60.0))
        self.assertFalse(gate.try_create("ws_a", now=1000.5, limit=3,
                                         window_s=60.0))
        self.assertTrue(gate.try_create("ws_a", now=1060.1, limit=3,
                                        window_s=60.0))  # first expired
        self.assertTrue(gate.try_create("ws_b", now=1000.0, limit=3,
                                        window_s=60.0))  # per-workspace


class TestInviteList(unittest.TestCase):
    def test_login_requires_invite(self):
        invites = InviteList()
        self.assertFalse(invites.is_invited("alice@example.com"))
        invites.invite("alice@example.com")
        self.assertTrue(invites.is_invited("alice@example.com"))
        invites.revoke("alice@example.com")
        self.assertFalse(invites.is_invited("alice@example.com"))

    def test_no_open_registration(self):
        invites = InviteList()
        for email in ("a@example.com", "b@example.com", "c@evil.test"):
            self.assertFalse(invites.is_invited(email))


# --------------------------------------------------------------------- guards
# Each mutant breaks one safeguard at a deliberate mutation point; each guard
# proves the invariant flips on the mutant and holds on the real rules
# (non-vacuity, CONTRIBUTING mutate-and-fail idiom).

class NoLockQuotaGate(QuotaGate):
    def try_admit(self, workspace, count, quota):
        used = self._used.get(workspace, 0)  # the lock removed
        self._stall()
        if used + count > quota:
            return False
        self._used[workspace] = used + count
        return True


class ProbeLeakAuthorize(Authorize):
    def __call__(self, token_hash, workspace_of_token, user_of_token, request):
        decision = super().__call__(token_hash, workspace_of_token, user_of_token, request)
        if decision.status == 404:  # the leak: distinct cross-workspace answer
            return Decision(False, 403, "forbidden")
        return decision


class NonConsumingTickets(TerminalTicketPolicy):
    def redeem(self, ticket_id, user, workspace, sandbox_id, generation):
        ok, code = super().redeem(ticket_id, user, workspace, sandbox_id,
                                  generation)
        record = self._tickets.get(hash_token(ticket_id))
        if record is not None:
            record["consumed"] = False  # replayable
        return ok, code


class NoTTLTickets(TerminalTicketPolicy):
    def redeem(self, ticket_id, user, workspace, sandbox_id, generation):
        record = self._tickets.get(hash_token(ticket_id))
        if record is not None:
            record["issued_at"] = self._clock()  # expiry never trips
        return super().redeem(ticket_id, user, workspace, sandbox_id,
                              generation)


class UnboundTickets(TerminalTicketPolicy):
    def redeem(self, ticket_id, user, workspace, sandbox_id, generation):
        record = self._tickets.get(hash_token(ticket_id))
        if record is not None:  # accept any binding, not the issued one
            return super().redeem(ticket_id, record["user"],
                                  record["workspace"], record["sandbox_id"],
                                  record["generation"])
        return False, "unknown_ticket"


class ClearTicketStore(TerminalTicketPolicy):
    def issue(self, user, workspace, sandbox_id, generation):
        ticket_id = super().issue(user, workspace, sandbox_id, generation)
        self._tickets["leak"] = ticket_id  # the clear id stored
        return ticket_id


class OpenRegistration(InviteList):
    def is_invited(self, email):
        return True  # the gate removed


class PlainTokenRegistry(TokenRegistry):
    def register(self, token, workspace, user=None):
        token_hash = super().register(token, workspace, user)
        self._by_hash[token_hash]["token"] = token  # stored in the clear
        return token_hash


class MutationGuards(unittest.TestCase):
    def test_guard_no_lock_breaks_race_probe(self):
        threads, quota, stall = 60, 20, 0.005

        def raced(gate):
            admitted = []

            def admit():
                if gate.try_admit("ws", 1, quota):
                    admitted.append(1)

            run_concurrently(threads, admit)
            return sum(admitted)

        real = QuotaGate(stall=lambda: time.sleep(stall))
        self.assertEqual(raced(real), quota)
        broken = NoLockQuotaGate(stall=lambda: time.sleep(stall))
        self.assertGreater(raced(broken), quota)  # over-admits unlocked

    def test_guard_probe_leak_breaks_404_rule(self):
        authz = world()
        _, hash_a, _ = ab_tokens()
        request = {"endpoint_class": "file", "resource_id": "file-b",
                   "user": "alice"}
        self.assertEqual(authz(hash_a, "ws_a", "alice", request).status, 404)
        broken = ProbeLeakAuthorize(dict(authz.resources))
        self.assertEqual(broken(hash_a, "ws_a", "alice", request).status, 403)

    def test_guard_non_consuming_breaks_replay_probe(self):
        real = TerminalTicketPolicy(clock=Clock(), ttl_s=60.0)
        ticket_id = real.issue("alice", "ws_a", "sbx-a", 1)
        binding = ("alice", "ws_a", "sbx-a", 1)
        self.assertTrue(real.redeem(ticket_id, *binding)[0])
        self.assertFalse(real.redeem(ticket_id, *binding)[0])  # single-use
        broken = NonConsumingTickets(clock=Clock(), ttl_s=60.0)
        ticket_id = broken.issue("alice", "ws_a", "sbx-a", 1)
        broken.redeem(ticket_id, *binding)
        self.assertTrue(broken.redeem(ticket_id, *binding)[0])  # replays

    def test_guard_no_ttl_breaks_expiry_probe(self):
        clock = Clock()
        real = TerminalTicketPolicy(clock=clock, ttl_s=60.0)
        ticket_id = real.issue("alice", "ws_a", "sbx-a", 1)
        clock.advance(120.0)
        self.assertEqual(real.redeem(ticket_id, "alice", "ws_a", "sbx-a", 1),
                         (False, "expired"))
        broken = NoTTLTickets(clock=Clock(), ttl_s=60.0)
        ticket_id = broken.issue("alice", "ws_a", "sbx-a", 1)
        broken._clock.advance(120.0)
        self.assertTrue(broken.redeem(ticket_id, "alice", "ws_a", "sbx-a", 1)[0])
    def test_guard_unbound_breaks_generation_probe(self):
        clock = Clock()
        real = TerminalTicketPolicy(clock=clock, ttl_s=60.0)
        ticket_id = real.issue("alice", "ws_a", "sbx-a", generation=1)
        ok, code = real.redeem(ticket_id, "bob", "ws_b", "sbx-b", 2)
        self.assertEqual((ok, code), (False, "binding_mismatch"))
        broken = UnboundTickets(clock=Clock(), ttl_s=60.0)
        ticket_id = broken.issue("alice", "ws_a", "sbx-a", generation=1)
        self.assertTrue(broken.redeem(ticket_id, "bob", "ws_b", "sbx-b", 2)[0])

    def test_guard_clear_store_breaks_no_clear_id_probe(self):
        real = TerminalTicketPolicy(clock=Clock())
        ticket_id = real.issue("alice", "ws_a", "sbx-a", 1)
        self.assertNotIn(ticket_id, json.dumps(real._tickets))
        broken = ClearTicketStore(clock=Clock())
        ticket_id = broken.issue("alice", "ws_a", "sbx-a", 1)
        self.assertIn(ticket_id, json.dumps(broken._tickets))

    def test_guard_open_registration_breaks_invite_probe(self):
        real = InviteList()
        self.assertFalse(real.is_invited("stranger@example.com"))
        broken = OpenRegistration()
        self.assertTrue(broken.is_invited("stranger@example.com"))

    def test_guard_plain_registry_breaks_hashed_storage_probe(self):
        token = new_token()
        real = TokenRegistry()
        real.register(token, "ws_a")
        self.assertNotIn(token, repr(real.__dict__))
        broken = PlainTokenRegistry()
        broken.register(token, "ws_a")
        self.assertIn(token, json.dumps(broken._by_hash))


if __name__ == "__main__":
    unittest.main()
