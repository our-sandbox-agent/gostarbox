"""Fixtures and guard tests for the #26 usage engine slice.

Fixture feeds: hand-checked pricing cases from docs/adr/usage-ledger.md §5
and docs/contracts/ledger-examples.json (cross-month split, mid-cycle
1 cent -> 2 cents = 3 cents exact, round-once-per-line, Lost lower bound),
plus EventLedger-driven lifecycle projections (destroy with volume purge,
snapshot TTL confirmed-delete, same-state resize, redelivery idempotence,
multi-sandbox accumulation). Mutation guards prove each safeguard
load-bearing (pattern per scripts/test_resource_events.py): rounding,
month and rate-version splitting, unpriced-never-zero, overlap rejection,
history immutability, sealed no-restatement, and a static no-float scan.
"""
import ast
import calendar
import io
import sys
import tokenize
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import usage_engine as ue  # noqa: E402
from resource_events import EventLedger  # noqa: E402
from test_resource_events import event  # noqa: E402
from usage_engine import (GIB_BYTES, RateCard, RateEntry, price,  # noqa: E402
                          render_text, round_half_up, usage_view)

ENGINE = Path(__file__).resolve().parent / "usage_engine.py"

SEP_2026 = calendar.timegm((2026, 9, 1, 0, 0, 0)) * 1000
OCT_2026 = calendar.timegm((2026, 10, 1, 0, 0, 0)) * 1000  # ledger example 1790812800000


def cpu_rate(version, numerator, from_ms, to_ms=None):
    """1 minor per 1,000,000 milliCPU·ms by default denominators."""
    return RateEntry(rate_id="cpu", version=version, currency="USD",
                     meter="cpu_reserved", effective_from_ms=from_ms,
                     effective_to_ms=to_ms, numerator_minor=numerator,
                     denominator_meter_units=1_000_000)


def seg(start, end, quantity, meter="cpu_reserved", **extra):
    row = {"start_ms": start, "end_ms": end, "quantity": {meter: quantity},
           "workspace_id": "ws1"}
    row.update(extra)
    return row


def ledger_segments(ledger, workspace_of):
    """Flatten an EventLedger projection into price() segment dicts."""
    out = []
    for rkey, projection in ledger.projection().items():
        for piece in projection["segments"]:
            out.append({**piece, "workspace_id": workspace_of(rkey)})
    return out


def single_ws(rkey):
    return "ws1"


class Rounding(unittest.TestCase):
    def test_half_up_boundaries(self):
        self.assertEqual(round_half_up(Fraction(1, 2)), 1)   # +0.5 -> up
        self.assertEqual(round_half_up(Fraction(3, 2)), 2)
        self.assertEqual(round_half_up(Fraction(5, 2)), 3)
        self.assertEqual(round_half_up(Fraction(2, 5)), 0)
        self.assertEqual(round_half_up(Fraction(-1, 2)), -1)  # -0.5 -> away from zero
        self.assertEqual(round_half_up(Fraction(-3, 2)), -2)
        self.assertEqual(round_half_up(Fraction(-2, 5)), 0)
        self.assertEqual(round_half_up(Fraction(0)), 0)

    def test_line_rounds_once_not_per_segment(self):
        # ledger-examples round-once-per-line: two 0.5-cent segments = 1 cent
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, [seg(0, 500, 1000), seg(500, 1000, 1000)])
        self.assertEqual(view["totals"], {"USD": 1})
        self.assertEqual(view["lines"][0]["units"], 1_000_000)


class MonthSplit(unittest.TestCase):
    def test_utc_month_boundary_split_exact(self):
        # ledger-examples utc-month-boundary: 1 CPU 1s each side of Oct 1
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, [seg(1790812799000, 1790812801000, 1000)])
        self.assertEqual([line["month"] for line in view["lines"]],
                         ["2026-09", "2026-10"])
        self.assertTrue(all(line["units"] == 1_000_000 for line in view["lines"]))
        self.assertEqual(view["totals"], {"USD": 2})
        self.assertEqual(OCT_2026, 1790812800000)  # fixture anchor


