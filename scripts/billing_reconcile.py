#!/usr/bin/env python3
"""Billing reconciliation semantics for #28 (stdlib only, NO network).

Executable contract layer for issue #28 (對帳、重試與差額修正):

- ReconcileRow: one 對帳列 per (month, workspace, meter) with UNITS and
  MONEY in SEPARATE columns — local units (usage engine), provider
  aggregated units, in-flight events (sent, not yet aggregated), invoice
  amount minor, credits already applied. A credit is NEVER counted as
  units fixed (不把 credit 當 units 被修好了).
- diff(local, provider): distinguishes a QUANTITY mismatch (unit-level
  fix) from a MONETARY adjustment (credit note / draft amendment), and
  REUSES #27's pending-aggregate semantics — it raises
  stripe_bridge.ReconciliationBlocked against pending aggregates and
  never judges provisional numbers; in-flight events are a residual,
  not a mismatch, until the ready deadline passes.
- Recovery: lost-local-ack after API accept (re-query by idempotency id
  -> recovered, no double adjustment), partial success (per-meter state,
  failed retriable, confirmed never re-sent), cross-month settlement
  (the correction belongs to the ORIGINAL month as an adjustment line;
  the original invoice is immutable — a finalized invoice takes the
  adjustment on the NEXT invoice, a draft may be amended), and async
  delay (late-aggregated events surface in the in-flight column).
- Correction: unique deterministic correction_id, audit trail
  (who/when/evidence) and AT-MOST-ONCE effect — applying the same
  correction twice is a no-op, and rerunning the whole reconcile
  (--repair style) never duplicates credits/refunds.

Model basis (issue #28 cites these — the issue explicitly rejects the
old assumption that "Stripe 不能撤回也不能推負數"):

- meter event cancel/adjustment EXISTS (official endpoint):
  https://docs.stripe.com/api/billing/meter-event-adjustment/create
- usage recording handles NEGATIVE totals (official docs):
  https://docs.stripe.com/billing/subscriptions/usage-based/recording-usage-api

The provider capability is modeled as INJECTED actions
(meter_adjustment / credit_note / draft_amendment) plus an idempotent
status query keyed by the correction id (the idempotency key) — the
scripts/stripe_bridge.py OutboxRecorder idiom lifted to corrections.
Real-endpoint LIMITATIONS are noted on CorrectionProviderDouble and in
docs/contracts/billing-reconcile.md; real Stripe, real invoices and
test-mode validation are a runtime TODO. This library makes NO network
calls and holds NO keys.

First version: adjustments require HUMAN approval (approved_by) — no
automatic customer re-charging (不自動對客戶重複扣款). An unapproved run
only PROPOSES corrections; a rerun with approval applies exactly the
proposed set (deterministic ids).

Tests: scripts/test_billing_reconcile.py.
Spec: docs/contracts/billing-reconcile.md.
"""
import copy
import dataclasses

from stripe_bridge import ReconciliationBlocked  # pending semantics reused (#27)

SCHEMA_VERSION = 1

PROVIDER_STATUSES = ("pending", "ready")  # stripe_bridge.MeteringClient vocabulary
INVOICE_STATES = ("draft", "finalized")

CORRECTION_KINDS = ("meter_adjustment", "credit_note", "draft_amendment")
MONEY_KINDS = ("credit_note", "draft_amendment")
KIND_TARGETS = {"meter_adjustment": "meter",
                "credit_note": "next_invoice",
                "draft_amendment": "draft_line"}


def _require_int(value, name, minimum=None):
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")


