#!/usr/bin/env python3
"""Usage engine slice for #26 (stdlib only, no Runner, no Stripe, no DB).

Turns confirmed projection segments (resource_events projection shape:
start_ms, end_ms, quantity{meter: int}, certainty?) into exact per-line
minor-unit amounts per docs/adr/usage-ledger.md §4 (normative):

- price = integrated units x numerator_minor over denominator_meter_units,
  accumulated as exact Fractions; integer milliseconds, integer quantities;
- grouping is UTC month x workspace x currency x meter; segments are split
  at UTC month boundaries AND rate-version boundaries so a new rate never
  covers old time and nothing is double counted;
- each LINE is rounded half-up exactly once at the end (abs-then-sign on
  negatives); totals sum already-rounded lines;
- gaps in rate coverage are UNPRICED, never silently zero;
- uncertain (Lost) segments never enter confirmed totals: they surface as a
  separate「至少 X＋待核對區間」trial estimate labeled 試用估價;
- sealed billing periods are never recomputed: their frozen lines pass
  through untouched and late corrections arrive as adjustment lines.

Integer and Fraction arithmetic only — enforced by a source scan in
scripts/test_usage_engine.py. usage_view() is the single source consumed by
API, Web and CLI; render_text() formats that view for terminals.
"""
import calendar
import dataclasses
from datetime import datetime, timezone
from fractions import Fraction

GIB_BYTES = 1 << 30  # GiB is 2^30 bytes, no GB GiB mixing (usage-ledger §4)
ROUNDING_POLICY = "round_half_up"
CERTAINTIES = ("confirmed", "uncertain")

METER_UNITS = {  # ADR §2 meter units, integer quantity x ms
    "cpu_reserved": "milliCPU·ms",
    "memory_reserved": "byte·ms",
    "volume_provisioned": "byte·ms",
    "snapshot_stored": "byte·ms",
}

ENTRY_FIELDS = ("rate_id", "version", "currency", "meter", "state",
                "effective_from_ms", "effective_to_ms", "numerator_minor",
                "denominator_meter_units", "rounding_policy")


def _require_int(value, name, minimum=None):
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")


