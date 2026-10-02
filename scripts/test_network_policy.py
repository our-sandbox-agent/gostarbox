"""Tests for the #22 network egress decision engine (scripts/network_policy.py).

Coverage per the issue acceptance (in-repo G04 sub-scope slice, Refs
#78; real iptables/bridge runtime is blocked, so all runtime facts
enter as pure policy decisions):
  - fixed deny battery, IPv4 + IPv6: loopback (127/8, ::1), private
    (RFC1918 + ULA fc00::/7), link-local (169.254/16, fe80::/10) and
    metadata 169.254.169.254; config control-plane and cross-sandbox
    CIDRs; deny classification wins over any allowlist
  - default deny: unknown public host denied; package-compat hosts
    (pip/npm/apt/Claude) allowed on 443 ONLY, denied on 80
  - DNS rebinding: pre-resolution answer clean, post-resolution answer
    evil -> deny; ALL resolved IPs checked (1 good + 1 bad -> deny)
  - CONNECT semantics: destination resolved then checked; redirects
    never followed (even allowlisted / same-host); Claude upstream URL
    rules delegated to byok_policy.upstream_rule (reused, not forked)
    with the IP-level deny check still applied
  - tenant allowlist cannot whitelist the metadata IP or a
    control-plane CIDR (deny-free-space intersection only)
  - management ports denied on ANY host, allowlisted public hosts
    included; hook endpoints default deny, allow only when configured
  - revocation: close actions per tunnel with explicit lease deadline,
    volumes kept, nothing destroyed; flush failure isolates
    (block_and_isolate, watchdog semantics)
  - log parity: allow and deny records share the exact (ts, host,
    ips, port, verdict, reason) shape
  - mutation guards (CONTRIBUTING mutate-and-fail idiom): each guard
    breaks one safeguard at a deliberate mutation point and asserts the
    invariant flips on the mutant and holds on the real rules.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from byok_policy import UPSTREAM_HOST  # noqa: E402
from network_policy import (MANAGEMENT_PORTS,  # noqa: E402
                            PACKAGE_COMPAT_ALLOWLIST, Decision,
                            EgressPolicy)

PUB = "93.184.216.34"        # example.com — public, doc-safe
PUB6 = "2606:2800:220:1:248:1893:25c8:1946"


def clock_seq(start=1000.0, step=1.0):
    box = [start]

    def tick():
        box[0] += step
        return box[0]

    return tick


def res(*ips):
    return lambda host: list(ips)


def rebind_resolver(first, second):
    """Stateful resolver: answers the first call with `first`, every
    later call with `second` (the DNS-rebinding shape)."""
    answers = [list(first), list(second)]

    def resolve(host):
        return list(answers.pop(0)) if len(answers) > 1 else list(answers[0])

    return resolve


def policy(**over):
    """Standard test policy: TEST-NET control-plane + cross-sandbox
    CIDRs (public ranges, so only the CONFIG deny catches them), one
    tenant-allowlisted public host."""
    cfg = {
        "sandbox_id": "sbx-1",
        "control_plane_cidrs": ["203.0.113.0/24"],
        "cross_sandbox_cidrs": ["198.51.100.0/24"],
        "tenant_allowlist": {"example.com": {443}},
    }
    cfg.update(over)
    return EgressPolicy(cfg, clock=clock_seq())


class FixedDenyBatteryTests(unittest.TestCase):
    def test_loopback_v4_v6_denied(self):
        for host in ("127.0.0.1", "127.200.1.1", "::1"):
            with self.subTest(host=host):
                decision = policy().decide(host, 443, res(PUB))
                self.assertEqual(
                    decision, Decision(False, "loopback_denied"))

    def test_private_rfc1918_denied(self):
        for host in ("10.1.2.3", "172.16.0.1", "172.31.255.255",
                     "192.168.0.1"):
            with self.subTest(host=host):
                self.assertEqual(policy().decide(host, 443, res(PUB)).reason,
                                 "private_denied")

    def test_private_boundary_is_real_cidr_math(self):
        # 172.15/172.32 sit OUTSIDE 172.16.0.0/12: not private, so the
        # verdict is the default deny, not private_denied
        for host in ("172.15.255.255", "172.32.0.1", "11.0.0.1",
                     "193.168.0.1"):
            with self.subTest(host=host):
                self.assertEqual(policy().decide(host, 443, res(PUB)).reason,
                                 "not_allowlisted")

    def test_ula_fc00_slash_7_denied(self):
        for host in ("fc00::1", "fcff::1", "fd00::1", "fdab::1"):
            with self.subTest(host=host):
                self.assertEqual(policy().decide(host, 443, res(PUB)).reason,
                                 "private_denied")

    def test_link_local_v4_v6_denied(self):
        for host in ("169.254.0.1", "169.254.170.1", "fe80::1",
                     "febf::1"):
            with self.subTest(host=host):
                self.assertEqual(
                    policy().decide(host, 443, res(PUB)).reason,
                    "link_local_denied")

    def test_metadata_ip_denied_and_named(self):
        self.assertEqual(
            policy().decide("169.254.169.254", 443, res(PUB)),
            Decision(False, "metadata_denied"))


class ConfigDenyTests(unittest.TestCase):
    def test_control_plane_cidrs_denied(self):
        for host in ("203.0.113.1", "203.0.113.254"):
            with self.subTest(host=host):
                self.assertEqual(
                    policy().decide(host, 443, res(PUB)).reason,
                    "control_plane_denied")

    def test_control_plane_v6_cidr_denied(self):
        pol = policy(control_plane_cidrs=["2001:db8:cafe::/48"])
        self.assertEqual(
            pol.decide("2001:db8:cafe::1", 443, res(PUB)).reason,
            "control_plane_denied")

    def test_cross_sandbox_cidrs_denied(self):
        pol = policy(cross_sandbox_cidrs=["198.51.100.0/24",
                                          "2001:db8:cafe::/48"])
        self.assertEqual(
            pol.decide("198.51.100.7", 443, res(PUB)).reason,
            "cross_sandbox_denied")
        self.assertEqual(
            pol.decide("2001:db8:cafe::1", 443, res(PUB)).reason,
            "cross_sandbox_denied")

    def test_deny_classification_precedes_allowlist(self):
        # the deny reason (not not_allowlisted) proves the fixed deny
        # set runs BEFORE any allowlist lookup on an unallowlisted host
        self.assertEqual(
            policy().decide("10.0.0.1", 443, res(PUB)).reason,
            "private_denied")


class AllowAndDefaultDenyTests(unittest.TestCase):
    def test_public_allowlisted_allow_v4_v6(self):
        pol = policy()
        self.assertEqual(pol.decide("example.com", 443, res(PUB)),
                         Decision(True, "allow"))
        self.assertEqual(
            pol.decide("example.com", 443, res(PUB6)),
            Decision(True, "allow"))

    def test_unknown_public_host_default_deny(self):
        self.assertEqual(
            policy().decide("arbitrary.example", 443, res(PUB)),
            Decision(False, "not_allowlisted"))

    def test_package_compat_battery_443_allow_80_deny(self):
        hosts = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org",
                 "deb.debian.org", "security.debian.org",
                 "api.anthropic.com"]
        for host in hosts:
            with self.subTest(host=host):
                pol = policy()
                self.assertEqual(pol.decide(host, 443, res(PUB)),
                                 Decision(True, "allow"))
                self.assertEqual(pol.decide(host, 80, res(PUB)).reason,
                                 "not_allowlisted")

    def test_claude_host_reused_from_byok_policy(self):
        # the upstream host constant is IMPORTED from byok_policy —
        # reuse, not a fork
        self.assertEqual(UPSTREAM_HOST, "api.anthropic.com")
        self.assertIn(UPSTREAM_HOST, PACKAGE_COMPAT_ALLOWLIST)
        self.assertEqual(PACKAGE_COMPAT_ALLOWLIST[UPSTREAM_HOST],
                         frozenset({443}))

    def test_unresolved_host_denied(self):
        self.assertEqual(
            policy().decide("example.com", 443, lambda h: []).reason,
            "unresolved")
        self.assertEqual(
            policy().decide("example.com", 443, None).reason, "unresolved")


class ManagementPortTests(unittest.TestCase):
    def test_management_ports_denied_on_public_allowlisted_host(self):
        pol = policy()  # example.com tenant-allowlisted on 443
        for port in (22, 2375, 2376, 2379, 6443, 10250):
            with self.subTest(port=port):
                self.assertEqual(
                    pol.decide("example.com", port, res(PUB)).reason,
                    "management_port_denied")

    def test_management_port_set_is_configurable(self):
        pol = policy(management_ports={8443},
                     tenant_allowlist={"example.com": {443, 22}})
        self.assertEqual(
            pol.decide("example.com", 8443, res(PUB)).reason,
            "management_port_denied")
        self.assertEqual(pol.decide("example.com", 22, res(PUB)),
                         Decision(True, "allow"))  # 22 no longer mgmt

    def test_default_management_port_set(self):
        self.assertIn(22, MANAGEMENT_PORTS)
        self.assertIn(6443, MANAGEMENT_PORTS)
        self.assertNotIn(443, MANAGEMENT_PORTS)
        self.assertNotIn(80, MANAGEMENT_PORTS)


class ResolverTests(unittest.TestCase):
    def test_all_resolved_ips_checked_good_plus_bad_denies(self):
        self.assertEqual(
            policy().decide("example.com", 443, res(PUB, "10.0.0.5")),
            Decision(False, "private_denied"))

    def test_dns_rebinding_post_answer_evil_denies(self):
        # pre-resolution answer clean, post-resolution answer evil
        resolver = rebind_resolver([PUB], ["169.254.169.254"])
        self.assertEqual(
            policy().decide_after_dns("example.com", 443, resolver),
            Decision(False, "metadata_denied"))

    def test_dns_rebinding_pre_answer_evil_denies(self):
        resolver = rebind_resolver(["192.168.1.1"], [PUB])
        self.assertEqual(
            policy().decide_after_dns("example.com", 443, resolver).reason,
            "private_denied")

    def test_decide_after_dns_both_answers_good_allows(self):
        resolver = rebind_resolver([PUB], [PUB6])
        self.assertEqual(
            policy().decide_after_dns("example.com", 443, resolver),
            Decision(True, "allow"))

    def test_decide_after_dns_logs_the_union(self):
        resolver = rebind_resolver([PUB], ["169.254.169.254"])
        pol = policy()
        pol.decide_after_dns("example.com", 443, resolver)
        self.assertEqual(pol.last_log()["ips"], [PUB, "169.254.169.254"])

    def test_plain_decide_does_not_double_resolve(self):
        calls = []

        def resolver(host):
            calls.append(host)
            return [PUB]

        policy().decide("example.com", 443, resolver)
        self.assertEqual(calls, ["example.com"])  # decide resolves once


class ConnectRuleTests(unittest.TestCase):
    def test_allowlisted_https_connect_allowed(self):
        pol = policy()
        self.assertEqual(
            pol.connect_rule("https://example.com/v1/x", res(PUB)),
            Decision(True, "allow"))
        self.assertEqual(pol.last_log()["port"], 443)  # default port

    def test_redirect_never_followed(self):
        pol = policy()
        for url in ("https://example.com/steal",     # allowlisted host
                    "https://evil.example.com/steal",
                    "https://api.anthropic.com/v1/x"):  # even same-host
            with self.subTest(url=url):
                self.assertEqual(
                    pol.connect_rule(url, res(PUB), is_redirect=True),
                    Decision(False, "redirect_not_followed"))

    def test_non_https_scheme_denied(self):
        self.assertEqual(
            policy().connect_rule("http://pypi.org/simple/", res(PUB)).reason,
            "scheme_not_https")

    def test_userinfo_denied(self):
        self.assertEqual(
            policy().connect_rule("https://u:p@example.com/x",
                                  res(PUB)).reason,
            "userinfo_present")

    def test_claude_upstream_url_rules_are_delegated(self):
        pol = policy()
        self.assertEqual(
            pol.connect_rule("https://api.anthropic.com/v1/messages",
                             res(PUB)),
            Decision(True, "allow"))
        # path rules come from byok upstream_rule, reused not forked
        self.assertEqual(
            pol.connect_rule("https://api.anthropic.com/other",
                             res(PUB)).reason,
            "path_not_allowed")
        self.assertEqual(
            pol.connect_rule("https://api.anthropic.com/v1/x?api_key=k",
                             res(PUB)).reason,
            "query_not_allowed")

    def test_claude_upstream_ip_check_still_applies(self):
        # delegated URL rules do not bypass the fixed deny set: a
        # rebound api.anthropic.com answer to metadata is denied
        self.assertEqual(
            policy().connect_rule("https://api.anthropic.com/v1/messages",
                                  res("169.254.169.254")).reason,
            "metadata_denied")

    def test_bad_port_literal_invalid(self):
        self.assertEqual(
            policy().connect_rule("https://example.com:notaport/",
                                  res(PUB)).reason,
            "invalid_url")


class HookEndpointTests(unittest.TestCase):
    def test_hook_endpoints_default_deny(self):
        self.assertEqual(
            policy().decide("console.example.com", 443, res(PUB)).reason,
            "not_allowlisted")

    def test_configured_hook_endpoint_allowed_port_only(self):
        pol = policy(hook_endpoints=[("console.example.com", 443)])
        self.assertEqual(
            pol.decide("console.example.com", 443, res(PUB)),
            Decision(True, "allow"))
        self.assertEqual(
            pol.decide("console.example.com", 8443, res(PUB)).reason,
            "not_allowlisted")


class TenantAllowlistTrustTests(unittest.TestCase):
    def test_tenant_cannot_whitelist_metadata_ip(self):
        pol = policy(tenant_allowlist={"169.254.169.254": {443}})
        self.assertEqual(
            pol.decide("169.254.169.254", 443, res("169.254.169.254")),
            Decision(False, "metadata_denied"))

    def test_tenant_cannot_whitelist_control_plane(self):
        pol = policy(tenant_allowlist={"cp.example": {443},
                                       "203.0.113.5": {443}})
        # by name (resolves into the control-plane CIDR) and by literal
        self.assertEqual(
            pol.decide("cp.example", 443, res("203.0.113.9")).reason,
            "control_plane_denied")
        self.assertEqual(
            pol.decide("203.0.113.5", 443, res("203.0.113.5")).reason,
            "control_plane_denied")

    def test_tenant_cannot_whitelist_loopback_or_cross_sandbox(self):
        pol = policy(tenant_allowlist={"127.0.0.1": {443}, "198.51.100.9":
                                       {443}})
        self.assertEqual(pol.decide("127.0.0.1", 443, res("127.0.0.1")).reason,
                         "loopback_denied")
        self.assertEqual(
            pol.decide("198.51.100.9", 443, res("198.51.100.9")).reason,
            "cross_sandbox_denied")


class LogParityTests(unittest.TestCase):
    def test_allow_and_deny_records_share_shape(self):
        pol = policy()
        pol.decide("example.com", 443, res(PUB))            # allow
        allow = pol.last_log()
        pol.decide("internal.example", 443, res("10.0.0.9"))  # deny
        deny = pol.last_log()
        for record, verdict, ips in ((allow, "allow", [PUB]),
                                     (deny, "deny", ["10.0.0.9"])):
            with self.subTest(verdict=verdict):
                self.assertEqual(
                    set(record),
                    {"ts", "host", "ips", "port", "verdict", "reason"})
                self.assertEqual(record["verdict"], verdict)
                self.assertEqual(record["ips"], ips)
                self.assertIsInstance(record["ts"], float)

    def test_every_decision_logs(self):
        pol = policy()
        self.assertIsNone(pol.last_log())
        pol.decide("example.com", 443, res(PUB))
        pol.decide("arbitrary.example", 443, res(PUB))
        pol.connect_rule("http://x.example/", res(PUB))
        self.assertEqual([r["verdict"] for r in pol.logs()],
                         ["allow", "deny", "deny"])


class RevocationTests(unittest.TestCase):
    def setUp(self):
        self.pol = policy()
        self.pol.decide("example.com", 443, res(PUB))
        self.pol.decide("pypi.org", 443, res(PUB))
        self.assertEqual(len(self.pol.open_tunnels()), 2)

    def test_revoke_closes_tunnels_with_lease_deadline(self):
        result = self.pol.revoke("sbx-1")
        self.assertTrue(result["revoked"])
        self.assertEqual(self.pol.open_tunnels(), [])
        actions = result["close_actions"]
        self.assertEqual({a["action"] for a in actions},
                         {"close_tunnel"})
        self.assertEqual({a["host"] for a in actions},
                         {"example.com", "pypi.org"})
        for action in actions:
            self.assertIsNotNone(action["lease_deadline_at"])
            self.assertEqual(action["sandbox_id"], "sbx-1")

    def test_revoke_never_destroys(self):
        result = self.pol.revoke("sbx-1")
        self.assertFalse(result["destroy"])
        self.assertTrue(result["keep_volumes"])
        self.assertNotIn("kill", "".join(a["action"]
                                         for a in result["close_actions"]))

    def test_revoke_with_no_open_tunnels(self):
        self.pol.revoke("sbx-1")
        again = self.pol.revoke("sbx-1")
        self.assertFalse(again["revoked"])
        self.assertEqual(again["reason"], "no_open_tunnels")
        self.assertEqual(again["close_actions"], [])


class FlushFailedTests(unittest.TestCase):
    def test_flush_failure_isolates_not_destroys(self):
        pol = policy()
        result = pol.flush_failed(error="iptables lock", now=5000.0)
        self.assertEqual(result["action"], "block_and_isolate")
        self.assertFalse(result["destroy"])
        self.assertTrue(result["keep_volumes"])
        self.assertTrue(result["requires_human"])
        self.assertIn("network_flush_failed", result["alerts"])


# ---------------------------------------------------------- mutation guards
# Each mutant breaks one safeguard of the policy at a deliberate
# mutation point; each guard proves the invariant flips on the mutant
# and holds on the real rules (non-vacuity, CONTRIBUTING idiom).

class NoPrivateCheckPolicy(EgressPolicy):
    def _deny_reason(self, ip):
        if super()._deny_reason(ip) == "private_denied":
            return None  # the bug: RFC1918/ULA check removed
        return super()._deny_reason(ip)


class NoLinkLocalCheckPolicy(EgressPolicy):
    def _deny_reason(self, ip):
        reason = super()._deny_reason(ip)
        # the bug: link-local + metadata checks removed
        return None if reason in ("link_local_denied",
                                  "metadata_denied") else reason


class NoFixedDenyPolicy(EgressPolicy):
    def _deny_reason(self, ip):
        return None  # the bug: whole fixed/config deny set deleted


class FirstIpOnlyPolicy(EgressPolicy):
    def _any_deny(self, parsed_ips):
        return super()._any_deny(parsed_ips[:1])  # the bug: ips[0] only


class NoManagementPortPolicy(EgressPolicy):
    def _port_denied(self, port):
        return False  # the bug: management ports open on any host


class DefaultAllowPolicy(EgressPolicy):
    def _is_allowlisted(self, host, port):
        return True  # the bug: default deny removed


class RedirectFollowingPolicy(EgressPolicy):
    def connect_rule(self, url, resolver=None, is_redirect=False):
        return super().connect_rule(url, resolver,
                                    is_redirect=False)  # the bug


class DestroyOnFlushPolicy(EgressPolicy):
    def flush_failed(self, error=None, now=None):
        result = super().flush_failed(error, now)
        result["destroy"] = True       # the bug: destroy-with-delete
        result["keep_volumes"] = False
        return result


class MutationGuards(unittest.TestCase):
    def test_guard_no_private_check_breaks_rfc1918(self):
        real = policy()
        self.assertFalse(real.decide("10.0.0.1", 443, res(PUB)).allowed)
        broken = NoPrivateCheckPolicy(
            {"tenant_allowlist": {"10.0.0.1": {443}}}, clock=clock_seq())
        self.assertTrue(broken.decide("10.0.0.1", 443, res(PUB)).allowed)

    def test_guard_no_link_local_check_breaks_metadata(self):
        real = policy()
        self.assertFalse(real.decide("169.254.169.254", 443,
                                     res(PUB)).allowed)
        self.assertEqual(real.decide("fe80::1", 443, res(PUB)).reason,
                         "link_local_denied")
        broken = NoLinkLocalCheckPolicy(
            {"tenant_allowlist": {"169.254.169.254": {443},
                                  "fe80::1": {443}}}, clock=clock_seq())
        self.assertTrue(broken.decide("169.254.169.254", 443,
                                      res(PUB)).allowed)
        self.assertTrue(broken.decide("fe80::1", 443, res(PUB)).allowed)

    def test_guard_no_fixed_deny_breaks_tenant_trust_boundary(self):
        real = policy(tenant_allowlist={"169.254.169.254": {443},
                                        "203.0.113.5": {443}})
        self.assertFalse(real.decide("169.254.169.254", 443,
                                     res(PUB)).allowed)
        self.assertFalse(real.decide("203.0.113.5", 443, res(PUB)).allowed)
        broken = NoFixedDenyPolicy(
            {"control_plane_cidrs": ["203.0.113.0/24"],
             "tenant_allowlist": {"169.254.169.254": {443},
                                  "203.0.113.5": {443}}}, clock=clock_seq())
        # the mutant lets the tenant allowlist override the deny set
        self.assertTrue(broken.decide("169.254.169.254", 443,
                                      res(PUB)).allowed)
        self.assertTrue(broken.decide("203.0.113.5", 443, res(PUB)).allowed)

    def test_guard_first_ip_only_breaks_all_ips_checked(self):
        real = policy()
        self.assertFalse(real.decide("example.com", 443,
                                     res(PUB, "10.0.0.5")).allowed)
        broken = FirstIpOnlyPolicy({"tenant_allowlist": {
            "example.com": {443}}}, clock=clock_seq())
        # 1 good + 1 bad walks straight through the mutant
        self.assertTrue(broken.decide("example.com", 443,
                                      res(PUB, "10.0.0.5")).allowed)

    def test_guard_no_management_ports_breaks_any_host_rule(self):
        real = policy()
        self.assertFalse(real.decide("example.com", 6443, res(PUB)).allowed)
        broken = NoManagementPortPolicy(
            {"tenant_allowlist": {"example.com": {443, 6443}}},
            clock=clock_seq())
        self.assertTrue(broken.decide("example.com", 6443, res(PUB)).allowed)

    def test_guard_default_allow_breaks_default_deny(self):
        real = policy()
        self.assertEqual(real.decide("arbitrary.example", 443,
                                     res(PUB)).reason, "not_allowlisted")
        broken = DefaultAllowPolicy({}, clock=clock_seq())
        self.assertTrue(broken.decide("arbitrary.example", 443,
                                      res(PUB)).allowed)

    def test_guard_redirect_following_breaks_no_redirect(self):
        real = policy()
        self.assertFalse(real.connect_rule("https://evil.example/steal",
                                           res(PUB),
                                           is_redirect=True).allowed)
        broken = RedirectFollowingPolicy(
            {"tenant_allowlist": {"evil.example": {443}}}, clock=clock_seq())
        self.assertTrue(broken.connect_rule("https://evil.example/steal",
                                            res(PUB),
                                            is_redirect=True).allowed)

    def test_guard_destroy_on_flush_breaks_isolate_not_destroy(self):
        real = policy()
        self.assertFalse(real.flush_failed()["destroy"])
        self.assertTrue(real.flush_failed()["keep_volumes"])
        broken = DestroyOnFlushPolicy({}, clock=clock_seq())
        self.assertTrue(broken.flush_failed()["destroy"])
        self.assertFalse(broken.flush_failed()["keep_volumes"])


if __name__ == "__main__":
    unittest.main()
