#!/usr/bin/env python3
"""Host watchdog lease, stop-confirmation and quota-restore rules for #18
(stdlib only).

Executable SEMANTICS of the #18 acceptance rules, not a runtime: a
host-side lease with per-sandbox epoch fencing (the #17 generation
convention from scripts/operation_reconcile.py — reused, not forked), the
detect→confirm stop discipline with three recorded timestamps
(detected_at, confirmed_stopped_at, billing_cutoff_at — the billing cutoff
is the confirmed stop ONLY; between detect and confirm the sandbox shows
Lost/Unknown, never Active, never free, matching the uncertain intervals
of scripts/resource_events.py), cgroup v2 quota value builders that NEVER
emit "max" (cpu.max "max" means UNLIMITED per the kernel cgroup-v2 doc and
would break the purchased quota — issue #18 note on plan-detail 3.4), and
the network-failure isolation policy (block + isolate, keep volumes,
never destroy-with-delete semantics).

Real host watchdog integration (kill -9 tests, cgroup sysfs writes,
multi-sandbox pressure tests, host-only management UID) is blocked on
#11/#17; this module writes no sysfs file and kills no process.

Tests: scripts/test_watchdog_lease.py.
Spec: docs/contracts/watchdog-lease.md.
"""
import copy

QUOTA_PERIOD_US = 100000   # cgroup v2 cpu.max default period (100ms)
ISOLATION_ACTION = "block_and_isolate"

# Stop-proof sources the HOST accepts on its own authority. A runner's
# report counts ONLY from a reachable runner: a stopped/dead runner cannot
# prove that it suspended (不能用已停止的 runner 證明必定會 suspend).
WATCHDOG_PROOF_SOURCES = frozenset({
    "watchdog_kill_confirmed",    # watchdog's own kill, host-confirmed
    "host_observed_stopped",      # host-level stopped-process observation
})
RUNNER_PROOF_SOURCE = "runner_confirmed_stop"


class StaleEpoch(Exception):
    """Renew from an old epoch: the old runner's lease cannot extend past
    the new instance's (fencing, #17 generation convention)."""


class LeaseExpired(Exception):
    """Renew of an already-expired lease: the generation is fenced for
    recovery (a new epoch), never resurrected."""


class QuotaRestoreError(ValueError):
    """A quota restore value would be unlimited ("max") or invalid."""


def _require_positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


class Lease:
    """One sandbox's host-side lease with an injected clock.

    The epoch counter is the per-sandbox fencing token, monotonically
    increasing across runner (re)creations (the #17 generation
    convention). issue() grants a NEW epoch and fences every older one;
    renew(epoch) from anything but the current epoch is rejected — a
    stale runner after recreation cannot keep the old lease alive.
    The named _-prefixed helpers are deliberate mutation points for the
    guard tests, not extension points.
    """

    def __init__(self, sandbox_id, clock, ttl_ms=30000):
        self.sandbox_id = sandbox_id
        self._clock = clock            # callable -> integer ms
        self.ttl_ms = ttl_ms
        self._epoch = 0                # last issued fencing token
        self._current = None

    def issue(self, ttl_ms=None):
        """Grant a new lease to a (new) runner instance: bumps the epoch."""
        now = self._now()
        ttl = self.ttl_ms if ttl_ms is None else ttl_ms
        self._epoch += 1
        self._current = {
            "sandbox_id": self.sandbox_id,
            "epoch": self._epoch,
            "ttl_ms": ttl,
            "issued_at": now,
            "last_heartbeat_at": now,
            "deadline_at": now + ttl,
        }
        return self.lease()

    def renew(self, epoch, now=None):
        """Heartbeat from the CURRENT epoch only: extends the deadline.
        Stale epoch -> StaleEpoch; expired lease -> LeaseExpired (fenced;
        recovery is a new epoch, never zombie resurrection)."""
        if self._current is None:
            raise ValueError("no lease issued")
        now = self._now(now)
        if not self._epoch_ok(epoch, self._current["epoch"]):
            raise StaleEpoch(
                f"epoch {epoch} is fenced; current is {self._current['epoch']}")
        if self.expired_at(now):
            raise LeaseExpired("lease already expired; fenced for recovery")
        self._current["last_heartbeat_at"] = now
        self._current["deadline_at"] = now + self._current["ttl_ms"]
        return self.lease()

    def expired_at(self, now=None):
        """Half-open expiry: expired once now >= deadline_at."""
        if self._current is None:
            return True   # never issued = nothing trusted
        return self._now(now) >= self._current["deadline_at"]

    def lease(self):
        return copy.deepcopy(self._current)

    # ------------------------------------------------------ mutation points
    def _now(self, now=None):
        return self._clock() if now is None else now

    def _epoch_ok(self, renew_epoch, current_epoch):
        """Fence: only the exact current epoch may renew."""
        return renew_epoch == current_epoch


