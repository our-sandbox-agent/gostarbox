"""Tests for the #21 BYOK credential policy rules (scripts/byok_policy.py).

Coverage per the issue acceptance (in-repo G03 sub-scope slice; the real
proxy runtime is blocked, so all runtime facts enter as pure policy):
  - secret store: GET/list metadata ONLY (grep-style value-absence
    assertions), enveloped at-rest form (no plaintext field name), audited
    use() accessor, digest-prefix logs (redact idiom)
  - missing key: Suspend resume -> 409 credentials_required (stays
    Suspend, no operation); create -> agent start refused (ADR §6)
  - injection: generation mismatch reject, double-key reject without
    rotate, rotate atomic (same ts, single active), tmpfs/0600/runtime-uid
    descriptor, cleared on stop, superseded by new generation
  - revocation: immediate teardown actions per sandbox+generation, pending
    rejected, NEVER auto-kill
  - upstream battery: https api.anthropic.com/v1/... allow; http, other
    host/port, other path, redirect, userinfo, query all reject
  - backup exclusion contains the secret store namespace; trust_report is
    honest (full proxy NOT claimed, sandbox readability disclosed)
  - mutation guards (CONTRIBUTING mutate-and-fail idiom): each guard
    breaks one safeguard at a deliberate point and asserts the invariant
    flips on the mutant and holds on the real rules.
"""
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from byok_policy import (CREDENTIALS_REQUIRED, CREDENTIAL_DIR,  # noqa: E402
                         INJECTION_MODE, INJECTION_OWNER,
                         NO_KEY_REFUSE_START, SECRET_STORE_NAMESPACE,
                         InjectionPlan, SecretStore, Verdict,
                         backup_plan, require_credential, redact,
                         trust_report, upstream_rule)

KEY = "sk-ant-api03-EXAMPLEONLYNOTREAL0123456789"
KEY2 = "sk-ant-api03-SECONDEXAMPLEONLYNOTREAL45678"


def clock_seq(start=1000.0, step=1.0):
    box = [start]

    def tick():
        box[0] += step
        return box[0]

    return tick


def store_with_keys(clock=None):
    store = SecretStore(clock=clock or clock_seq())
    kid = store.put("anthropic_api_key", KEY)
    kid2 = store.put("anthropic_api_key_new", KEY2)
    return store, kid, kid2


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.store, self.kid, self.kid2 = store_with_keys()

    def test_put_derives_key_id_from_digest_prefix(self):
        prefix = hashlib.sha256(KEY.encode()).hexdigest()[:12]
        self.assertEqual(self.kid, "key_" + prefix)
        self.assertEqual(self.store.get(self.kid)["digest_prefix"], prefix)

    def test_get_returns_metadata_only_never_value(self):
        view = self.store.get(self.kid)
        self.assertEqual(
            set(view),
            {"key_id", "name", "created_at", "last_used_at",
             "digest_prefix", "revoked_at"})
        text = json.dumps(view)
        self.assertNotIn(KEY, text)  # grep-style: the value is not there
        self.assertIn(view["digest_prefix"], text)  # prefix is

    def test_list_is_metadata_only(self):
        text = json.dumps(self.store.list())
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY2, text)
        self.assertEqual(len(self.store.list()), 2)

    def test_use_returns_value_updates_last_used_and_audits(self):
        self.assertIsNone(self.store.get(self.kid)["last_used_at"])
        self.assertEqual(self.store.use(self.kid, purpose="proxy"), KEY)
        view = self.store.get(self.kid)
        self.assertEqual(view["last_used_at"],
                         self.store.audit[-1]["at"])
        self.assertEqual(self.store.audit[-1]["key_id"], self.kid)
        self.assertEqual(self.store.audit[-1]["purpose"], "proxy")

    def test_use_unknown_and_revoked_return_none(self):
        self.assertIsNone(self.store.use("key_nosuch"))
        self.store.revoke(self.kid)
        self.assertIsNone(self.store.use(self.kid))
        denied = [e for e in self.store.audit if e["event"] == "use_denied"]
        self.assertEqual([d["reason"] for d in denied],
                         ["unknown_key", "revoked"])

    def test_revoke_drops_plaintext_and_marks_metadata(self):
        self.store.revoke(self.kid)
        self.assertNotIn(self.kid, self.store._plaintext)  # memory cleared
        self.assertIsNotNone(self.store.get(self.kid)["revoked_at"])

    def test_re_put_same_value_re_registers_for_resume(self):
        self.store.revoke(self.kid)
        again = self.store.put("anthropic_api_key", KEY)
        self.assertEqual(again, self.kid)  # same digest -> same id
        self.assertIsNone(self.store.get(self.kid)["revoked_at"])
        self.assertEqual(self.store.use(self.kid), KEY)

    def test_at_rest_is_enveloped_only(self):
        persisted = self.store.at_rest()
        text = json.dumps(persisted)
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY2, text)
        for record in persisted.values():
            self.assertNotIn("plaintext", record)
            self.assertNotIn("value", record)
            self.assertIn("digest", record)
            self.assertIn("sealed_blob", record)

    def test_audit_log_never_contains_the_value(self):
        self.store.use(self.kid)
        self.store.revoke(self.kid2)
        text = json.dumps(self.store.audit)
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY2, text)
        self.assertIn(redact(hashlib.sha256(KEY.encode()).hexdigest()),
                      text)  # keys identified by digest prefix