# ------------------------------------------------------------------ rows
@dataclasses.dataclass(frozen=True)
class ReconcileRow:
    """One reconciliation line (對帳列) per (month, workspace, meter).

    UNITS and MONEY live in SEPARATE columns and never net against each
    other: credits_applied_minor can zero out the MONEY gap but NEVER
    changes the units columns or masks a quantity mismatch (issue #28
    acceptance: 不把 credit 當 units 被修好了).

    provider_units may carry the provider's PROVISIONAL aggregated value
    while provider_status == 'pending' (display only): it is NEVER
    judged until the aggregate is ready (stripe_bridge semantics).
    """

    month: str
    workspace_id: str
    meter: str
    currency: str
    local_units: int                    # usage engine confirmed/sealed units
    provider_units: object = None       # aggregated units (int once judged)
    provider_status: str = "pending"    # pending | ready
    in_flight_units: int = 0            # sent, not yet aggregated (async)
    invoice_minor: object = None        # invoiced amount, minor units
    invoice_state: object = None        # draft | finalized | None
    credits_applied_minor: int = 0      # net money already credited

    def __post_init__(self):
        for field in ("month", "workspace_id", "meter", "currency"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} must be a non-empty string")
        _require_int(self.local_units, "local_units", minimum=0)
        _require_int(self.in_flight_units, "in_flight_units", minimum=0)
        _require_int(self.credits_applied_minor, "credits_applied_minor")
        if self.provider_status not in PROVIDER_STATUSES:
            raise ValueError(f"provider_status must be one of {PROVIDER_STATUSES}")
        if self.provider_units is not None:
            _require_int(self.provider_units, "provider_units", minimum=0)
        if self.invoice_minor is not None:
            _require_int(self.invoice_minor, "invoice_minor", minimum=0)
        if (self.invoice_state is not None
                and self.invoice_state not in INVOICE_STATES):
            raise ValueError(f"invoice_state must be one of {INVOICE_STATES} or None")

    def to_dict(self):
        return dataclasses.asdict(self)


# ------------------------------------------------------------------ diff
def _judgeable(provider):
    """Pending gate (mutation point for the pending guard): only READY
    aggregates may be judged — stripe_bridge.MeteringClient semantics,
    exception class reused from there."""
    return provider.get("status") == "ready"


def _units_gap(local_units, provider_units, in_flight_units):
    """Residual units gap AFTER accounting for in-flight events (sent,
    not yet aggregated — async-delay tolerance). MONEY (credits) NEVER
    enters this computation (mutation point for the column-separation
    guard: a mutant that subtracts credits here masks a real quantity
    mismatch with a credit)."""
    return local_units - (provider_units + in_flight_units)


def _money_gap(local_minor, invoice_minor, credits_minor):
    """Net monetary gap: what the invoice still charges after credits
    already applied, against the locally expected amount."""
    return local_minor - (invoice_minor - credits_minor)


def diff(local, provider):
    """Judge ONE meter line.

    local: {'units', 'minor'} (usage engine output). provider:
    {'status', 'units', 'in_flight_units'?, 'invoice_minor'?,
    'credits_minor'?}.

    Raises ReconciliationBlocked (from stripe_bridge) when the provider
    aggregate is pending — NEVER diffs against pending aggregates, even
    ones exposing provisional numbers. The caller layers the ready
    deadline tolerance on top (BillingReconciler.reconcile).

    Returns {'kind', 'units_gap', 'money_gap', 'in_flight_units'} with
    kind one of:

    - 'units_mismatch': quantity gap -> unit-level fix (provider meter
      adjustment); money follows re-aggregation, never an instant credit;
    - 'money_mismatch': amounts differ with units equal -> monetary
      adjustment (credit note / draft amendment) — a credit can never
      repair units and a unit fix is never booked as a credit;
    - 'no_invoice': units equal, nothing invoiced yet;
    - 'match'.
    """
    if not isinstance(local, dict) or not isinstance(provider, dict):
        raise ValueError("local and provider must be dicts")
    _require_int(local.get("units"), "local.units", minimum=0)
    _require_int(local.get("minor"), "local.minor", minimum=0)
    status = provider.get("status")
    if status not in PROVIDER_STATUSES:
        raise ValueError(f"provider.status must be one of {PROVIDER_STATUSES}")
    if not _judgeable(provider):
        raise ReconciliationBlocked(
            "usage aggregate is still pending; never diff against a "
            "pending aggregate (stripe_bridge semantics) — poll to ready")
    _require_int(provider.get("units"), "provider.units", minimum=0)
    in_flight = provider.get("in_flight_units", 0)
    _require_int(in_flight, "provider.in_flight_units", minimum=0)
    credits = provider.get("credits_minor", 0)
    _require_int(credits, "provider.credits_minor")
    invoice_minor = provider.get("invoice_minor")
    if invoice_minor is not None:
        _require_int(invoice_minor, "provider.invoice_minor", minimum=0)
    units_gap = _units_gap(local["units"], provider["units"], in_flight)
    money_gap = (None if invoice_minor is None
                 else _money_gap(local["minor"], invoice_minor, credits))
    if units_gap != 0:
        kind = "units_mismatch"
    elif money_gap is None:
        kind = "no_invoice"
    elif money_gap != 0:
        kind = "money_mismatch"
    else:
        kind = "match"
    return {"kind": kind, "units_gap": units_gap, "money_gap": money_gap,
            "in_flight_units": in_flight}