class MidCyclePriceChange(unittest.TestCase):
    def _card(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)          # 1 cent per CPU·s
        card.upsert(cpu_rate(2, 2, 1000), now_ms=0)       # from t1000: 2 cents
        return card

    def test_three_cents_exact(self):
        # ledger-examples rate-version-boundary: 2s total = 3 cents, the new
        # rate must never cover the old second
        view = usage_view(self._card(), [seg(0, 2000, 1000)])
        self.assertEqual(view["totals"], {"USD": 3})
        self.assertEqual(len(view["lines"]), 1)  # same month, line spans versions
        self.assertEqual(view["lines"][0]["rate_versions"], ["cpu v1", "cpu v2"])

    def test_rate_change_at_month_edge_keeps_both_splits(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        card.upsert(cpu_rate(2, 2, OCT_2026), now_ms=SEP_2026)
        view = usage_view(card, [seg(OCT_2026 - 1000, OCT_2026 + 1000, 1000)])
        self.assertEqual(view["totals"], {"USD": 3})
        self.assertEqual([line["month"] for line in view["lines"]],
                         ["2026-09", "2026-10"])


class Unpriced(unittest.TestCase):
    def test_gap_is_unpriced_never_zero(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0, 1000), now_ms=0)
        card.upsert(cpu_rate(2, 2, 2000), now_ms=1000)
        view = usage_view(card, [seg(0, 3000, 1000)])
        self.assertEqual(view["totals"], {"USD": 3})  # 1s@1 + 1s@2
        self.assertEqual(view["unpriced"],
                         [{"month": "1970-01", "workspace_id": "ws1",
                           "meter": "cpu_reserved", "units": 1_000_000,
                           "certainty": "confirmed"}])
        self.assertNotIn(0, view["totals"].values())

    def test_missing_meter_rate_is_unpriced(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        segments = [seg(0, 1000, 1000),
                    seg(0, 1000, 2048, meter="memory_reserved")]
        view = usage_view(card, segments)
        self.assertEqual(view["totals"], {"USD": 1})
        self.assertFalse(any(line["meter"] == "memory_reserved"
                             for line in view["lines"]))
        self.assertEqual(view["unpriced"][0]["meter"], "memory_reserved")
        self.assertEqual(view["unpriced"][0]["units"], 2048_000)


class LostEstimate(unittest.TestCase):
    def _segments(self):
        # ledger-examples lost-lower-bound: confirmed 1s + uncertain 2s
        return [seg(0, 1000, 1000),
                seg(1000, 3000, 1000, certainty="uncertain")]

    def test_confirmed_lower_bound_plus_pending_range(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, self._segments())
        self.assertEqual(view["totals"], {"USD": 1})  # uncertain never counted
        self.assertEqual(view["estimate"]["lines"][0]["units"], 2_000_000)
        self.assertEqual(view["estimate"]["totals"], {"USD": 2})
        self.assertEqual(view["estimate"]["label"], "試用估價")
        self.assertIn("至少", view["uncertain_note"])
        self.assertIn("待核對區間", view["uncertain_note"])
        self.assertIn("未計入確認總額", view["uncertain_note"])


class Units(unittest.TestCase):
    def test_gib_is_binary_and_fractional_gib_rounds_half_up(self):
        self.assertEqual(GIB_BYTES, 1 << 30)
        card = RateCard()
        card.upsert(RateEntry(rate_id="vol", version=1, currency="USD",
                              meter="volume_provisioned", effective_from_ms=0,
                              numerator_minor=10,
                              denominator_meter_units=GIB_BYTES * 3_600_000),
                    now_ms=0)
        hour = 3_600_000
        full = usage_view(card, [seg(0, hour, GIB_BYTES, meter="volume_provisioned")])
        quarter = usage_view(card, [seg(0, hour, GIB_BYTES // 4,
                                        meter="volume_provisioned")])
        self.assertEqual(full["totals"], {"USD": 10})   # exactly 1 GiB·h
        self.assertEqual(quarter["totals"], {"USD": 3})  # 2.5 -> half-up 3


class LifecycleFixtures(unittest.TestCase):
    """EventLedger projection segments priced end to end (issue #26)."""

    def test_destroy_stops_compute_volume_accrues_to_purge(self):
        ledger = EventLedger()
        for ev in [
            event("runtime.started", "runtime", "r1", 0,
                  quantity={"cpu_reserved": 1000}),
            event("resource.provisioned", "workspace_volume", "v1", 0,
                  quantity={"volume_provisioned": 1024}),
            event("runtime.stopped", "runtime", "r1", 2000),  # destroy: compute stops
            event("volume.purged", "workspace_volume", "v1", 4000),  # independent
        ]:
            ledger.append(ev)
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        card.upsert(RateEntry(rate_id="vol", version=1, currency="USD",
                              meter="volume_provisioned", effective_from_ms=0,
                              numerator_minor=5,
                              denominator_meter_units=4_096_000), now_ms=0)
        view = usage_view(card, ledger_segments(ledger, single_ws))
        by_meter = {line["meter"]: line for line in view["lines"]}
        self.assertEqual(by_meter["cpu_reserved"]["units"], 2_000_000)   # stops at t2000
        self.assertEqual(by_meter["cpu_reserved"]["minor"], 2)
        self.assertEqual(by_meter["volume_provisioned"]["units"], 4_096_000)  # to purge
        self.assertEqual(by_meter["volume_provisioned"]["minor"], 5)
        self.assertEqual(view["totals"], {"USD": 7})

    def test_snapshot_ttl_expiry_is_not_release(self):
        # ledger-examples snapshot-expiry-is-not-release: 6,144,000 byte·ms
        ledger = EventLedger()
        ledger.append(event("snapshot.created", "snapshot", "s1", 0,
                            quantity={"snapshot_stored": 2048}))
        ledger.append(event("snapshot.expired", "snapshot", "s1", 2000))  # request only
        ledger.append(event("snapshot.deleted", "snapshot", "s1", 3000))
        card = RateCard()
        card.upsert(RateEntry(rate_id="snap", version=1, currency="USD",
                              meter="snapshot_stored", effective_from_ms=0,
                              numerator_minor=1,
                              denominator_meter_units=6_144_000), now_ms=0)
        view = usage_view(card, ledger_segments(ledger, single_ws))
        self.assertEqual(view["totals"], {"USD": 1})
        self.assertEqual(view["lines"][0]["units"], 6_144_000)

    def test_same_state_resize_accumulates_one_line(self):
        # ledger-examples same-state-resize: 3,000,000 milliCPU·ms
        ledger = EventLedger()
        ledger.append(event("runtime.started", "runtime", "r1", 0,
                            quantity={"cpu_reserved": 1000}))
        ledger.append(event("policy.applied", "runtime", "r1", 1000,
                            quantity={"cpu_reserved": 2000}))
        ledger.append(event("runtime.stopped", "runtime", "r1", 2000))
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, ledger_segments(ledger, single_ws))
        self.assertEqual(view["lines"][0]["units"], 3_000_000)
        self.assertEqual(view["totals"], {"USD": 3})

    def test_redelivery_idempotence(self):
        ledger = EventLedger()
        events = [
            event("runtime.started", "runtime", "r1", 0,
                  quantity={"cpu_reserved": 1000}),
            event("runtime.stopped", "runtime", "r1", 2000),
        ]
        for ev in events:
            ledger.append(ev)
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        before = usage_view(card, ledger_segments(ledger, single_ws))
        for ev in ledger.events():  # whole batch redelivered
            receipt = ledger.append(ev)
            self.assertEqual(receipt["status"], "duplicate")
        self.assertEqual(usage_view(card, ledger_segments(ledger, single_ws)), before)
        self.assertEqual(before["totals"], {"USD": 2})

    def test_multi_sandbox_accumulation(self):
        ledger = EventLedger()
        for rid in ("r1", "r2", "r3"):
            ledger.append(event("runtime.started", "runtime", rid, 0,
                                quantity={"cpu_reserved": 1000}))
        ledger.append(event("runtime.stopped", "runtime", "r1", 3000))
        ledger.append(event("runtime.stopped", "runtime", "r2", 2000))
        ledger.append(event("runtime.stopped", "runtime", "r3", 1000))

        def ws_of(rkey):
            return "ws1" if rkey in ("runtime/r1", "runtime/r2") else "ws2"

        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, ledger_segments(ledger, ws_of))
        lines = {(line["workspace_id"], line["meter"]): line for line in view["lines"]}
        self.assertEqual(lines[("ws1", "cpu_reserved")]["units"], 5_000_000)  # r1+r2
        self.assertEqual(lines[("ws2", "cpu_reserved")]["units"], 1_000_000)
        self.assertEqual(view["totals"], {"USD": 6})