class RequireCredentialTests(unittest.TestCase):
    def test_resume_without_key_is_409_credentials_required(self):
        verdict = require_credential({"action": "resume",
                                      "observed_state": "Suspend",
                                      "has_credential": False})
        self.assertEqual(verdict, Verdict(False, "credentials_required"))
        # semantics delegated to the contracts, not re-invented
        self.assertEqual(CREDENTIALS_REQUIRED["http_status"], 409)
        self.assertEqual(CREDENTIALS_REQUIRED["code"],
                         "credentials_required")
        self.assertFalse(CREDENTIALS_REQUIRED["creates_operation"])
        self.assertEqual(CREDENTIALS_REQUIRED["stays"], "Suspend")

    def test_resume_with_key_ok(self):
        self.assertEqual(
            require_credential({"action": "resume",
                                "observed_state": "Suspend",
                                "has_credential": True}),
            Verdict(True, "ok"))

    def test_create_without_key_refuses_agent_start(self):
        verdict = require_credential({"action": "create",
                                      "has_credential": False})
        self.assertEqual(verdict, Verdict(False, "no_key_refuse_start"))
        self.assertIsNone(NO_KEY_REFUSE_START["http_status"])  # not a 409
        self.assertIn("not started", NO_KEY_REFUSE_START["behavior"])

    def test_missing_action_defaults_conservative(self):
        self.assertEqual(require_credential({"observed_state": "Suspend"}),
                         Verdict(False, "credentials_required"))