def local_lines(view):
    """Adapt a usage_engine.usage_view()/price() result into the local
    line map reconcile() consumes. Confirmed AND sealed lines count as
    local truth (sealed lines are the frozen local record of a closed
    month); estimates (試用估價) and past adjustment lines do not."""
    lines = {}
    for line in view.get("lines", ()):
        if line.get("kind") not in ("confirmed", "sealed"):
            continue
        key = (line["month"], line["workspace_id"], line["meter"])
        row = lines.setdefault(key, {"units": 0, "minor": 0,
                                     "currency": line["currency"]})
        row["units"] += line["units"]
        row["minor"] += line["minor"]
    return lines


# ------------------------------------------------------------ corrections
@dataclasses.dataclass(frozen=True)
class Correction:
    """One remediation with AT MOST ONCE effect and a full audit trail
    (who: approved_by, when: approved_at_ms, evidence: the
    reconciliation row snapshot that motivated it).

    Column separation is STRUCTURAL: a meter_adjustment carries units
    and NO money; a credit_note / draft_amendment carries money and NO
    units — a credit can never be booked as units fixed.

    minor sign: positive = money back to the customer (credit),
    negative = additional charge. Both require the same human approval
    in the first version (不自動對客戶重複扣款).
    """

    correction_id: str
    kind: str
    month: str
    workspace_id: str
    meter: str
    currency: str
    units: int = 0
    minor: int = 0
    target: str = "meter"          # meter | draft_line | next_invoice
    approved_by: str = ""          # '' = proposed only, never applied
    approved_at_ms: int = 0
    evidence: dict = dataclasses.field(default_factory=dict)
    reason: str = ""

    def __post_init__(self):
        for field in ("correction_id", "month", "workspace_id", "meter",
                      "currency"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} must be a non-empty string")
        if self.kind not in CORRECTION_KINDS:
            raise ValueError(f"kind must be one of {CORRECTION_KINDS}")
        if self.target != KIND_TARGETS[self.kind]:
            raise ValueError(f"target for {self.kind} must be "
                             f"{KIND_TARGETS[self.kind]!r}")
        _require_int(self.units, "units")
        _require_int(self.minor, "minor")
        _require_int(self.approved_at_ms, "approved_at_ms", minimum=0)
        if not isinstance(self.approved_by, str):
            raise ValueError("approved_by must be a string")
        if not isinstance(self.evidence, dict):
            raise ValueError("evidence must be a dict")
        if not isinstance(self.reason, str):
            raise ValueError("reason must be a string")
        if self.kind == "meter_adjustment" and (self.units == 0 or self.minor != 0):
            raise ValueError("meter_adjustment fixes UNITS only: units != 0, "
                             "minor == 0 — a credit is never units fixed")
        if self.kind in MONEY_KINDS and (self.minor == 0 or self.units != 0):
            raise ValueError(f"{self.kind} fixes MONEY only: minor != 0, "
                             "units == 0")

    def to_dict(self):
        return dataclasses.asdict(self)


def to_adjustment_line(correction):
    """Map a MONEY correction to a usage_engine adjustment line.

    Cross-month rule (issue #28): the correction BELONGS to the original
    month (period) as an adjustment line — the original invoice stays
    immutable; a finalized invoice receives the adjustment on the NEXT
    invoice. Sign flip: correction.minor is customer-favor-positive,
    the adjustment line minor is invoice-negative for a credit.
    """
    if correction.kind not in MONEY_KINDS:
        raise ValueError("unit-level fixes go through provider meter "
                         "adjustments, not invoice lines")
    return {"period": correction.month,
            "workspace_id": correction.workspace_id,
            "currency": correction.currency,
            "meter": correction.meter,
            "units": 0,
            "minor": -correction.minor,
            "reason": correction.reason or correction.correction_id}


