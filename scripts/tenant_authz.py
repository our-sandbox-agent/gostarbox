#!/usr/bin/env python3
"""Tenant authorization rules for issue #16 (in-repo stdlib slice).

Rules-only library: the authorization / quota / terminal-ticket disciplines the
real #76-style control plane (Hono + Postgres) must enforce. 沿用 #76 schema，
不另起 API — this module adds no endpoints and no server. Real OAuth, cookies,
Postgres transactions and deployment are runtime TODOs listed in
docs/contracts/tenant-authz.md; nothing here is deployed.

Components:
  hash_token / verify_token / TokenRegistry  — tokens stored hashed (sha256),
      revocation list, is_revoked; the clear token exists only at issuance.
  Authorize(token_hash, workspace_of_token, request) — workspace-scoped access
      check for the 7 endpoint classes; cross-workspace/unknown resources
      answer 404 (no probe signal), cross-user actions within one workspace
      answer 403, missing/unknown/revoked token answers 401.
  TerminalTicketPolicy — short-lived single-use attach tickets bound to
      (user, workspace, sandbox_id, generation); reconnect mints a NEW ticket;
      tickets are stored keyed by sha256(id) and logged only via redact().
  QuotaGate — workspace quota + create rate-limit with ATOMIC admission
      (check-then-commit under one lock; the stand-in for the real DB
      transaction/unique constraint).  `--race` demonstrates that naive
      count-then-insert over-admits under 50+ threads while the gate does not.
  InviteList — 邀請名單: login only for invited accounts, no open registration.

Tests: scripts/test_tenant_authz.py (mutate-and-fail guards included).
"""
import argparse
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

ENDPOINT_CLASSES = ("api", "exec", "terminal_ticket", "file", "snapshot",
                    "secrets", "operation")

# --------------------------------------------------------------------- tokens


def hash_token(token):
    """sha256 hex of a token; the only form ever stored or compared."""
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token, token_hash):
    """Constant-time check that `token` matches the stored hash."""
    return hmac.compare_digest(hash_token(token), token_hash)


def new_token():
    """Fresh opaque token material (clear form; hash it before storage)."""
    return secrets.token_urlsafe(32)


class TokenRegistry:
    """Token store that only ever holds sha256 hashes plus a revocation list.

    A revoked token authenticates as nothing (401), same as an unknown one.
    """

    def __init__(self):
        self._by_hash = {}  # token_hash -> {"workspace", "user"}; never clear
        self._revoked = set()

    def register(self, token, workspace, user=None):
        """Register a freshly issued token; returns its hash for callers."""
        token_hash = hash_token(token)
        self._by_hash[token_hash] = {"workspace": workspace, "user": user}
        return token_hash

    def lookup(self, token):
        """Record for a presented token, or None when unknown/revoked."""
        token_hash = hash_token(token)
        if token_hash in self._revoked:
            return None
        return self._by_hash.get(token_hash)

    def revoke(self, token_hash):
        self._revoked.add(token_hash)

    def is_revoked(self, token_hash):
        return token_hash in self._revoked


# ----------------------------------------------------------------- authorize


class Decision(tuple):
    """(allowed, http_status, code); the rule table's answer vocabulary."""

    __slots__ = ()

    def __new__(cls, allowed, status, code):
        return super().__new__(cls, (allowed, status, code))

    allowed = property(lambda self: self[0])
    status = property(lambda self: self[1])
    code = property(lambda self: self[2])


ALLOW = Decision(True, 200, "ok")
UNAUTHORIZED = Decision(False, 401, "unauthorized")
FORBIDDEN = Decision(False, 403, "forbidden")
NOT_FOUND = Decision(False, 404, "not_found")


class Authorize:
    """Workspace-scoped access rule, uniform across the 7 endpoint classes.

    Rule table (docs/contracts/tenant-authz.md is the normative copy):
      token unknown, or its workspace unknown -> 401 unauthorized
      resource unknown                        -> 404 not_found
      resource in another workspace           -> 404 not_found, the SAME answer
                                                as unknown: no probe signal
      user-scoped resource of another user in
      the same workspace (shared endpoint)    -> 403 forbidden
      everything else                         -> allow

    `resources` maps resource_id -> {"workspace": str, "owner": str|None};
    owner marks user-scoped resources (e.g. a personal secret).
    """

    def __init__(self, resources=None):
        self.resources = dict(resources or {})

    def register_resource(self, resource_id, workspace, owner=None):
        self.resources[resource_id] = {"workspace": workspace, "owner": owner}

    def __call__(self, token_hash, workspace_of_token, user_of_token, request):
        endpoint = request.get("endpoint_class")
        if endpoint not in ENDPOINT_CLASSES:
            raise ValueError(f"unknown endpoint class: {endpoint!r}")
        if not token_hash or workspace_of_token is None:
            return UNAUTHORIZED
        resource = self.resources.get(request.get("resource_id"))
        if resource is None or resource["workspace"] != workspace_of_token:
            return NOT_FOUND
        # The acting principal comes ONLY from the verified token record; a
        # user field inside the request is never trusted for authorization.
        owner = resource.get("owner")
        if owner is not None and owner != user_of_token:
            return FORBIDDEN
        return ALLOW


# ------------------------------------------------------------ terminal ticket


def redact(ticket_id):
    """Loggable stand-in for a ticket id: hashed prefix only, never clear."""
    return "tt_" + hash_token(ticket_id)[:12]