class WatchdogPolicy:
    """Detect → confirm stop discipline for the #18 host watchdog.

    evaluate() is a PURE decision over the persisted lease state (the
    caller records the returned timestamps — the Reconciler.diff
    convention: deciding is separate from doing). T_detect is recorded
    when expiry is first seen; a confirmed stop — and ONLY a confirmed
    stop — sets confirmed_stopped_at and the billing cutoff. Between
    detect and confirm the sandbox displays Lost/Unknown: not Active,
    capacity not released, billing interval not closed.

    lease_state keys:
      lease_expired   bool
      detected_at     ms | None            (T_detect once recorded)
      epoch           int | None           (current fencing epoch)
      stop_evidence   None | {"source": str, "at": ms, "epoch": int|None}

    The named _-prefixed helpers are deliberate mutation points for the
    guard tests, not extension points.
    """

    def __init__(self, confirm_timeout_ms=30000):
        self.confirm_timeout_ms = confirm_timeout_ms

    def evaluate(self, now, lease_state, runner_reachable):
        if not lease_state.get("lease_expired"):
            return self._healthy_decision()
        detected_at = lease_state.get("detected_at")
        if detected_at is None:
            detected_at = now       # T_detect: first sight of the expiry
        evidence, rejection = self._admit(lease_state.get("stop_evidence"),
                                          runner_reachable,
                                          lease_state.get("epoch"))
        if evidence is None:
            return self._unconfirmed_decision(now, detected_at, rejection)
        confirmed_at = max(evidence["at"], detected_at)
        return {
            "phase": "confirmed_stopped",
            "display_state": "Lost",            # proven; recovery via #17
            "detected_at": detected_at,
            "confirmed_stopped_at": confirmed_at,
            "billing_cutoff_at": confirmed_at,  # cutoff = confirmed stop ONLY
            "release_capacity": True,
            "alerts": ["lease_expired"],
            "actions": ["close_billing_interval", "release_capacity"],
            "requires_human": False,
            "evidence_rejected": None,
        }

    def watchdog_unreachable_alert(self, now):
        """The WATCHDOG ITSELF is dead/unreachable: distinct alert + human
        path. Never derives a stop confirmation, billing cutoff or
        capacity release from watchdog failure (Lost counting 0 does not
        mean the host stopped — issue #18 本票不含)."""
        return {
            "phase": "watchdog_unreachable",
            "flag": "watchdog_failed",          # DISTINCT from lease alerts
            "display_state": "Unknown",
            "detected_at": now,
            "confirmed_stopped_at": None,
            "billing_cutoff_at": None,
            "release_capacity": False,
            "alerts": ["watchdog_unreachable"],
            "actions": ["human_investigation"],
            "requires_human": True,
            "evidence_rejected": None,
        }

    # ------------------------------------------------------ mutation points
    def _admit(self, evidence, runner_reachable, epoch):
        """(admitted evidence | None, rejection reason | None).

        A dead runner's own report is NEVER proof of suspend; host-side
        kill confirmation or stopped-process observation is; evidence
        from a stale epoch is not (a recreated instance is not evidence,
        #17 convention)."""
        if not evidence:
            return None, None
        source = evidence.get("source")
        if source in WATCHDOG_PROOF_SOURCES:
            pass
        elif source == RUNNER_PROOF_SOURCE and runner_reachable:
            pass
        elif source == RUNNER_PROOF_SOURCE:
            return None, "dead_runner_not_proof"
        else:
            return None, "unknown_proof_source"
        if (epoch is not None and evidence.get("epoch") is not None
                and evidence["epoch"] != epoch):
            return None, "stale_epoch_not_proof"
        return evidence, None

    def _healthy_decision(self):
        return {"phase": "healthy", "display_state": None,
                "detected_at": None, "confirmed_stopped_at": None,
                "billing_cutoff_at": None, "release_capacity": False,
                "alerts": [], "actions": [], "requires_human": False,
                "evidence_rejected": None}

    def _unconfirmed_decision(self, now, detected_at, rejection):
        decision = {"phase": "lost_unconfirmed",
                    "display_state": "Lost",     # Unknown/Lost, not Active
                    "detected_at": detected_at,
                    "confirmed_stopped_at": None,
                    "billing_cutoff_at": None,   # NO cutoff before confirm
                    "release_capacity": False,   # not free
                    "alerts": ["lease_expired"],
                    "actions": ["fence_old_epoch", "attempt_stop",
                                "collect_stop_evidence"],
                    "requires_human": False,
                    "evidence_rejected": rejection}
        if now - detected_at >= self.confirm_timeout_ms:
            # persistent runner loss: clear alert + human disposition
            decision["requires_human"] = True
            decision["alerts"] = decision["alerts"] + [
                "stop_unconfirmed_escalation"]
        return decision