class CorrectionsLedger:
    """At-most-once corrections store keyed by correction_id.

    apply(correction, action) ordering:

    1. human approval gate — an empty approved_by is refused outright
       (first version: no automatic adjustments);
    2. id already in the ledger -> 'already_applied', action NOT called
       (applying the same correction twice is a no-op);
    3. status query by the correction id (the provider idempotency key):
       a receipt means the provider accepted it earlier and the LOCAL
       ACK WAS LOST -> recorded as 'recovered', action NOT called (no
       double adjustment);
    4. only then run the injected action and record 'applied'. An action
       that raises records NOTHING: the correction stays retriable
       (partial success / lost-ack window).

    Persistence (to_dict/from_dict) keeps the full audit trail — a
    daily-backup restore loses no evidence, and a --repair rerun on the
    restored ledger cannot duplicate effects (ids are deterministic).

    The named _-prefixed helpers are deliberate mutation points for the
    guard tests (scripts/test_billing_reconcile.py), not extension
    points.
    """

    def __init__(self, status_query=None):
        self._status_query = status_query or (lambda correction_id: None)
        self._entries = {}

    # -------------------------------------------------- mutation points
    def _known(self, correction_id):
        """Apply-once dedupe lookup (mutation point for the dedupe guard)."""
        return correction_id in self._entries

    def _remote_receipt(self, correction):
        """Lost-ack recovery: an idempotent READ keyed by the correction
        id — never a fresh write (mutation point for the read-not-write
        guard)."""
        return self._status_query(correction.correction_id)

    def _approval_ok(self, correction):
        """First version: HUMAN approval required (mutation point for
        the approval guard — 不自動對客戶重複扣款)."""
        return bool(correction.approved_by)

    # ------------------------------------------------------------- apply
    def _record(self, correction, receipt, status, now_ms):
        self._entries[correction.correction_id] = {
            "correction_id": correction.correction_id,
            "correction": correction.to_dict(),
            "receipt": copy.deepcopy(receipt),
            "status": status,
            "applied_at_ms": now_ms,
        }

    def apply(self, correction, action, now_ms=0):
        """Apply at most once. Raises ValueError for a non-Correction, a
        non-callable action or an unapproved correction; an action
        exception propagates with NOTHING recorded (retriable)."""
        _require_int(now_ms, "now_ms", minimum=0)
        if not isinstance(correction, Correction):
            raise ValueError("correction must be a Correction")
        if not callable(action):
            raise ValueError("action must be callable")
        if not self._approval_ok(correction):
            raise ValueError("refused: first-version corrections require "
                             "human approval (approved_by)")
        if self._known(correction.correction_id):
            entry = self._entries[correction.correction_id]
            return {"correction_id": correction.correction_id,
                    "status": "already_applied",
                    "receipt": copy.deepcopy(entry["receipt"]),
                    "changed": False}
        receipt = self._remote_receipt(correction)
        if receipt is not None:  # provider accepted it; local ack was lost
            self._record(correction, receipt, "recovered", now_ms)
            return {"correction_id": correction.correction_id,
                    "status": "recovered",
                    "receipt": copy.deepcopy(receipt), "changed": True}
        receipt = action(correction)  # injected provider action
        self._record(correction, receipt, "applied", now_ms)
        return {"correction_id": correction.correction_id,
                "status": "applied",
                "receipt": copy.deepcopy(receipt), "changed": True}

    # ------------------------------------------------------ introspection
    def _view(self, entry):
        return copy.deepcopy(entry)

    def entry(self, correction_id):
        entry = self._entries.get(correction_id)
        return self._view(entry) if entry is not None else None

    def entries(self):
        return [self._view(e) for e in self._entries.values()]

    def ids(self):
        return sorted(self._entries)

    def credits_for(self, month, workspace_id, meter):
        """Net money already applied toward this line (customer-favor
        positive) — feeds the row's credits_applied_minor column and
        nets out of the next money-gap computation, so a rerun never
        re-credits the same difference."""
        return sum(e["correction"]["minor"] for e in self._entries.values()
                   if e["correction"]["kind"] in MONEY_KINDS
                   and (e["correction"]["month"], e["correction"]["workspace_id"],
                        e["correction"]["meter"]) == (month, workspace_id, meter))

    # -------------------------------------------------------- persistence
    def to_dict(self):
        return {"schema_version": SCHEMA_VERSION,
                "entries": self.entries()}

    @classmethod
    def from_dict(cls, data, status_query=None):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported persisted schema_version")
        ledger = cls(status_query)
        for entry in data["entries"]:
            ledger._entries[entry["correction_id"]] = copy.deepcopy(entry)
        return ledger


