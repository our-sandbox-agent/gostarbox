# Budget gates: eligibility, overrun model, persist-then-stop (#29)

Proposed, 2026-10-03. In-repo, stdlib-only executable RULES of the #29
acceptance (預算警示、停機與付費資格限制): `scripts/budget_gates.py`
(`EligibilityGate`, `overrun_window`, `BudgetEnforcer`,
`BlockedStoragePolicy` / `new_period`), tested by
`scripts/test_budget_gates.py` (43 tests incl. 8 mutation guards).
Nothing here is wired to a runtime: no metering loop, no runner, no
watchdog process, no Stripe, no DB — durability and stop operations are
INJECTED callables/stores. The slice REUSES the existing contracts
rather than forking them: payment-state policy comes from
[stripe-bridge.md](stripe-bridge.md) `BillingEligibility` (#27), stop
semantics (confirm-before-billing-cutoff, never auto-destroy) from
[watchdog-lease.md](watchdog-lease.md) (#18), atomic admission from the
`QuotaGate` pattern of [tenant-authz.md](tenant-authz.md) (#16), and the
conservative-when-uncertain posture from
[auto-policy.md](auto-policy.md) (#19).

## Single-source eligibility (共用 eligibility)

`EligibilityGate.check(account_state, action)` is the ONE decision
function for `create` / `resume` / `fork` / `resize` on Web, CLI and
API. Surfaces render its verdict; none re-implements it. Surface actions
map onto the #27 vocabulary (aligned, not forked): `create`, `fork` and
`resize` all start or GROW commitment → `provision`; `resume` →
`resume`.

Policy table (payment rows are #27 `BillingEligibility`, quoted for
completeness; budget rows are this contract):

| account state | create / fork / resize | resume | deny reason |
|---|---|---|---|
| `paid` (payment OK, budget OK) | allow | allow | — |
| `paid`, spend ≥ block-new threshold or at limit | deny | deny | `over_budget` |
| `paid`, persisted `budget_blocked` flag (sticky) | deny | deny | `over_budget` |
| `payment_failed`, inside 7-day grace | deny | allow | `payment_failed_grace` |
| `payment_failed`, grace expired (欠款停權) | deny | deny | `unpaid_suspension` |
| `canceled` / `deleted` (terminal) | deny | deny | `canceled` |
| `open` (no billing evidence; also the default when absent — conservative) | deny | deny | `no_billing_evidence` |

Threshold defaults (Proposed — product decision, not measured):
warn **80%**, block-new **95%** of the period limit; both ceil-rounded
in integer minor units (a percentage never blocks late).

### Atomic admission (the concurrency rule)

`admit(account_id, account_state, action, cost_minor)` runs the
eligibility check + budget increment as ONE critical section (the #16
`QuotaGate` pattern; the real backend stands this in for a Postgres
transaction/constraint). N parallel creates against a nearly-exhausted
budget cannot all pass: 30 threads vs a 1-slot budget admit exactly one
(`test_threaded_race_1_slot_exactly_one_admitted`; the guard proves the
lock is load-bearing). Admission also refuses a create whose result
would REACH the limit (`no_headroom_to_limit`) — the budget can never be
admitted up to the wall; the last slice stays reserved (see the overrun
model).

## Max overshoot of the 5-minute check (最大超額)

`overrun_window(policy)` computes, in exact integer math:

```
max_overshoot = Σ(highest rate of each active resource) × check_interval
              + Σ(same rates) × stop_duration
               └─ detection: spend that can happen unnoticed between two
                  periodic (default 5-minute) checks
               └─ stop: accrual until the CONFIRMED stop — #18: the
                  billing cutoff is the confirmed stop ONLY, so stop
                  time still bills
```

Worked example: two active sandboxes at their highest rates
2 minor/ms and 1 minor/ms (combined 3), checks every 300 000 ms, stop
bound 30 000 ms:

- detection = 3 × 300 000 = **900 000 minor**
- stop term = 3 × 30 000 = **90 000 minor**
- max overshoot = **990 000 minor**; hard-cap headroom required ≥ 990 000

Against a 20 000 000 minor limit the early-stop line is
20 000 000 − 990 000 = **19 010 000 minor**. A HARD cap requires BOTH
(the spec states the two together): reserved headroom ≥ max overshoot
below the limit AND early stop at limit − overshoot (block-new before
the wall). If overshoot ≥ limit the hard cap is INFEASIBLE at this
burn rate — `hard_cap_infeasible` flags it; the rate, check interval or
stop bound must shrink first (no threshold arithmetic can save it).

## At-limit sequence: persist `blocked` FIRST, then stop

`BudgetEnforcer.evaluate(spend, limit)` is pure: `ok` → `warn` (80%) →
`block_new` (95%) → `block_stop` at `spend >= limit` (the limit itself
is block_stop — an off-by-one mutant is guarded). `enforce()` executes
the block_stop sequence, whose order is normative:

1. **Persist the `blocked` record FIRST** (durable via the injected
   storage) — before any stop attempt. The record carries
   `stops_planned` / `stops_issued` / `stops_confirmed` / `alerts`.
2. **THEN issue stop (suspend) operations**, and each individual stop
   CALL is preceded by its durable issued-mark — a stop is never in
   flight without durable evidence (guard: reordering flips the event
   timeline).
3. **Stop timeout** (runner timeout): the record stays
   `blocked_stop_pending` with `stop_timeout` + `watchdog_followup`
   alerts — the #18 watchdog follows up (confirm-before-cutoff); this
   module NEVER auto-destroys and NEVER auto-re-issues an
   issued-but-unconfirmed stop (the remote may have succeeded — the #27
   outbox semantics).
4. **Races**: budget at limit while a stop is in flight → the
   concurrent enforce sees the durable record and issues NO second stop;
   `blocked` persists across restart via the injected persistence (no
   unblocked leak). The only crash-window re-send is for stops NEVER
   issued (persist happened, issue did not). The only unlocks are
   explicit: `new_period()` (budget side) or settlement (payment side).

Final phase `blocked_stopped` requires every planned stop confirmed.
Stops are SUSPENDS; data and volumes are kept (#18 semantics).

## Storage during blocked (Proposed — product decision)

Storage cost may CONTINUE after compute is blocked: volumes accrue.
`BlockedStoragePolicy` makes the terms explicit:

| Term | Proposed value |
|---|---|
| Who pays storage during blocked | `customer` (volumes accrue to the customer's bill) |
| Cleanup timer | 30 days blocked → cleanup fires; accrual CAPS there (the volumes are gone after cleanup) |
| Accrual reporting | always through `storage_accrued_minor()` — never silently zero elsewhere |
| New period | resets the BUDGET window only (`spend_minor` → 0, `budget_blocked` cleared) |
| 新帳期解除 vs 欠款 | `new_period_clears_unpaid_suspension: false` — a new-period unlock is NOT debt forgiveness; unpaid suspension lifts only by settlement through the #27 payment path |

## Honest state (狀態不假稱已零成本)

The enforcer's cost report never claims zero cost while blocked:
`compute_cost` is `still_accruing` until EVERY planned stop is CONFIRMED
(#18: billing cutoff = confirmed stop ONLY); `storage_cost` is
`still_accruing` for the whole blocked duration. `blocked_stop_pending`
carries `stop_timeout` + `watchdog_followup` alerts (暫停失敗／超額警示
可觀測). A mutant that reports `stopped`/zero on stop-pending is guarded.

## Runtime TODO (blocked; NOT claimed)

- No real usage-metering loop feeds `spend_minor`; no scheduler runs the
  5-minute checks; no runner/watchdog receives the stop operations (the
  #18 integration is the follow-up path this contract references).
- No real persistence backend — the injected store stands in for the
  Postgres transaction/constraint of the real control plane; the
  admission lock stands in for it on the eligibility side.
- Threshold values (80/95, 5-minute interval, 30s stop bound, 30-day
  cleanup) are Proposed defaults, to be set from workload testing —
  same posture as #19 (未確定前不偷填常數 applies to any CHANGE of
  them, and the defaults are labeled product decisions).
- Web/CLI/API surface wiring (rendering these verdicts) is cross-ticket
  integration, not this slice.

## Verify

```
python3 -m unittest discover -s scripts -p 'test_*.py'
npm test
npm run build
```