class InjectionTests(unittest.TestCase):
    def setUp(self):
        self.store, self.kid, self.kid2 = store_with_keys()
        self.store.set_sandbox_generation("sbx-1", 7)

    def plan(self, key_id=None, generation=7, sandbox="sbx-1", rotate=False):
        return self.store.plan_injection(sandbox, generation,
                                         key_id or self.kid,
                                         rotate=rotate)

    def test_descriptor_is_tmpfs_0600_runtime_uid_instance_lifetime(self):
        plan = self.plan()
        self.assertTrue(plan.ok)
        d = plan.injection["descriptor"]
        self.assertEqual(d["path"],
                         f"{CREDENTIAL_DIR}/anthropic_api_key")
        self.assertEqual(d["filesystem"], "tmpfs")
        self.assertEqual(d["mode"], INJECTION_MODE)
        self.assertEqual(d["mode"], "0o600")
        self.assertEqual(d["owner"], INJECTION_OWNER)
        self.assertEqual(d["lifetime"], "instance")
        self.assertEqual(d["cleared_on"], "stop")
        self.assertEqual(plan.injection["status"], "pending")

    def test_generation_mismatch_rejected(self):
        self.assertEqual(self.plan(generation=6).reason,
                         "generation_mismatch")  # stale generation
        self.assertEqual(self.plan(sandbox="sbx-unbound").reason,
                         "generation_mismatch")  # never registered

    def test_unknown_and_revoked_key_rejected(self):
        self.assertEqual(self.plan(key_id="key_nosuch").reason, "unknown_key")
        self.store.revoke(self.kid)
        self.assertEqual(self.plan().reason, "key_revoked")

    def test_second_key_rejected_without_explicit_rotate(self):
        self.assertTrue(self.plan().ok)
        conflict = self.plan(key_id=self.kid2)
        self.assertEqual(conflict.reason, "key_conflict")
        again = self.plan()  # same key twice is not idempotent either
        self.assertEqual(again.reason, "already_injected")

    def test_confirm_activates_and_lists_per_sandbox_generation(self):
        self.plan()
        self.assertEqual(self.store.confirm_injection("sbx-1", 7),
                         Verdict(True, "ok"))
        active = self.store.active_injections()
        self.assertEqual(len(active), 1)
        self.assertEqual((active[0]["sandbox_id"], active[0]["generation"],
                          active[0]["key_id"]), ("sbx-1", 7, self.kid))

    def test_confirm_rejected_after_key_revoked(self):
        self.plan()
        result = self.store.revoke(self.kid)  # revoke sweeps the pending plan
        self.assertEqual(result["teardown"][0]["action"],
                         "reject_pending_injection")
        self.assertEqual(self.store.confirm_injection("sbx-1", 7),
                         Verdict(False, "no_pending_injection"))
        self.assertEqual(self.store.active_injections(), [])

    def test_rotate_is_atomic_same_ts_single_active(self):
        self.plan()
        self.store.confirm_injection("sbx-1", 7)
        rotated = self.plan(key_id=self.kid2, rotate=True)
        self.assertTrue(rotated.ok)
        old = self.store._injection_history[-1]
        self.assertEqual(old["revoked_at"],
                         rotated.injection["planned_at"])  # same instant
        self.assertEqual(old["revoked_reason"], "rotate")
        self.assertEqual(len(self.store.active_injections()), 0)
        self.store.confirm_injection("sbx-1", 7)  # runtime confirms the swap
        active = self.store.active_injections()
        self.assertEqual(len(active), 1)  # never two live injections
        self.assertEqual(active[0]["key_id"], self.kid2)

    def test_new_generation_supersedes_injection(self):
        self.plan()
        self.store.set_sandbox_generation("sbx-1", 8)  # resume: new instance
        self.assertEqual(self.store.active_injections(), [])
        self.assertEqual(self.plan(generation=7).reason,
                         "generation_mismatch")

    def test_clear_on_stop_marks_revoked(self):
        self.plan()
        self.store.confirm_injection("sbx-1", 7)
        paths = self.store.clear("sbx-1")
        self.assertEqual(paths, [f"{CREDENTIAL_DIR}/anthropic_api_key"])
        self.assertEqual(self.store.active_injections(), [])
        self.assertEqual(self.store._injection_history[-1]
                         ["revoked_reason"], "stopped")


class RevocationTests(unittest.TestCase):
    def setUp(self):
        self.store, self.kid, self.kid2 = store_with_keys()
        self.store.set_sandbox_generation("sbx-1", 7)
        self.store.set_sandbox_generation("sbx-2", 3)
        self.store.plan_injection("sbx-1", 7, self.kid)
        self.store.confirm_injection("sbx-1", 7)

    def test_revoke_returns_teardown_actions_and_never_auto_kills(self):
        self.store.plan_injection("sbx-2", 3, self.kid)  # still pending
        result = self.store.revoke(self.kid)
        self.assertTrue(result["revoked"])
        self.assertFalse(result["kill_sandbox"])
        actions = {(a["action"], a["sandbox_id"], a["generation"])
                   for a in result["teardown"]}
        self.assertEqual(actions, {
            ("teardown_injection", "sbx-1", 7),
            ("reject_pending_injection", "sbx-2", 3),
        })
        for action in result["teardown"]:
            self.assertNotIn("kill", action["action"])
            self.assertIn("path", action)
        self.assertEqual(self.store.active_injections(), [])

    def test_pending_plans_for_a_revoked_key_are_rejected(self):
        self.store.revoke(self.kid)
        self.store.set_sandbox_generation("sbx-3", 1)
        plan = self.store.plan_injection("sbx-3", 1, self.kid)
        self.assertEqual(plan, InjectionPlan(False, None, "key_revoked"))

    def test_revoke_unknown_key(self):
        result = self.store.revoke("key_nosuch")
        self.assertFalse(result["revoked"])
        self.assertEqual(result["reason"], "unknown_key")