class CorrectionProviderDouble:
    """In-memory provider stand-in for correction actions (test-mode
    semantics, no network). Each endpoint takes effect AT MOST ONCE per
    correction id (the idempotency key) — same contract as
    stripe_bridge.ProviderDouble; status() is an idempotent READ.

    Models the endpoints the issue cites:

    - meter_adjustment -> POST /v1/billing/meter_event_adjustments
      (cancel a reported event / reset a range;
      https://docs.stripe.com/api/billing/meter-event-adjustment/create).
      LIMITATION (real-endpoint note): Stripe can cancel by event id /
      event identifier or reset a whole range — it cannot rewrite an
      arbitrary aggregated total; mapping an units_gap to the right
      cancel/resend calls is the injected action's job at runtime.
    - credit_note / draft_amendment -> customer credit / next-invoice
      adjustment; usage recording supports NEGATIVE totals
      (https://docs.stripe.com/billing/subscriptions/usage-based/recording-usage-api).
    """

    def __init__(self):
        self._receipts = {}
        self.effects = 0     # side effects — one per unique correction id
        self.calls = {}      # kind -> write attempts seen (dedupe visible)

    def _act(self, kind, correction):
        self.calls[kind] = self.calls.get(kind, 0) + 1
        cid = correction.correction_id
        if cid not in self._receipts:
            self.effects += 1
            self._receipts[cid] = {
                "provider_id": f"{kind}_{self.effects:04d}",
                "idempotency_key": cid, "kind": kind,
                "month": correction.month, "workspace_id": correction.workspace_id,
                "units": correction.units, "minor": correction.minor}
        return copy.deepcopy(self._receipts[cid])

    def meter_adjustment(self, correction):
        return self._act("meter_adjustment", correction)

    def credit_note(self, correction):
        return self._act("credit_note", correction)

    def draft_amendment(self, correction):
        return self._act("draft_amendment", correction)

    def status(self, correction_id):
        receipt = self._receipts.get(correction_id)
        return copy.deepcopy(receipt) if receipt is not None else None

    def actions_map(self):
        return {"meter_adjustment": self.meter_adjustment,
                "credit_note": self.credit_note,
                "draft_amendment": self.draft_amendment}