def round_half_up(value):
    """Round an exact rational to an integer, half away from zero.

    usage-ledger §4: negative adjustments round the absolute value half-up
    then restore the sign; language-default bankers rounding is forbidden.
    """
    sign = -1 if value < 0 else 1
    magnitude = abs(value)
    return sign * ((2 * magnitude.numerator + magnitude.denominator)
                   // (2 * magnitude.denominator))


def _month_key(ms):
    dt = datetime.fromtimestamp(ms // 1000, timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def _month_boundaries(start_ms, end_ms):
    """UTC month-start instants strictly inside the half-open segment."""
    dt = datetime.fromtimestamp(start_ms // 1000, timezone.utc)
    year, month, cuts = dt.year, dt.month, []
    while True:
        month += 1
        if month == 13:
            year, month = year + 1, 1
        boundary = calendar.timegm((year, month, 1, 0, 0, 0)) * 1000
        if boundary >= end_ms:
            return cuts
        if boundary > start_ms:
            cuts.append(boundary)


@dataclasses.dataclass(frozen=True)
class RateEntry:
    """One immutable rate-card row (ADR §4). effective_to_ms None = open."""

    rate_id: str
    version: int
    currency: str
    meter: str
    state: object = None  # None prices every state of the meter
    effective_from_ms: int = 0
    effective_to_ms: object = None
    numerator_minor: int = 0
    denominator_meter_units: int = 1
    rounding_policy: str = ROUNDING_POLICY


class RateCard:
    """Append-only versioned rate card. History is immutable once effective."""

    def __init__(self):
        self._entries = []

    # ---------------------------------------------------------- validation
    def _coerce(self, row):
        if isinstance(row, RateEntry):
            entry = row
        elif isinstance(row, dict):
            unknown = set(row) - set(ENTRY_FIELDS)
            if unknown:
                raise ValueError(f"unknown rate fields {sorted(unknown)}")
            entry = RateEntry(**row)
        else:
            raise ValueError("rate row must be a RateEntry or dict")
        for field in ("rate_id", "currency", "meter"):
            if not isinstance(getattr(entry, field), str) or not getattr(entry, field):
                raise ValueError(f"{field} must be a non-empty string")
        if entry.state is not None and not isinstance(entry.state, str):
            raise ValueError("state must be a string or None")
        _require_int(entry.version, "version", minimum=1)
        _require_int(entry.effective_from_ms, "effective_from_ms", minimum=0)
        if entry.effective_to_ms is not None:
            _require_int(entry.effective_to_ms, "effective_to_ms", minimum=0)
            if entry.effective_to_ms <= entry.effective_from_ms:
                raise ValueError("effective_to_ms must be after effective_from_ms")
        _require_int(entry.numerator_minor, "numerator_minor", minimum=0)
        _require_int(entry.denominator_meter_units, "denominator_meter_units",
                     minimum=1)
        if entry.rounding_policy != ROUNDING_POLICY:
            raise ValueError(f"unsupported rounding_policy "
                             f"{entry.rounding_policy!r} (only {ROUNDING_POLICY})")
        return entry

    @staticmethod
    def _price_key(entry):
        return (entry.currency, entry.meter, entry.state)

    @staticmethod
    def _reaches(entry, at_ms):
        return entry.effective_to_ms is None or at_ms < entry.effective_to_ms

    @classmethod
    def _overlap(cls, a, b):
        def past(x, y):  # x's range extends past y's start
            return y.effective_from_ms < x.effective_to_ms if x.effective_to_ms is not None else True
        return past(a, b) and past(b, a)

    def _check_history_immutable(self, existing, entry, now_ms):
        """An already-effective (or past) row can never be edited: repricing
        history is forbidden. Mutation point for the history guard test."""
        if existing.effective_from_ms <= now_ms:
            raise ValueError(
                f"{entry.rate_id} v{entry.version} took effect at "
                f"{existing.effective_from_ms} (<= now {now_ms}); already-effective "
                f"rows are immutable history — append a new version instead")

    # -------------------------------------------------------------- upsert
    def upsert(self, row, now_ms):
        """Insert a rate row or replace an entirely future-dated one.

        Same identity (rate_id, version) with identical content is an
        idempotent no-op; with different content it is refused once the
        stored row has taken effect. Inserting a later-starting row closes
        an open-ended predecessor at the new start (a future-only truncation
        that never reprices the past). Earlier- or same-starting overlaps
        are hard errors.
        """
        _require_int(now_ms, "now_ms", minimum=0)
        entry = self._coerce(row)
        existing = self._find(entry.rate_id, entry.version)
        if existing is not None:
            if existing == entry:
                return {"status": "unchanged", "closed": []}
            self._check_history_immutable(existing, entry, now_ms)
            self._entries.remove(existing)
        closed = self._insert(entry, now_ms)
        return {"status": "appended", "closed": closed}

    def _insert(self, entry, now_ms):
        closed = []
        for other in list(self._entries):
            if self._price_key(other) != self._price_key(entry):
                continue
            if not self._overlap(other, entry):
                continue
            if other.effective_from_ms >= entry.effective_from_ms:
                raise ValueError(
                    f"effective range overlaps {other.rate_id} v{other.version} "
                    f"for price key {self._price_key(entry)} (ADR §4: no overlap)")
            if entry.effective_from_ms <= now_ms:
                raise ValueError("cannot cut into an already-effective row; "
                                 "new versions must be future-dated")
            if entry.effective_to_ms is not None and (other.effective_to_ms is None
                                                      or entry.effective_to_ms < other.effective_to_ms):
                raise ValueError(
                    f"would punch an unpriced hole into {other.rate_id} v{other.version}; "
                    f"later versions must extend to or past the predecessor's end")
            # future-only truncation: the predecessor hands over at entry's start
            self._entries.remove(other)
            self._entries.append(dataclasses.replace(
                other, effective_to_ms=entry.effective_from_ms))
            closed.append(f"{other.rate_id} v{other.version}")
        self._entries.append(entry)
        return closed

    def _find(self, rate_id, version):
        for entry in self._entries:
            if (entry.rate_id, entry.version) == (rate_id, version):
                return entry
        return None

    # ------------------------------------------------------------- lookups
    def _covers(self, entry, at_ms):
        return entry.effective_from_ms <= at_ms and self._reaches(entry, at_ms)

    def lookup(self, meter, at_ms, state=None):
        """Rate in effect for a meter at an instant; most specific state
        wins (exact match beats a state-None wildcard). Two equally specific
        matches (e.g. two currencies) are an ambiguity error, not a guess."""
        _require_int(at_ms, "at_ms", minimum=0)
        matches = [e for e in self._entries if e.meter == meter
                   and self._covers(e, at_ms) and e.state in (state, None)]
        specific = [e for e in matches if e.state == state]
        if len(specific) > 1:
            raise ValueError(f"ambiguous rates for {meter} at {at_ms}: "
                             f"{[e.rate_id + ' v' + str(e.version) for e in specific]}")
        if specific:
            return specific[0]
        wild = [e for e in matches if e.state is None]
        if len(wild) > 1:
            raise ValueError(f"ambiguous rates for {meter} at {at_ms}: "
                             f"{[e.rate_id + ' v' + str(e.version) for e in wild]}")
        return wild[0] if wild else None

    def boundaries(self, meter, state, start_ms, end_ms):
        """Rate-version cut instants strictly inside a half-open interval."""
        cuts = set()
        for entry in self._entries:
            if entry.meter != meter or entry.state not in (state, None):
                continue
            for at in (entry.effective_from_ms, entry.effective_to_ms):
                if at is not None and start_ms < at < end_ms:
                    cuts.add(at)
        return sorted(cuts)

    def entries(self, meter=None):
        return tuple(e for e in self._entries
                     if meter is None or e.meter == meter)

    # ------------------------------------------------------ provider sync
    def sync_from_provider(self, rows, now_ms):
        """Append-only provider sync (Stripe contract, issue #26).

        Future-dated new versions append (closing open predecessors). Rows
        that already took effect, rows differing from stored history, and
        overlapping rows are flagged as conflicts — history is never
        rewritten and the present is never repriced by a sync. Returns a
        report; never raises for row-level problems.
        """
        report = {"appended": [], "unchanged": [], "conflicts": []}

        def identity(entry):
            return {"rate_id": entry.rate_id, "version": entry.version}

        for row in rows:
            try:
                entry = self._coerce(row)
            except ValueError as exc:
                report["conflicts"].append({"rate_id": None, "version": None,
                                            "reason": f"invalid row: {exc}"})
                continue
            existing = self._find(entry.rate_id, entry.version)
            if existing is not None:
                if existing == entry:
                    report["unchanged"].append(identity(entry))
                else:
                    report["conflicts"].append({
                        **identity(entry),
                        "reason": "differs from stored history; sync never rewrites"})
                continue
            if entry.effective_from_ms <= now_ms:
                report["conflicts"].append({
                    **identity(entry),
                    "reason": "not future-dated; would reprice already-effective usage"})
                continue
            try:
                self.upsert(entry, now_ms)
            except ValueError as exc:
                report["conflicts"].append({**identity(entry), "reason": str(exc)})
            else:
                report["appended"].append(identity(entry))
        return report


# ------------------------------------------------------------------ pricing
def _outside_sealed(start_ms, end_ms, sealed_periods):
    """Clip a span out of every sealed half-open window (mutation point for
    the sealed-immutability guard: sealed periods are never recomputed)."""
    spans = [(start_ms, end_ms)]
    for period in sealed_periods:
        p0, p1 = period["start_ms"], period["end_ms"]
        clipped = []
        for s, e in spans:
            if e <= p0 or s >= p1:
                clipped.append((s, e))
                continue
            if s < p0:
                clipped.append((s, p0))
            if p1 < e:
                clipped.append((p1, e))
        spans = clipped
    return [(s, e) for s, e in spans if s < e]


def _segment_pieces(segment, period, sealed_periods):
    """Flatten one segment into per-meter pieces clipped to the period and
    outside sealed windows. Validates the trust boundary: integer times and
    quantities, known certainty."""
    if not isinstance(segment, dict):
        raise ValueError("segment must be a dict")
    start, end = segment.get("start_ms"), segment.get("end_ms")
    _require_int(start, "segment.start_ms", minimum=0)
    _require_int(end, "segment.end_ms", minimum=0)
    if start > end:
        raise ValueError("segment.start_ms must be <= segment.end_ms")
    if period is not None:
        start, end = max(start, period[0]), min(end, period[1])
        if start >= end:
            return
    certainty = segment.get("certainty", "confirmed")
    if certainty not in CERTAINTIES:
        raise ValueError(f"certainty must be one of {CERTAINTIES}")
    workspace = segment.get("workspace_id", "default")
    state = segment.get("state")
    quantity = segment.get("quantity")
    if not isinstance(quantity, dict) or not quantity:
        return
    for meter, value in sorted(quantity.items()):
        _require_int(value, f"segment.quantity[{meter!r}]", minimum=0)
        if value == 0:
            continue
        for s, e in _outside_sealed(start, end, sealed_periods):
            yield {"start_ms": s, "end_ms": e, "meter": meter, "quantity": value,
                   "workspace_id": workspace, "certainty": certainty, "state": state}


def price(card, segments, period=None, sealed_periods=(), adjustments=()):
    """Price confirmed segments into rounded per-line minor units (ADR §4).

    Returns internal computation: lines (confirmed, sealed, adjustment
    kinds), totals per currency (sum of rounded lines), unpriced coverage
    gaps, the uncertain trial estimate, and sealed period ids.
    """
    sealed_periods = [dict(p) for p in sealed_periods]
    lines_exact = {}   # (month, ws, currency, meter) -> {units, exact, rates}
    unpriced = {}      # (month, ws, meter) -> {units, certainty}
    estimate = {}      # (ws, currency, meter) -> {units, exact, rates}
    uncertain_ms = 0

    for segment in segments:
        for piece in _segment_pieces(segment, period, sealed_periods):
            start, end = piece["start_ms"], piece["end_ms"]
            cuts = set(_month_boundaries(start, end))
            cuts.update(card.boundaries(piece["meter"], piece["state"], start, end))
            points = [start] + sorted(c for c in cuts if start < c < end) + [end]
            for s, e in zip(points, points[1:]):
                units = piece["quantity"] * (e - s)
                month = _month_key(s)
                if piece["certainty"] == "uncertain":
                    uncertain_ms += e - s
                    entry = card.lookup(piece["meter"], s, piece["state"])
                    if entry is None:
                        key = (month, piece["workspace_id"], piece["meter"])
                        row = unpriced.setdefault(key, {"units": 0, "certainty": "uncertain"})
                        row["units"] += units
                        continue
                    key = (piece["workspace_id"], entry.currency, piece["meter"])
                    row = estimate.setdefault(key, {"units": 0,
                                                    "exact": Fraction(0), "rates": set()})
                    row["units"] += units
                    row["exact"] += Fraction(units * entry.numerator_minor,
                                             entry.denominator_meter_units)
                    row["rates"].add((entry.rate_id, entry.version))
                    continue
                entry = card.lookup(piece["meter"], s, piece["state"])
                if entry is None:  # gap: unpriced, never a silent zero
                    key = (month, piece["workspace_id"], piece["meter"])
                    row = unpriced.setdefault(key, {"units": 0, "certainty": "confirmed"})
                    row["units"] += units
                    continue
                key = (month, piece["workspace_id"], entry.currency, piece["meter"])
                row = lines_exact.setdefault(key, {"units": 0,
                                                   "exact": Fraction(0), "rates": set()})
                row["units"] += units
                row["exact"] += Fraction(units * entry.numerator_minor,
                                         entry.denominator_meter_units)
                row["rates"].add((entry.rate_id, entry.version))

    lines, totals = [], {}
    for key in sorted(lines_exact):
        month, ws, currency, meter = key
        row = lines_exact[key]
        lines.append({"kind": "confirmed", "month": month, "workspace_id": ws,
                      "currency": currency, "meter": meter, "units": row["units"],
                      "minor": round_half_up(row["exact"]),
                      "rate_versions": sorted(f"{rid} v{v}" for rid, v in row["rates"])})
        totals[currency] = totals.get(currency, 0) + lines[-1]["minor"]

    for sealed in sealed_periods:  # frozen lines pass through untouched
        for line in sealed.get("lines", ()):
            line = {**line, "kind": "sealed"}
            lines.append(line)
            totals[line["currency"]] = totals.get(line["currency"], 0) + line["minor"]

    adjustment_lines, adjustment_totals = [], {}
    for adjustment in adjustments:  # never mutate sealed totals (ADR §3)
        line = {"kind": "adjustment", "month": adjustment["period"],
                "workspace_id": adjustment.get("workspace_id", "default"),
                "currency": adjustment["currency"], "meter": adjustment["meter"],
                "units": adjustment.get("units", 0), "minor": adjustment["minor"],
                "reason": adjustment.get("reason", "")}
        adjustment_lines.append(line)
        adjustment_totals[line["currency"]] = (adjustment_totals.get(line["currency"], 0)
                                               + line["minor"])
    lines.extend(adjustment_lines)

    estimate_lines, estimate_totals = [], {}
    for key in sorted(estimate):
        ws, currency, meter = key
        row = estimate[key]
        estimate_lines.append({"workspace_id": ws, "currency": currency,
                               "meter": meter, "units": row["units"],
                               "minor": round_half_up(row["exact"]),
                               "rate_versions": sorted(f"{rid} v{v}"
                                                       for rid, v in row["rates"])})
        estimate_totals[currency] = (estimate_totals.get(currency, 0)
                                     + estimate_lines[-1]["minor"])

    return {
        "lines": lines,
        "totals": totals,
        "adjustment_totals": adjustment_totals,
        "unpriced": [{"month": month, "workspace_id": ws, "meter": meter, **row}
                     for (month, ws, meter), row in sorted(unpriced.items())],
        "estimate": {"label": "試用估價", "lines": estimate_lines,
                     "totals": estimate_totals, "uncertain_ms": uncertain_ms},
        "sealed": [p["period"] for p in sealed_periods],
        "uncertain_ms": uncertain_ms,
    }


def _fmt_totals(totals):
    return ", ".join(f"{c} {minor}" for c, minor in sorted(totals.items())) or "0"


def usage_view(card, segments, period=None, sealed_periods=(), adjustments=()):
    """Single source of truth for API, Web and CLI (issue #26 acceptance).

    Returns {lines, totals, currency, unpriced, uncertain_note, sealed,
    adjustment_totals, estimate}. Confirmed totals never include uncertain
    estimates (試用估價) or unpriced gaps.
    """
    result = price(card, segments, period, sealed_periods, adjustments)
    estimate = result["estimate"]
    note = None
    if estimate["lines"] or result["uncertain_ms"]:
        note = (f"至少 {_fmt_totals(result['totals'])}，另有待核對區間 "
                f"{result['uncertain_ms']} ms"
                f"（試用估價 {_fmt_totals(estimate['totals'])}，未計入確認總額）")
    currencies = sorted({line["currency"] for line in result["lines"]}
                        | set(estimate["totals"]))
    return {
        "lines": result["lines"],
        "totals": result["totals"],
        "currency": currencies,
        "unpriced": result["unpriced"],
        "uncertain_note": note,
        "sealed": result["sealed"],
        "adjustment_totals": result["adjustment_totals"],
        "estimate": estimate,
    }


def render_text(view):
    """Terminal rendering of a usage_view dict (estimates labeled 試用估價)."""
    out = ["試用估價（未收取款項）"]
    for line in view["lines"]:
        unit = METER_UNITS.get(line["meter"], "units")
        if line["kind"] == "adjustment":
            out.append(f"  調整 {line['month']} {line['workspace_id']} "
                       f"{line['meter']}: {line['currency']} {line['minor']} minor"
                       f"（{line['reason']}）")
        else:
            rates = ",".join(line.get("rate_versions", ()))
            suffix = f" [{rates}]" if rates else ""
            sealed = " 已封帳（原線不重算）" if line["kind"] == "sealed" else ""
            out.append(f"  {line['month']} {line['workspace_id']} {line['meter']}: "
                       f"{line['units']} {unit} -> "
                       f"{line['currency']} {line['minor']} minor{suffix}{sealed}")
    out.append(f"  合計: {_fmt_totals(view['totals'])}")
    for row in view["unpriced"]:
        out.append(f"  未定價 {row['month']} {row['workspace_id']} {row['meter']}: "
                   f"{row['units']} {METER_UNITS.get(row['meter'], 'units')}"
                   f"（不可默認 0）")
    for line in view["estimate"]["lines"]:
        out.append(f"  試用估價 {line['workspace_id']} {line['meter']}: "
                   f"{line['units']} {METER_UNITS.get(line['meter'], 'units')} -> "
                   f"{line['currency']} {line['minor']} minor（未計入總額）")
    if view["adjustment_totals"]:
        out.append(f"  調整合計: {_fmt_totals(view['adjustment_totals'])}"
                   f"（關聯原帳單，不改已封帳金額）")
    if view["sealed"]:
        out.append(f"  已封帳: {', '.join(view['sealed'])}")
    if view["uncertain_note"]:
        out.append(f"  {view['uncertain_note']}")
    return "\n".join(out)
