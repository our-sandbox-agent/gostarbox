# Network egress policy (#22)

Status: **Decision engine delivered as a stdlib library + tests; the
real runtime enforcement is NOT started and NOT claimed.** Per the
2026-10-02 盤點 note this slice is the **G04 sub-scope FIRST (Refs
#78)** — host / control-plane / metadata / cross-sandbox isolation as
a pure egress DECISION engine (deny-first allowlist + DNS-rebinding
model + revocation/isolation semantics). The **full #22 remainder —
real iptables/nftables rendering, Docker bridge icc/ARP/source-forgery
live tests (bridge fallback included, 不能只憑 icc=false 判定完成),
live revocation timing — stays runtime-blocked** on the proxy/network
integration and is listed under Runtime TODO.

Sources of truth: issue #22 + its 2026-10-02 盤點 note, issue #78 (G04
sub-scope), [byok-policy.md](byok-policy.md) (`upstream_rule` — the
Claude upstream URL rules, REUSED here, not forked),
[watchdog-lease.md](watchdog-lease.md) (`network_failure_policy` —
block-and-isolate semantics, reused),
[terminal-protocol.json](terminal-protocol.json) (transport.entry: the
unified `console.<domain>` hook endpoints), plan-detail.md 3.3/3.6.

Implementation: `scripts/network_policy.py` (stdlib `ipaddress` only —
no hand-rolled CIDR math; no server, no fs, no network IO); tests:
`scripts/test_network_policy.py` (50 tests incl. 8 mutate-and-fail
guards, CONTRIBUTING idiom).

## Deny first (先 deny 再啟動 workload)

The policy object is built and **default-deny BEFORE any workload
starts**; the default verdict for anything not explicitly allowlisted
is DENY (`not_allowlisted`). The engine is the DECISION half only —
the runtime render/apply order is a Runtime TODO below.

## Fixed deny set

Checked with the `ipaddress` stdlib; classification runs on **every
resolved address** and always **before** any allowlist lookup:

| set | networks (default) | deny reason |
|---|---|---|
| loopback | `127.0.0.0/8`, `::1/128` | `loopback_denied` |
| private | RFC1918 `10/8`, `172.16/12`, `192.168/16` + ULA `fc00::/7` | `private_denied` |
| link-local | `169.254.0.0/16`, `fe80::/10` | `link_local_denied` |
| metadata | `169.254.169.254` (inside link-local, named for triage) | `metadata_denied` |
| control-plane / host | config `control_plane_cidrs` (empty default; RFC1918 already covers the typical ranges) | `control_plane_denied` |
| cross-sandbox | config `cross_sandbox_cidrs` (the sandbox bridge nets) | `cross_sandbox_denied` |
| management ports | config `management_ports` — denied on **ANY host**, allowlisted or not (default: ssh 22, telnet 23, MS rpc/netbios/smb, docker 2375/2376, etcd 2379/2380, rdp 3389, vnc 5900, k8s api 6443, kubelet 10250/10255/10256) | `management_port_denied` |

Config keys (`EgressPolicy(config)`): `sandbox_id`,
`control_plane_cidrs`, `cross_sandbox_cidrs`, `management_ports`,
`hook_endpoints` `[(host, port)]`, `tenant_allowlist`
`{host: [ports]}`, `lease_ttl_ms` (default 30000).

## Decision flow (`decide(host, port, resolver)`)

1. `is_redirect` → deny `redirect_not_followed` (below);
2. port not an int in 1..65535 → `invalid_port`; management port →
   `management_port_denied`;
3. resolve via the **injected** resolver (host → `[ips]`; IP literals
   answer for themselves); unparseable answer → `invalid_ip`; no
   answer → `unresolved`;
4. **every** resolved address is classified against the fixed + config
   deny set — **any denied IP denies the whole request** (1 good + 1
   bad IP → deny);
5. allowlist match (package-compat defaults / configured hook
   endpoints / tenant allowlist) → **allow** and an open tunnel
   record; otherwise **deny `not_allowlisted`** (default deny).

### DNS rebinding (`decide_after_dns`)

The resolver may answer differently on the second call. The model:
`decide_after_dns(host, port, resolver)` consults the resolver **twice**
(pre- and post-resolution answers) and checks the **union** — any
denied IP in either set denies. **The runtime MUST re-check at connect
time** with the addresses it actually dials; this engine is the
policy model of that double check, not the enforcement.