# ------------------------------------------------------------ reconciler
class BillingReconciler:
    """Drive one reconciliation pass over local x provider line maps.

    reconcile() is a PURE function of its inputs plus the ledger:
    rerunning it with the same inputs (--repair style) proposes the SAME
    deterministic correction ids; the ledger dedupes them and money
    already credited nets out of the next money gap — no duplicate
    credits/refunds, ever.
    """

    def __init__(self, ledger, actions):
        if not isinstance(ledger, CorrectionsLedger):
            raise ValueError("ledger must be a CorrectionsLedger")
        if not isinstance(actions, dict):
            raise ValueError("actions must be a dict of kind -> callable")
        self._ledger = ledger
        self._actions = dict(actions)

    # -------------------------------------------------- mutation points
    def _correction_id(self, month, workspace_id, meter, suffix):
        """Deterministic id from the line identity + fix kind (mutation
        point for the determinism guard): the same mismatch yields the
        same id on every run — that is what makes reruns at-most-once."""
        return f"corr:{month}:{workspace_id}:{meter}:{suffix}"

    def _money_target(self, invoice_state):
        """Un-finalized vs finalized split (mutation point for the
        finalized-immutability guard): a DRAFT invoice may be amended in
        place; a FINALIZED invoice is immutable — the adjustment lands
        on the NEXT invoice, the original is never touched."""
        if invoice_state == "draft":
            return "draft_amendment", "draft_line"
        return "credit_note", "next_invoice"

    def _deadline_passed(self, now_ms, ready_deadline_ms):
        """Async-delay tolerance (mutation point for the deadline
        guard): a pending aggregate is NEVER a mismatch; only once the
        ready deadline has passed does it escalate (stalled). A None
        deadline means unlimited tolerance until the aggregate is ready."""
        return ready_deadline_ms is not None and now_ms >= ready_deadline_ms

    # ------------------------------------------------------------- build
    def _build_correction(self, row, judgment, approved_by, now_ms):
        evidence = {**row.to_dict(), "units_gap": judgment["units_gap"],
                    "money_gap": judgment["money_gap"]}
        common = dict(month=row.month, workspace_id=row.workspace_id,
                      meter=row.meter, currency=row.currency,
                      approved_by=approved_by, approved_at_ms=now_ms,
                      evidence=evidence)
        if judgment["kind"] == "units_mismatch":
            cid = self._correction_id(row.month, row.workspace_id,
                                      row.meter, "units")
            return Correction(cid, "meter_adjustment", target="meter",
                              units=judgment["units_gap"],
                              reason=f"units gap {judgment['units_gap']}: "
                                     "cancel/resend via provider meter adjustment",
                              **common)
        kind, target = self._money_target(row.invoice_state)
        suffix = "money-draft" if kind == "draft_amendment" else "money-next"
        cid = self._correction_id(row.month, row.workspace_id, row.meter, suffix)
        return Correction(cid, kind, target=target,
                          minor=-judgment["money_gap"],
                          reason=f"money gap {judgment['money_gap']} -> {target}",
                          **common)

    def _dispatch(self, entry, row, judgment, approved_by, now_ms):
        """Per-meter, failure-isolated correction application."""
        try:
            correction = self._build_correction(row, judgment, approved_by, now_ms)
        except ValueError as exc:
            entry.update(correction_id=None, apply_status="failed",
                         error=f"invalid correction: {exc}")
            return
        entry["correction_id"] = correction.correction_id
        if not approved_by:
            entry["apply_status"] = "proposed"  # 人工批准: nothing applied
            return
        action = self._actions.get(correction.kind)
        if action is None:
            entry.update(apply_status="failed",
                         error=f"no action injected for {correction.kind}")
            return
        try:
            receipt = self._ledger.apply(correction, action, now_ms=now_ms)
        except Exception as exc:  # per-meter isolation; stays retriable
            entry.update(apply_status="failed", error=str(exc))
            return
        entry["apply_status"] = receipt["status"]
        entry["receipt"] = copy.deepcopy(receipt["receipt"])

    # ------------------------------------------------------------ pass
    def reconcile(self, local, provider, in_flight=None, *, now_ms=0,
                  ready_deadline_ms=None, approved_by=""):
        """One reconciliation pass.

        local / provider: {(month, workspace_id, meter): line-dict}.
        A local line is {'units', 'minor', 'currency'} (usage engine
        output via local_lines()); a provider line is {'status',
        'units', 'currency', 'invoice_minor', 'invoice_state'}.
        in_flight: same keys -> sent-not-yet-aggregated units.

        Without approved_by, corrections are PROPOSED only (first
        version: human approval, 不自動對客戶重複扣款). With approved_by
        they are applied through the ledger (at-most-once). Per-meter
        independence: one meter's action failure never affects the
        others — the failed meter stays retriable, confirmed meters are
        never re-sent.
        """
        _require_int(now_ms, "now_ms", minimum=0)
        if not isinstance(approved_by, str):
            raise ValueError("approved_by must be a string")
        in_flight = in_flight or {}
        rows, summary = [], {}
        for key in sorted(set(local) | set(provider)):
            month, ws, meter = key
            lline = local.get(key) or {}
            pline = provider.get(key) or {}
            credits = self._ledger.credits_for(month, ws, meter)
            row = ReconcileRow(
                month=month, workspace_id=ws, meter=meter,
                currency=pline.get("currency") or lline.get("currency") or "",
                local_units=lline.get("units", 0),
                provider_units=pline.get("units"),
                provider_status=pline.get("status", "pending"),
                in_flight_units=in_flight.get(key, 0),
                invoice_minor=pline.get("invoice_minor"),
                invoice_state=pline.get("invoice_state"),
                credits_applied_minor=credits)
            entry = {"row": row.to_dict()}
            try:
                judgment = diff(
                    {"units": lline.get("units", 0), "minor": lline.get("minor", 0)},
                    {"status": row.provider_status, "units": pline.get("units"),
                     "in_flight_units": row.in_flight_units,
                     "invoice_minor": row.invoice_minor,
                     "credits_minor": credits})
            except ReconciliationBlocked:
                # async aggregation: never a mismatch; escalate only past
                # the ready deadline
                kind = ("stalled" if self._deadline_passed(now_ms, ready_deadline_ms)
                        else "awaiting_aggregation")
                entry.update(kind=kind, units_gap=None, money_gap=None)
            else:
                entry.update(kind=judgment["kind"],
                             units_gap=judgment["units_gap"],
                             money_gap=judgment["money_gap"])
                if judgment["kind"] in ("units_mismatch", "money_mismatch"):
                    self._dispatch(entry, row, judgment, approved_by, now_ms)
            summary[entry["kind"]] = summary.get(entry["kind"], 0) + 1
            rows.append(entry)
        return {"rows": rows, "summary": summary,
                "ledger_ids": sorted(self._ledger.ids())}