class UpstreamRuleTests(unittest.TestCase):
    def test_allow_battery(self):
        for url in ("https://api.anthropic.com/v1/messages",
                    "https://api.anthropic.com/v1/messages/count_tokens",
                    "https://API.Anthropic.Com/v1/x"):  # case-insensitive
            with self.subTest(url=url):
                self.assertEqual(upstream_rule(url), Verdict(True, "allow"))

    def test_reject_battery(self):
        for url, reason in [
            ("http://api.anthropic.com/v1/messages", "scheme_not_https"),
            ("https://evil.example.com/v1/messages", "host_not_allowed"),
            ("https://api.anthropic.com.evil.com/v1/x", "host_not_allowed"),
            ("https://api.anthropic.com:8443/v1/x", "host_not_allowed"),
            ("https://api.anthropic.com/api/x", "path_not_allowed"),
            ("https://api.anthropic.com/", "path_not_allowed"),
            ("https://api.anthropic.com/v1/x?api_key=" + KEY,
             "query_not_allowed"),
            ("https://api.anthropic.com/v1/x?beta=true",
             "query_not_allowed"),  # any query: no smuggling channel
            ("https://key:secret@api.anthropic.com/v1/x",
             "userinfo_present"),
            ("https://api.anthropic.com:notaport/v1/x", "invalid_url"),
            ("not a url", "scheme_not_https"),  # urlsplit: no scheme
        ]:
            with self.subTest(url=url):
                self.assertEqual(upstream_rule(url),
                                 Verdict(False, reason))

    def test_redirects_are_never_followed(self):
        for url in ("https://api.anthropic.com/v1/x",  # even same-host
                    "https://evil.example.com/steal",
                    "http://api.anthropic.com/v1/x"):
            with self.subTest(url=url):
                self.assertEqual(
                    upstream_rule(url, is_redirect=True),
                    Verdict(False, "redirect_not_followed"))


class BoundaryTests(unittest.TestCase):
    def test_backup_plan_excludes_secret_namespace_and_tmpfs(self):
        plan = backup_plan()
        self.assertIn(SECRET_STORE_NAMESPACE, plan["excluded"])
        self.assertIn(CREDENTIAL_DIR + "/", plan["excluded"])
        self.assertTrue(any("plaintext" in rule for rule in plan["rules"]))

    def test_trust_report_is_honest_about_scope(self):
        report = trust_report()
        self.assertFalse(report["final_goal_full_proxy"]["claimed"])
        self.assertTrue(report["key_readable_by_sandbox_processes"])
        for place in ("workspace_volume", "home_volume", "backups", "logs"):
            self.assertIn(place, report["never_persisted_in"])
        self.assertIn("trusted proxy memory", report["key_held_in"])


# ---------------------------------------------------------- mutation guards
# Each mutant breaks one safeguard of the policy at a deliberate mutation
# point; each guard proves the invariant flips on the mutant and holds on
# the real rules (non-vacuity, CONTRIBUTING mutate-and-fail idiom).

class LeakyGetStore(SecretStore):
    def get(self, key_id):
        view = super().get(key_id)
        if view is not None:
            view["value"] = self._plaintext.get(key_id)  # the leak
        return view


class LeakyAuditStore(SecretStore):
    def use(self, key_id, purpose="proxy"):
        value = super().use(key_id, purpose)
        if value is not None:
            self.audit.append({"event": "use", "key_id": key_id,
                               "value": value})  # the leak
        return value


class PlaintextAtRestStore(SecretStore):
    def at_rest(self):
        persisted = super().at_rest()
        for key_id, value in self._plaintext.items():
            persisted[key_id]["plaintext"] = value  # the leak
        return persisted


def permissive_upstream(url, is_redirect=False):
    return Verdict(True, "allow")  # every egress guard removed


class NoGenerationCheckStore(SecretStore):
    def plan_injection(self, sandbox_id, generation, key_id, rotate=False):
        # the bug: stamp the plan with the CURRENT generation, erasing the
        # stale-generation fence
        return super().plan_injection(
            sandbox_id, self._generations.get(sandbox_id, generation),
            key_id, rotate=rotate)


class AutoKillRevokeStore(SecretStore):
    def revoke(self, key_id):
        result = super().revoke(key_id)
        result["teardown"].append({  # the bug: silent auto-kill
            "action": "kill_sandbox_processes", "sandbox_id": "sbx-1",
            "immediate": True})
        return result


