#!/usr/bin/env python3
"""Network egress decision engine for issue #22 (in-repo stdlib slice).

Per the 2026-10-02 盤點 note this is the **G04 sub-scope first**
(Refs #78): host / control-plane / metadata / cross-sandbox isolation
as a PURE decision engine — the deny-first allowlist logic that a
CONNECT/HTTP proxy (or an iptables/nftables renderer) will consult.
The FULL #22 runtime — real iptables/nftables rendering, Docker
bridge icc/ARP/source-forgery live tests (bridge fallback, 不能只憑
icc=false 判定), live revocation timing — stays runtime-blocked and is
NOT claimed (docs/contracts/network-policy.md maps delivered vs
blocked).

Order of business is DENY FIRST (先 deny 再啟動 workload): the policy
is built and default-deny BEFORE any workload starts; the default
verdict for anything not explicitly allowlisted is DENY.

Fixed deny set (python ipaddress stdlib — no hand-rolled CIDR math):
  loopback 127.0.0.0/8 + ::1/128; private RFC1918 + ULA fc00::/7;
  link-local 169.254.0.0/16 + fe80::/10 (metadata 169.254.169.254
  included and named); control-plane/host CIDRs (config); cross-sandbox
  CIDRs (config); management ports on ANY host (config).

Reuse, not fork:
  - byok_policy.upstream_rule owns the Claude upstream URL rules; this
    module only adds the IP-level deny check on top, and takes the
    api.anthropic.com host from byok_policy.UPSTREAM_HOST.
  - watchdog_lease.network_failure_policy owns the block-and-isolate
    flush-failure semantics (keep volumes, never destroy-with-delete).

Components (EgressPolicy):
  decide(host, port, resolver) — resolve via the injected resolver
      (host -> [ips]), check EVERY resolved address against the deny
      set (any denied IP denies), then the allowlist.
  decide_after_dns(host, port, resolver) — DNS-rebinding model: the
      resolver is consulted twice (pre-/post-resolution answers may
      differ); BOTH answer sets are checked as one union. The runtime
      MUST re-check at connect time with the addresses it actually
      dials — this module is the decision, not the enforcement.
  connect_rule(url, resolver, is_redirect) — CONNECT/HTTP proxy
      semantics: destination resolved then checked; redirects are
      NEVER followed (same stance as byok upstream_rule); the fixed
      Claude upstream delegates to upstream_rule.
  Tenant allowlist entries NEVER override the fixed deny set — tenant
      entries are intersected with the deny-free space only (不信任租戶
      allowlist): the deny check runs BEFORE any allowlist match.
  revoke(identifier) — closes existing tunnels (close actions listed,
      explicit lease deadline per tunnel); never destroys volumes.
  flush_failed() — block_and_isolate, watchdog semantics (isolate, do
      NOT destroy-with-delete).
  last_log() — every decision is loggable: (ts, host, ips, port,
      verdict, reason) with allow/deny parity.

Tests: scripts/test_network_policy.py (mutate-and-fail guards incl.).
Spec: docs/contracts/network-policy.md.
"""
import ipaddress
import time
import urllib.parse

from byok_policy import UPSTREAM_HOST, upstream_rule
from watchdog_lease import network_failure_policy

# --------------------------------------------------------------- constants

# Cloud metadata lives INSIDE link-local 169.254.0.0/16; it is denied by
# the link-local rule but named explicitly in the log for triage.
METADATA_ADDRESSES = (
    ipaddress.ip_address("169.254.169.254"),
)

FIXED_DENY_NETWORKS = (
    (ipaddress.ip_network("127.0.0.0/8"), "loopback_denied"),
    (ipaddress.ip_network("::1/128"), "loopback_denied"),
    (ipaddress.ip_network("10.0.0.0/8"), "private_denied"),      # RFC1918
    (ipaddress.ip_network("172.16.0.0/12"), "private_denied"),   # RFC1918
    (ipaddress.ip_network("192.168.0.0/16"), "private_denied"),  # RFC1918
    (ipaddress.ip_network("fc00::/7"), "private_denied"),        # ULA
    (ipaddress.ip_network("169.254.0.0/16"), "link_local_denied"),
    (ipaddress.ip_network("fe80::/10"), "link_local_denied"),
    (ipaddress.ip_network("0.0.0.0/8"), "loopback_denied"),      # reaches localhost on Linux
    (ipaddress.ip_network("64:ff9b::/96"), "nat64_denied"),      # embedded IPv4
    (ipaddress.ip_network("64:ff9b:1::/48"), "nat64_denied"),    # local-use NAT64
    (ipaddress.ip_network("224.0.0.0/4"), "multicast_denied"),
    (ipaddress.ip_network("ff00::/8"), "multicast_denied"),
    (ipaddress.ip_network("240.0.0.0/4"), "reserved_denied"),
)