# ------------------------------------------------------- quota value builders
def _quota_max_us(cpu_milli, period_us):
    """Period-bound quota in µs. Floor division never grants MORE than
    purchased (a fractional µs rounds down)."""
    return cpu_milli * period_us // 1000


def _reject_unlimited(cpu_max_value):
    """Load-bearing guard: cpu.max 'max' means UNLIMITED; a restore must
    never lift the purchased bound (issue #18, plan-detail 3.4)."""
    if cpu_max_value.split(" ", 1)[0] == "max":
        raise QuotaRestoreError(
            "cpu.max restore must be the purchased quota, never 'max'")
    return cpu_max_value


def quota_restore(cpu_milli, period_us=QUOTA_PERIOD_US):
    """cgroup v2 cpu.max VALUE string restoring the PURCHASED cpu quota:
    "$MAX $PERIOD" with MAX = cpu_milli * period_us / 1000 µs of CPU time
    per period (e.g. 2000 milliCPU, period 100000µs -> "200000 100000").
    NEVER emits "max" — kernel cgroup-v2 defines it as unlimited and it
    would break the purchased CPU bound. Pure string builder: no sysfs
    writes."""
    _require_positive_int(cpu_milli, "cpu_milli")
    _require_positive_int(period_us, "period_us")
    value = f"{_quota_max_us(cpu_milli, period_us)} {period_us}"
    return _reject_unlimited(value)


def memory_max(bytes_count):
    """cgroup v2 memory.max VALUE string (plain byte count, never 'max')."""
    _require_positive_int(bytes_count, "memory_max bytes")
    return str(bytes_count)


def pids_max(n):
    """cgroup v2 pids.max VALUE string (plain process count)."""
    if type(n) is not int or n < 0:
        raise ValueError(f"pids_max must be an integer >= 0, got {n!r}")
    return str(n)


# ------------------------------------------------------- network failure
def network_failure_policy(stage, error=None, now=None):
    """Network cleanup/flush failure: BLOCK and ISOLATE for human
    decision. Volumes are KEPT; a data-deletion destroy is NEVER
    triggered from a cleanup failure (no destroy-with-delete semantics).
    No billing cutoff is derived from a network failure."""
    if not isinstance(stage, str) or not stage:
        raise ValueError("stage must be a non-empty string")
    return {
        "action": ISOLATION_ACTION,   # 'block_and_isolate', never 'destroy'
        "destroy": False,
        "keep_volumes": True,
        "stage": stage,               # 'cleanup' | 'flush' | ...
        "error": error,
        "display_state": "Unknown",
        "detected_at": now,
        "confirmed_stopped_at": None,
        "billing_cutoff_at": None,
        "alerts": [f"network_{stage}_failed"],
        "requires_human": True,
    }