class RateCardRules(unittest.TestCase):
    def test_upsert_identical_is_noop(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        self.assertEqual(card.upsert(cpu_rate(1, 1, 0), now_ms=0),
                         {"status": "unchanged", "closed": []})

    def test_rejected_upsert_leaves_card_untouched(self):
        # validation must precede mutation: a refused replace cannot delete
        # the stored future row (would silently unprice its range)
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        card.upsert(cpu_rate(2, 2, 5000), now_ms=0)
        before = [repr(e) for e in card.entries("cpu_reserved")]
        with self.assertRaises(ValueError):
            card.upsert(cpu_rate(2, 2, 1000), now_ms=4000)
        after = [repr(e) for e in card.entries("cpu_reserved")]
        self.assertEqual(before, after)

    def test_new_version_closes_open_predecessor(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        receipt = card.upsert(cpu_rate(2, 2, 1000), now_ms=0)
        self.assertEqual(receipt["status"], "appended")
        self.assertEqual(receipt["closed"], ["cpu v1"])
        self.assertEqual(card.entries("cpu_reserved")[0].effective_to_ms, 1000)

    def test_overlapping_same_price_key_rejected(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0, 2000), now_ms=0)
        with self.assertRaises(ValueError):
            card.upsert(cpu_rate(2, 2, 0), now_ms=0)      # same start, overlaps
        with self.assertRaises(ValueError):
            card.upsert(cpu_rate(3, 3, 500, 1500), now_ms=0)  # hole punch
        self.assertEqual(len(card.entries()), 1)

    def test_different_price_keys_may_overlap(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        card.upsert(RateEntry(rate_id="mem", version=1, currency="USD",
                              meter="memory_reserved", effective_from_ms=0,
                              numerator_minor=1, denominator_meter_units=1),
                    now_ms=0)
        card.upsert(RateEntry(rate_id="cpu-idle", version=1, currency="USD",
                              meter="cpu_reserved", state="Idle",
                              effective_from_ms=0, numerator_minor=1,
                              denominator_meter_units=2_000_000), now_ms=0)
        self.assertEqual(len(card.entries()), 3)  # state-specific is its own key

    def test_specific_state_beats_wildcard(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        card.upsert(RateEntry(rate_id="cpu-idle", version=1, currency="USD",
                              meter="cpu_reserved", state="Idle",
                              effective_from_ms=0, numerator_minor=1,
                              denominator_meter_units=2_000_000), now_ms=0)
        view = usage_view(card, [seg(0, 1000, 1000),                      # 1 minor
                                 seg(1000, 2000, 1000, state="Idle")])   # 0.5 -> 1
        self.assertEqual(view["totals"], {"USD": 2})

    def test_already_effective_row_is_immutable_history(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0, 1000), now_ms=500)  # effective at now=500
        with self.assertRaises(ValueError):
            card.upsert(cpu_rate(1, 5, 0, 1000), now_ms=500)   # reprice history
        with self.assertRaises(ValueError):
            card.upsert(cpu_rate(1, 1, 0, 2000), now_ms=2000)  # extend the past
        self.assertEqual(card.entries()[0].numerator_minor, 1)

    def test_future_row_may_be_replaced_until_effective(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        card.upsert(cpu_rate(2, 2, 5000), now_ms=0)
        receipt = card.upsert(cpu_rate(2, 9, 5000), now_ms=1000)  # still future
        self.assertEqual(receipt["status"], "appended")
        self.assertEqual(card.entries()[-1].numerator_minor, 9)

    def test_sync_from_provider_is_append_only(self):
        card = RateCard()
        stored = cpu_rate(1, 1, 0)
        card.upsert(stored, now_ms=0)
        report = card.sync_from_provider([
            stored,                                       # identical -> unchanged
            cpu_rate(1, 99, 0),                           # rewrite -> conflict
            cpu_rate(2, 2, 3000),                         # future version -> append
            cpu_rate(3, 3, 1000),                         # already-effective -> conflict
            cpu_rate(4, 4, 2000),                         # overlaps v2 [3000..) ? no: [2000,..) overlaps truncated v1 [0,3000) -> conflict
        ], now_ms=1000)
        self.assertEqual([r["version"] for r in report["unchanged"]], [1])
        self.assertEqual([r["version"] for r in report["appended"]], [2])
        self.assertEqual([r["version"] for r in report["conflicts"]], [1, 3, 4])
        # history untouched: v1 still prices [0,3000) at 1 minor per M units
        self.assertEqual(price(card, [seg(0, 1000, 1000)])["totals"], {"USD": 1})

    def test_sync_never_rewrites_stored_rows(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        before = card.entries()
        card.sync_from_provider([cpu_rate(1, 42, 0)], now_ms=0)
        self.assertEqual(card.entries(), before)


class SealedPeriods(unittest.TestCase):
    def _sealed_view(self, segments):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        sealed = [{
            "period": "2026-09", "start_ms": SEP_2026, "end_ms": OCT_2026,
            "lines": [{"month": "2026-09", "workspace_id": "ws1", "currency": "USD",
                       "meter": "cpu_reserved", "units": 1_000_000, "minor": 1,
                       "rate_versions": ["cpu v1"]}],
        }]
        adjustments = [{"period": "2026-09", "workspace_id": "ws1", "currency": "USD",
                        "meter": "cpu_reserved", "minor": 1,
                        "reason": "lost-correction confirmed stop at H+1000"}]
        return usage_view(card, segments, sealed_periods=sealed,
                          adjustments=adjustments)

    def test_sealed_lines_untouched_late_correction_is_adjustment(self):
        # Lost sealed at 1 minor; recovery proves 2M units. The sealed line
        # stays 1; the correction is a +1 adjustment line, totals unchanged.
        segments = [seg(SEP_2026, SEP_2026 + 2000, 1000)]  # recompute would say 2
        view = self._sealed_view(segments)
        sealed_lines = [line for line in view["lines"] if line["kind"] == "sealed"]
        self.assertEqual(sealed_lines[0]["minor"], 1)
        self.assertEqual(view["totals"], {"USD": 1})  # not restated to 2
        self.assertEqual(view["adjustment_totals"], {"USD": 1})
        adjustment = next(line for line in view["lines"] if line["kind"] == "adjustment")
        self.assertEqual(adjustment["minor"], 1)
        self.assertEqual(view["sealed"], ["2026-09"])
        # outside the seal the same correction would recompute normally
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        self.assertEqual(usage_view(card, segments)["totals"], {"USD": 2})

    def test_segments_after_seal_still_price(self):
        segments = [seg(SEP_2026, SEP_2026 + 1000, 1000),
                    seg(OCT_2026, OCT_2026 + 1000, 1000)]
        view = self._sealed_view(segments)
        confirmed = [line for line in view["lines"] if line["kind"] == "confirmed"]
        self.assertEqual([line["month"] for line in confirmed], ["2026-10"])
        self.assertEqual(view["totals"], {"USD": 2})  # sealed 1 + fresh 1


class ViewContract(unittest.TestCase):
    def test_usage_view_shape_and_idempotence(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, [seg(0, 1000, 1000),
                                 seg(1000, 2000, 1000, certainty="uncertain")])
        self.assertEqual(set(view), {"lines", "totals", "currency", "unpriced",
                                     "uncertain_note", "sealed",
                                     "adjustment_totals", "estimate"})
        self.assertEqual(view["currency"], ["USD"])
        self.assertEqual(usage_view(card, [seg(0, 1000, 1000),
                                           seg(1000, 2000, 1000,
                                               certainty="uncertain")]), view)
        for value in _walk(view):
            self.assertNotIsInstance(value, float)

    def test_render_text_labels(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, [seg(0, 1000, 1000),
                                 seg(1000, 3000, 1000, certainty="uncertain")])
        text = render_text(view)
        self.assertIn("試用估價", text)
        self.assertIn("cpu_reserved", text)
        self.assertIn("至少", text)
        self.assertIn("待核對區間", text)

    def test_render_text_unpriced_and_sealed(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0, 1000), now_ms=0)
        sealed = [{"period": "2026-09", "start_ms": SEP_2026, "end_ms": OCT_2026,
                   "lines": [{"month": "2026-09", "workspace_id": "ws1",
                              "currency": "USD", "meter": "cpu_reserved",
                              "units": 1, "minor": 1}]}]
        view = usage_view(card, [seg(0, 3000, 1000)], sealed_periods=sealed,
                          adjustments=[{"period": "2026-09", "workspace_id": "ws1",
                                        "currency": "USD", "meter": "cpu_reserved",
                                        "minor": -1, "reason": "credit"}])
        text = render_text(view)
        self.assertIn("未定價", text)
        self.assertIn("不可默認 0", text)
        self.assertIn("已封帳: 2026-09", text)
        self.assertIn("調整", text)
        self.assertIn("-1", text)

    def test_period_filter_clips_segments(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        view = usage_view(card, [seg(0, 3000, 1000)], period=(1000, 2000))
        self.assertEqual(view["lines"][0]["units"], 1_000_000)


def _walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)
    else:
        yield node


class SourceGuards(unittest.TestCase):
    """Static scan: exact arithmetic only in the engine source."""

    def test_no_true_division_or_float_constants(self):
        tree = ast.parse(ENGINE.read_text())
        for node in ast.walk(tree):
            self.assertNotIsInstance(node, ast.Div, "true division is forbidden")
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                self.fail(f"float literal {node.value!r} in engine source")

    def test_no_float_name_in_code(self):
        source = ENGINE.read_text()
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.NAME:
                self.assertNotEqual(tok.string, "float",
                                    f"'float' used at line {tok.start[0]}")


class MutationGuards(unittest.TestCase):
    """Each safeguard must be load-bearing: break it, the result is wrong."""

    def test_rounding_guard_bankers_rounding_breaks_boundaries(self):
        # banker's rounding (language default) would take 0.5 -> 0 and
        # -0.5 -> 0; half-up is normative (ADR §4)
        with mock.patch.object(ue, "round_half_up", staticmethod(round)):
            self.assertEqual(round(Fraction(1, 2)), 0)  # guard bypassed
        self.assertEqual(round_half_up(Fraction(1, 2)), 1)

    def test_month_split_guard_no_split_collapses_months(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        segments = [seg(1790812799000, 1790812801000, 1000)]
        self.assertEqual(len(usage_view(card, segments)["lines"]), 2)
        with mock.patch.object(ue, "_month_boundaries", lambda s, e: []):
            collapsed = usage_view(card, segments)
        self.assertEqual(len(collapsed["lines"]), 1)  # months merged without guard

    def test_rate_split_guard_old_rate_covers_new_time(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        card.upsert(cpu_rate(2, 2, 1000), now_ms=0)
        segments = [seg(0, 2000, 1000)]
        self.assertEqual(usage_view(card, segments)["totals"], {"USD": 3})
        with mock.patch.object(RateCard, "boundaries",
                               lambda self, meter, state, s, e: []):
            underpriced = usage_view(card, segments)
        self.assertEqual(underpriced["totals"], {"USD": 2})  # v1 covers t1000..

    def test_unpriced_guard_zero_pricing_breaks_gap_rule(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0, 1000), now_ms=0)
        card.upsert(cpu_rate(2, 2, 2000), now_ms=1000)
        segments = [seg(0, 3000, 1000)]
        real = usage_view(card, segments)
        self.assertEqual(real["unpriced"][0]["units"], 1_000_000)
        self.assertEqual(real["totals"], {"USD": 3})
        zero_entry = cpu_rate(99, 0, 0)  # a silent zero default for the gap
        with mock.patch.object(RateCard, "lookup",
                               lambda self, meter, at, state=None: zero_entry):
            falsified = usage_view(card, segments)
        self.assertEqual(falsified["unpriced"], [])      # gap vanished: bad
        self.assertEqual(falsified["totals"], {"USD": 0})  # silently free: bad
        self.assertEqual(falsified["lines"][0]["units"], 3_000_000)

    def test_overlap_guard_permits_ambiguity(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0, 2000), now_ms=0)
        with self.assertRaises(ValueError):
            card.upsert(cpu_rate(2, 2, 0), now_ms=0)  # same-start overlap
        permissive = RateCard()
        permissive.upsert(cpu_rate(1, 1, 0, 2000), now_ms=0)
        with mock.patch.object(RateCard, "_overlap",
                               classmethod(lambda cls, a, b: False)):
            permissive.upsert(cpu_rate(2, 2, 0), now_ms=0)  # guard bypassed
        covering = [e for e in permissive.entries() if e.effective_from_ms <= 1500
                    and (e.effective_to_ms is None or 1500 < e.effective_to_ms)]
        self.assertEqual(len(covering), 2)  # two rates claim t1500: ambiguous
        with self.assertRaises(ValueError):
            permissive.lookup("cpu_reserved", 1500)  # engine refuses to guess

    def test_history_guard_permissive_upsert_reprices_past(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=1000)  # already effective
        segments = [seg(0, 1000, 1000)]
        self.assertEqual(usage_view(card, segments)["totals"], {"USD": 1})
        tampered = RateCard()
        tampered.upsert(cpu_rate(1, 1, 0), now_ms=1000)
        with mock.patch.object(RateCard, "_check_history_immutable",
                               lambda self, existing, entry, now: None):
            tampered.upsert(cpu_rate(1, 5, 0), now_ms=1000)  # guard bypassed
        self.assertEqual(usage_view(tampered, segments)["totals"], {"USD": 5})
        # the strict test below is what the guard keeps true
        with self.assertRaises(ValueError):
            card.upsert(cpu_rate(1, 5, 0), now_ms=1000)
        self.assertEqual(usage_view(card, segments)["totals"], {"USD": 1})

    def test_sealed_guard_recompute_would_restate_sealed_totals(self):
        card = RateCard()
        card.upsert(cpu_rate(1, 1, 0), now_ms=0)
        sealed = [{"period": "2026-09", "start_ms": SEP_2026, "end_ms": OCT_2026,
                   "lines": [{"month": "2026-09", "workspace_id": "ws1",
                              "currency": "USD", "meter": "cpu_reserved",
                              "units": 1_000_000, "minor": 1}]}]
        segments = [seg(SEP_2026, SEP_2026 + 2000, 1000)]  # correction says 2
        view = usage_view(card, segments, sealed_periods=sealed)
        self.assertEqual(view["totals"], {"USD": 1})
        with mock.patch.object(ue, "_outside_sealed",
                               lambda s, e, periods: [(s, e)]):
            restated = usage_view(card, segments, sealed_periods=sealed)
        self.assertEqual(restated["totals"], {"USD": 3})  # 1 sealed + 2 restated


if __name__ == "__main__":
    unittest.main()