class AlwaysRotateStore(SecretStore):
    def plan_injection(self, sandbox_id, generation, key_id, rotate=False):
        # the bug: any second key silently swaps in, explicit rotate removed
        return super().plan_injection(sandbox_id, generation, key_id,
                                      rotate=True)


class MutationGuards(unittest.TestCase):
    def setUp(self):
        self.store, self.kid, self.kid2 = store_with_keys()

    def test_guard_leaky_get_breaks_metadata_only(self):
        self.assertNotIn(KEY, json.dumps(self.store.get(self.kid)))
        leaky = LeakyGetStore()
        kid = leaky.put("anthropic_api_key", KEY)
        self.assertIn(KEY, json.dumps(leaky.get(kid)))  # value in the view

    def test_guard_leaky_audit_breaks_log_redaction(self):
        self.store.use(self.kid)
        self.assertNotIn(KEY, json.dumps(self.store.audit))
        leaky = LeakyAuditStore()
        kid = leaky.put("anthropic_api_key", KEY)
        leaky.use(kid)
        self.assertIn(KEY, json.dumps(leaky.audit))  # the value is logged

    def test_guard_plaintext_at_rest_breaks_envelope(self):
        self.assertNotIn(KEY, json.dumps(self.store.at_rest()))
        leaky = PlaintextAtRestStore()
        leaky.put("anthropic_api_key", KEY)
        persisted = leaky.at_rest()
        self.assertIn(KEY, json.dumps(persisted))  # plaintext at rest
        self.assertIn("plaintext", list(persisted.values())[0])

    def test_guard_permissive_upstream_breaks_egress(self):
        bad = ["http://api.anthropic.com/v1/x",
               "https://evil.example.com/v1/x",
               "https://api.anthropic.com/other",
               "https://api.anthropic.com/v1/x?api_key=" + KEY,
               "https://u:p@api.anthropic.com/v1/x"]
        for url in bad:
            self.assertFalse(upstream_rule(url).allowed)  # real rejects
        for url in bad:
            self.assertTrue(permissive_upstream(url).allowed)  # mutant allows

    def test_guard_no_generation_check_breaks_fencing(self):
        self.store.set_sandbox_generation("sbx-1", 7)
        self.assertEqual(
            self.store.plan_injection("sbx-1", 6, self.kid).reason,
            "generation_mismatch")
        broken = NoGenerationCheckStore()
        kid = broken.put("anthropic_api_key", KEY)
        broken.set_sandbox_generation("sbx-1", 7)
        # the stale-generation plan walks straight through the mutant
        self.assertTrue(broken.plan_injection("sbx-1", 6, kid).ok)

    def test_guard_auto_kill_revoke_breaks_no_kill(self):
        self.store.set_sandbox_generation("sbx-1", 7)
        self.store.plan_injection("sbx-1", 7, self.kid)
        self.store.confirm_injection("sbx-1", 7)
        result = self.store.revoke(self.kid)
        self.assertFalse(result["kill_sandbox"])
        self.assertFalse(any("kill" in a["action"]
                             for a in result["teardown"]))
        killer = AutoKillRevokeStore()
        killer.put("anthropic_api_key", KEY)
        kid = list(killer._records)[0]
        killer.set_sandbox_generation("sbx-1", 7)
        killer.plan_injection("sbx-1", 7, kid)
        killer.confirm_injection("sbx-1", 7)
        broken = killer.revoke(kid)
        self.assertTrue(any("kill" in a["action"]
                            for a in broken["teardown"]))  # auto-kill

    def test_guard_always_rotate_breaks_explicit_rotate(self):
        self.store.set_sandbox_generation("sbx-1", 7)
        self.store.plan_injection("sbx-1", 7, self.kid)
        self.assertEqual(
            self.store.plan_injection("sbx-1", 7, self.kid2).reason,
            "key_conflict")
        broken = AlwaysRotateStore()
        k1 = broken.put("anthropic_api_key", KEY)
        k2 = broken.put("anthropic_api_key_new", KEY2)
        broken.set_sandbox_generation("sbx-1", 7)
        broken.plan_injection("sbx-1", 7, k1)
        # the mutant silently swaps the key with no rotate request
        self.assertTrue(broken.plan_injection("sbx-1", 7, k2).ok)
        broken.confirm_injection("sbx-1", 7)
        self.assertEqual(broken.active_injections()[0]["key_id"], k2)


if __name__ == "__main__":
    unittest.main()