# Management/infrastructure ports denied on ANY host, allowlist or not.
MANAGEMENT_PORTS = frozenset({
    22,                     # ssh
    23,                     # telnet
    135, 139, 445,          # windows rpc / netbios / smb
    2375, 2376,             # docker daemon (plain / tls)
    2379, 2380,             # etcd
    3389,                   # rdp
    5900,                   # vnc
    6443,                   # kubernetes api
    10250, 10255, 10256,    # kubelet / read-only / metrics
})

# Package-manager + Claude compatibility DEFAULT allowlist: https/443
# ONLY (per the #22 acceptance: pip/npm/apt/Claude 相容性). The Claude
# host comes from byok_policy (reuse, not a fork).
PACKAGE_COMPAT_ALLOWLIST = {
    "pypi.org": frozenset({443}),               # pip
    "files.pythonhosted.org": frozenset({443}),
    "registry.npmjs.org": frozenset({443}),     # npm
    "deb.debian.org": frozenset({443}),         # apt
    "security.debian.org": frozenset({443}),
    UPSTREAM_HOST: frozenset({443}),            # Claude upstream
}

DEFAULT_LEASE_TTL_MS = 30000


# ----------------------------------------------------------------- verdicts

class Decision(tuple):
    """(allowed, reason): the egress answer vocabulary. The full log
    record of the same call — (ts, host, ips, port, verdict, reason) —
    is in policy.last_log()."""

    __slots__ = ()

    def __new__(cls, allowed, reason):
        return super().__new__(cls, (allowed, reason))

    allowed = property(lambda self: self[0])
    reason = property(lambda self: self[1])


def _policy_config(config):
    """Normalized config view (defaults + lowercase host tables)."""
    cfg = dict(config or {})
    parsed = {
        "sandbox_id": cfg.get("sandbox_id"),
        "control_plane_cidrs": list(cfg.get("control_plane_cidrs", [])),
        "cross_sandbox_cidrs": list(cfg.get("cross_sandbox_cidrs", [])),
        "management_ports": frozenset(
            cfg.get("management_ports", MANAGEMENT_PORTS)),
        "tenant_allowlist": {h.lower(): frozenset(p) for h, p in
                             (cfg.get("tenant_allowlist") or {}).items()},
        "hook_endpoints": {h.lower(): frozenset({p}) for h, p in
                           (cfg.get("hook_endpoints") or [])},
        "lease_ttl_ms": cfg.get("lease_ttl_ms", DEFAULT_LEASE_TTL_MS),
    }
    return parsed