class TerminalTicketPolicy:
    """Short-lived single-use attach tickets for the terminal hello frame.

    A ticket binds (user, workspace, sandbox_id, generation) at issue time;
    redeem() accepts it exactly once while younger than ttl_s (default 60s,
    issue-configurable). Reconnect means minting a NEW ticket — replaying a
    consumed id is rejected. The store is keyed by sha256(ticket_id) so the
    clear id exists only in the mint response; logs use redact().
    Rejection codes: unknown_ticket, replay, expired, binding_mismatch.
    """

    def __init__(self, clock=time.monotonic, ttl_s=60.0):
        self._clock = clock
        self.ttl_s = ttl_s
        self._tickets = {}  # sha256(ticket_id) -> record; never the clear id
        self._lock = threading.Lock()

    def issue(self, user, workspace, sandbox_id, generation):
        ticket_id = secrets.token_urlsafe(24)
        self._tickets[hash_token(ticket_id)] = {
            "user": user,
            "workspace": workspace,
            "sandbox_id": sandbox_id,
            "generation": generation,
            "issued_at": self._clock(),
            "consumed": False,
        }
        return ticket_id

    def redeem(self, ticket_id, user, workspace, sandbox_id, generation):
        """Single-use bind-checked redemption; True on the one valid use.

        The whole check-then-commit runs under one lock so concurrent redeems
        of the same ticket cannot both succeed (same discipline as QuotaGate).
        """
        with self._lock:
            record = self._tickets.get(hash_token(ticket_id))
            if record is None:
                return False, "unknown_ticket"
            if record["consumed"]:
                return False, "replay"
            if self._clock() - record["issued_at"] > self.ttl_s:
                return False, "expired"
            if (record["user"], record["workspace"], record["sandbox_id"],
                    record["generation"]) != (user, workspace, sandbox_id,
                                              generation):
                return False, "binding_mismatch"
            record["consumed"] = True
            return True, "ok"


# ------------------------------------------------------------------ quota gate


class QuotaGate:
    """Workspace quota + create rate-limit with ATOMIC admission.

    try_admit() runs check-then-commit as one critical section — the stdlib
    stand-in for the real Postgres transaction / unique constraint the issue
    demands (先 count 再 insert without the transaction is exactly the
    over-admission bug). The optional `stall` hook runs inside the critical
    section purely to widen the check->commit window so tests and --race can
    observe what concurrency does; it is not a feature.
    """

    def __init__(self, stall=None):
        self._lock = threading.Lock()
        self._stall = stall or (lambda: None)
        self._used = {}  # workspace -> admitted units
        self._creates = {}  # workspace -> deque of create timestamps

    def try_admit(self, workspace, count, quota):
        """Atomically reserve `count` units iff the workspace stays in quota."""
        with self._lock:
            used = self._used.get(workspace, 0)
            self._stall()
            if used + count > quota:
                return False
            self._used[workspace] = used + count
            return True

    def release(self, workspace, count=1):
        with self._lock:
            self._stall()
            self._used[workspace] = max(0, self._used.get(workspace, 0) - count)

    def used(self, workspace):
        return self._used.get(workspace, 0)

    def try_create(self, workspace, now, limit, window_s):
        """Create rate-limit: at most `limit` creates per sliding window."""
        with self._lock:
            window = self._creates.setdefault(workspace, deque())
            cutoff = now - window_s
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True


# ---------------------------------------------------------------- invite list


class InviteList:
    """邀請名單 (issue #16): login only for invited accounts.

    No open registration (本票不含公開自由註冊): absent from the list means
    no login, not a pending signup.
    """

    def __init__(self):
        self._invited = set()

    def invite(self, email):
        self._invited.add(email)

    def revoke(self, email):
        self._invited.discard(email)

    def is_invited(self, email):
        """The login gate: True only while on the list."""
        return email in self._invited


# ----------------------------------------------------------------- --race demo


def run_concurrently(n, target):
    """Run target() in n threads released simultaneously by a barrier."""
    barrier = threading.Barrier(n)

    def runner():
        barrier.wait()
        target()

    threads = [threading.Thread(target=runner) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _race_demo(threads=50, quota=25, stall_s=0.005):
    """Naive count-then-insert vs QuotaGate under `threads` racing threads."""
    gate = QuotaGate(stall=lambda: time.sleep(stall_s))
    counters = {"naive": 0, "gate": 0}
    naive_used = {}

    def naive_admit():
        used = naive_used.get("ws", 0)  # count ...
        time.sleep(stall_s)  # ... the race window ...
        if used + 1 <= quota:
            naive_used["ws"] = used + 1  # ... blind insert over-admits
            counters["naive"] += 1

    def gated_admit():
        if gate.try_admit("ws", 1, quota):
            counters["gate"] += 1

    run_concurrently(threads, naive_admit)
    run_concurrently(threads, gated_admit)
    print(f"quota={quota} threads={threads} stall={stall_s * 1000:.0f}ms")
    print(f"naive count-then-insert: admitted {counters['naive']} "
          f"(over-admits: {counters['naive'] > quota})")
    print(f"QuotaGate (locked):      admitted {counters['gate']} "
          f"(bounded: {counters['gate'] <= quota})")
    return counters


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="demonstrate quota admission atomicity (issue #16)")
    parser.add_argument("--race", action="store_true",
                        help="run the over-admission race demonstration")
    parser.add_argument("--threads", type=int, default=50)
    args = parser.parse_args(argv)
    if not args.race:
        parser.print_help()
        return 0
    counters = _race_demo(threads=args.threads)
    # the gate must never over-admit; naive over-admission is the demonstration
    return 0 if counters["gate"] <= 25 else 1


if __name__ == "__main__":
    raise SystemExit(main())