### CONNECT/HTTP proxy semantics (`connect_rule(url, resolver)`)

- The destination is **resolved then checked** exactly like `decide()`
  (scheme must be https; default port 443).
- **Redirects are NEVER followed** — any redirect target is denied
  `redirect_not_followed` (same stance as `byok_policy.upstream_rule`).
- userinfo → `userinfo_present`; bad port literal / no host →
  `invalid_url`.
- The **fixed Claude upstream** (`api.anthropic.com`) delegates its
  URL-level rules to `byok_policy.upstream_rule` — REUSED, not forked
  (path/query/scheme/host rules live there) — while the IP-level deny
  check still applies on top, so a rebound `api.anthropic.com` answer
  pointing at metadata is still denied.

### Tenant allowlist is not trusted (不信任租戶 allowlist)

Tenant allowlist entries only ADD allow candidates; they can never
remove a deny. Because the deny classification (step 4) runs before
any allowlist lookup (step 5), a tenant entry is effectively
**intersected with the deny-free space only** — `169.254.169.254`,
control-plane and cross-sandbox destinations stay denied even when the
tenant allowlists them by name or literal. A mutation guard pins this.

## Default allowlist (package compatibility, https/443 only)

`pypi.org`, `files.pythonhosted.org` (pip), `registry.npmjs.org` (npm),
`deb.debian.org`, `security.debian.org` (apt; snapshot host optional —
add via config when needed), and `api.anthropic.com` (Claude; host
constant imported from `byok_policy`). All are allowed on **443 only**;
port 80 is denied (`not_allowlisted`).

## Hook endpoints

Restricted terminal/console hooks per
[terminal-protocol.json](terminal-protocol.json) (the unified
`console.<domain>` entry): configured explicitly via
`hook_endpoints: [(host, port)]`, **default deny** — an unconfigured
hook host is `not_allowlisted`.

## Revocation and isolation

- `revoke(identifier)` — closes every open tunnel for the identifier
  NOW and returns the **close actions** (one `close_tunnel` per
  tunnel, each carrying its explicit `lease_deadline_at`); no residual
  lease is left behind. `destroy: false`, `keep_volumes: true` —
  teardown/kill stays a human/policy decision (watchdog isolation
  semantics), **never destroy-with-delete**. A mutation guard pins
  this.
- `flush_failed(error, now)` — an egress flush/cleanup failure answers
  **`block_and_isolate`** by direct reuse of
  `watchdog_lease.network_failure_policy("flush", ...)`: volumes KEPT,
  destroy NEVER triggered from a cleanup failure, human investigation
  required, display Unknown.

## Log parity (網頁只宣稱看得到的)

Every decision — allow AND deny — appends one record
`(ts, host, ips, port, verdict, reason)` (`last_log()` / `logs()`),
identical field shape for both verdicts. The console may therefore
claim visibility **only into connections that actually produced a log
record**; no synthetic or predicted connection list is implied.

## Runtime TODO (blocked; NOT claimed)

- **iptables/nftables rendering + apply**: turning this decision
  engine into actual host/bridge rules, and the reverse-path
  verification that they hold.
- **Live bridge/ARP/source-forgery tests** (acceptance row 2): Docker
  bridge with `icc=false` AND the bridge fallback — completion cannot
  be judged from `icc=false` alone — plus ARP spoofing and forged-source
  attempts across sandboxes.
- **A real CONNECT/HTTP proxy** consulting `decide`/`connect_rule`, and
  the connect-time re-check of the resolver answers (rebinding duty
  above).
- **Live revocation timing** (撤銷時限): real close-latency of
  established tunnels after `revoke`, and lease expiry on unreachable
  peers.
- Console/dashboard wiring of the log records (log-parity claim above).

## Explicitly not done here

- No iptables/nftables emission, no privileged network calls, no DNS,
  no proxy process — pure decisions + tests.
- No MITM / transparent proxy / multi-runner (issue 本票不含).
- #78 passing does NOT close #22 and does NOT by itself enable the
  trial; the full egress policy + revocation + package compatibility
  (this engine's runtime) is still required.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m compileall -q scripts
```

All pure-stdlib and offline.