class EgressPolicy:
    """Deny-first egress decision engine for one sandbox egress context.

    Decision order in _decide (each step can end the request):
      redirect_not_followed -> invalid_port -> management_port_denied
      -> invalid_ip -> unresolved -> fixed/config deny classification
      -> allowlist match (default package list / hook endpoints /
      tenant allowlist) -> not_allowlisted (DEFAULT DENY).

    The deny classification ALWAYS runs before any allowlist lookup, so
    a tenant allowlist entry can only ever allow a deny-free
    destination (intersected with the deny-free space only).

    The named _-prefixed helpers are deliberate mutation points for the
    guard tests, not extension points.
    """

    def __init__(self, config=None, clock=time.time):
        cfg = _policy_config(config)
        self._clock = clock
        self._sandbox_id = cfg["sandbox_id"]
        self._management_ports = cfg["management_ports"]
        self._tenant_allowlist = cfg["tenant_allowlist"]
        self._hooks = cfg["hook_endpoints"]
        self._allowlist = dict(PACKAGE_COMPAT_ALLOWLIST)
        self._lease_ttl_ms = cfg["lease_ttl_ms"]
        self._deny_networks = list(FIXED_DENY_NETWORKS)
        self._deny_networks += [
            (ipaddress.ip_network(cidr, strict=False), "control_plane_denied")
            for cidr in cfg["control_plane_cidrs"]]
        self._deny_networks += [
            (ipaddress.ip_network(cidr, strict=False), "cross_sandbox_denied")
            for cidr in cfg["cross_sandbox_cidrs"]]
        self._log = []
        self._tunnels = []
        self._seq = 0

    # ------------------------------------------------------------ decide

    def decide(self, host, port, resolver=None, is_redirect=False):
        """Resolve (injected resolver: host -> [ips]), check EVERY
        resolved address, then the allowlist. IP-literal hosts skip the
        resolver. Any denied IP denies the whole request."""
        host = (host or "").strip().lower()
        return self._decide(host, port, self._resolve(host, resolver),
                            is_redirect)

    def decide_after_dns(self, host, port, resolver, is_redirect=False):
        """DNS-rebinding model: the resolver is consulted TWICE — the
        pre-resolution and post-resolution answers may differ under
        rebinding — and the UNION of both answer sets is checked; any
        denied IP in either set denies. The runtime MUST re-check at
        connect time with the addresses it actually dials (decide() on
        the final answer set); this method is the policy model of that
        double check, not the enforcement."""
        host = (host or "").strip().lower()
        pre = self._resolve(host, resolver)
        post = self._resolve(host, resolver)
        union = list(pre)
        for ip in post:
            if ip not in union:
                union.append(ip)
        return self._decide(host, port, union, is_redirect)

    def connect_rule(self, url, resolver=None, is_redirect=False):
        """CONNECT/HTTP proxy semantics: the destination is resolved
        then checked exactly like decide(); redirects are NEVER followed
        (deny the redirect target, same stance as byok upstream_rule).
        For the FIXED Claude upstream the URL-level rules are
        byok_policy.upstream_rule REUSED, not forked — the IP-level
        deny check still applies on top (a rebound api.anthropic.com
        answer to link-local is still denied)."""
        ts = self._clock()
        try:
            parts = urllib.parse.urlsplit((url or "").strip())
            port = parts.port  # ValueError on a bad port literal
        except ValueError:
            return self._record("", None, [], False, "invalid_url", ts)
        host = (parts.hostname or "").lower()
        if is_redirect:
            return self._record(host, port, [], False,
                                "redirect_not_followed", ts)
        if not host:
            return self._record(host, port, [], False, "invalid_url", ts)
        if parts.username or parts.password:
            return self._record(host, port, [], False, "userinfo_present",
                                ts)
        if parts.scheme != "https":
            return self._record(host, port, [], False, "scheme_not_https",
                                ts)
        if port is None:
            port = 443
        if host == UPSTREAM_HOST:
            verdict = upstream_rule(url, is_redirect=False)
            if not verdict.allowed:
                return self._record(host, port, [], False, verdict.reason,
                                    ts)
        return self.decide(host, port, resolver)

    # ------------------------------------------------------ decision core

    def _decide(self, host, port, ips, is_redirect=False):
        ts = self._clock()
        if is_redirect:
            return self._record(host, port, ips, False,
                                "redirect_not_followed", ts)
        if type(port) is not int or not 1 <= port <= 65535:
            return self._record(host, port, ips, False, "invalid_port", ts)
        if self._port_denied(port):
            return self._record(host, port, ips, False,
                                "management_port_denied", ts)
        parsed = []
        for raw in ips:
            try:
                parsed.append(ipaddress.ip_address(str(raw).strip("[]")))
            except ValueError:
                return self._record(host, port, ips, False, "invalid_ip",
                                    ts)
        if not parsed:
            return self._record(host, port, ips, False, "unresolved", ts)
        reason = self._any_deny(parsed)
        if reason is not None:
            return self._record(host, port, ips, False, reason, ts)
        if not self._is_allowlisted(host, port):
            return self._record(host, port, ips, False, "not_allowlisted",
                                ts)
        self._open_tunnel(host, port, ips, ts)
        return self._record(host, port, ips, True, "allow", ts)

    def _resolve(self, host, resolver):
        """IP literals answer for themselves (no resolver consulted);
        names ask the injected resolver (host -> [ips]). A failing resolver
        yields NO addresses (unresolved → default deny), never an exception
        out of decide()."""
        bare = host.strip("[]")
        try:
            ipaddress.ip_address(bare)
            return [bare]
        except ValueError:
            pass
        if resolver is None:
            return []
        try:
            return [str(ip) for ip in (resolver(host) or [])]
        except Exception:
            return []

    # ------------------------------------------------- mutation points

    def _port_denied(self, port):
        """Management/infrastructure ports are denied on ANY host,
        allowlisted or not."""
        return port in self._management_ports

    def _deny_reason(self, ip):
        """Classification of ONE address against the fixed + config deny
        set; None = deny-free. ip is a parsed ipaddress object.
        IPv4-mapped/6to4/Teredo IPv6 forms are unwrapped first — dual-stack
        sockets dial them as plain IPv4, so ::ffff:169.254.169.254 must hit
        the v4 metadata rule, not sail past a version-mismatched `in`."""
        if ip.version == 6:
            if ip.ipv4_mapped is not None:
                ip = ip.ipv4_mapped
            elif ip.sixtofour is not None:
                ip = ip.sixtofour
            elif ip.teredo is not None:
                ip = ip.teredo[1]
        if any(ip == meta for meta in METADATA_ADDRESSES):
            return "metadata_denied"
        for network, reason in self._deny_networks:
            if ip in network:  # version-mismatched `in` is just False
                return reason
        return None

    def _any_deny(self, parsed_ips):
        """ALL resolved addresses are checked (any denied IP denies);
        the first deny reason wins the log line."""
        for ip in parsed_ips:
            reason = self._deny_reason(ip)
            if reason is not None:
                return reason
        return None

    def _is_allowlisted(self, host, port):
        """Allow verdict source: default package-compat list, configured
        hook endpoints, or the tenant allowlist. The fixed deny set was
        checked BEFORE this — tenant entries can only allow deny-free
        destinations (deny-free space intersection only)."""
        for table in (self._allowlist, self._hooks, self._tenant_allowlist):
            ports = table.get(host)
            if ports and port in ports:
                return True
        return False

    # ------------------------------------------------------------ logging

    def _record(self, host, port, ips, allowed, reason, ts):
        """One loggable decision: (ts, host, ips, port, verdict,
        reason). Allow and deny records share the exact same shape
        (allow/deny log parity — 網頁只宣稱看得到實際有記錄的連線)."""
        record = {
            "ts": ts,
            "host": host,
            "ips": [str(ip) for ip in ips],
            "port": port,
            "verdict": "allow" if allowed else "deny",
            "reason": reason,
        }
        self._log.append(record)
        return Decision(allowed, reason)

    def last_log(self):
        """The most recent decision record (None before any call)."""
        return dict(self._log[-1]) if self._log else None

    def logs(self):
        return [dict(record) for record in self._log]

    # -------------------------------------------------------- revocation

    def _open_tunnel(self, host, port, ips, ts):
        self._seq += 1
        self._tunnels.append({
            "tunnel_id": f"tun-{self._seq}",
            "sandbox_id": self._sandbox_id,
            "host": host,
            "ips": [str(ip) for ip in ips],
            "port": port,
            "opened_at": ts,
            "lease_deadline_at": ts + self._lease_ttl_ms / 1000.0,
            "status": "open",
            "closed_at": None,
            "closed_reason": None,
        })

    def open_tunnels(self):
        """Still-open tunnels (revocation input)."""
        return [dict(t) for t in self._tunnels if t["status"] == "open"]

    def revoke(self, identifier=None):
        """Close every open tunnel for the identifier (sandbox) NOW and
        list the close actions; each action carries the tunnel's
        explicit lease deadline. Volumes are KEPT and nothing is
        destroyed — teardown/kill stays a human/policy decision
        (watchdog isolation semantics), never destroy-with-delete."""
        ident = identifier if identifier is not None else self._sandbox_id
        now = self._clock()
        actions = []
        for tunnel in self._tunnels:
            if (tunnel["sandbox_id"] == ident
                    and tunnel["status"] == "open"):
                tunnel["status"] = "closed"
                tunnel["closed_at"] = now
                tunnel["closed_reason"] = "revoked"
                actions.append({
                    "action": "close_tunnel",
                    "tunnel_id": tunnel["tunnel_id"],
                    "sandbox_id": ident,
                    "host": tunnel["host"],
                    "port": tunnel["port"],
                    "lease_deadline_at": tunnel["lease_deadline_at"],
                })
        return {
            "revoked": bool(actions),
            "reason": None if actions else "no_open_tunnels",
            "identifier": ident,
            "close_actions": actions,
            "destroy": False,
            "keep_volumes": True,
            "note": ("tunnels closed now (no residual lease); sandbox "
                     "teardown stays a human/policy decision (watchdog "
                     "isolation)"),
        }

    def flush_failed(self, error=None, now=None):
        """Egress flush/cleanup failure: BLOCK and ISOLATE (watchdog
        network_failure_policy semantics, reused not forked) — volumes
        KEPT, a data-deleting destroy is NEVER triggered from a cleanup
        failure."""
        return network_failure_policy("flush", error=error, now=now)
