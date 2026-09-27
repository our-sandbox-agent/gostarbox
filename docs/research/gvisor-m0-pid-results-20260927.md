# #70 remaining PID matrix: corrected decision and evidence

**GO-candidate for guest rejection/recovery; trial release remains blocked by
separate session, containment and product gates.** The prior NO-GO used a
contradictory acceptance criterion, now corrected in #70. A full workload UID
rejecting new tasks is the expected result, not a reason to switch runtime.
See [Proposed ADR](../adr/pid-trial-candidate.md). No paid Claude task was run.

## Review rerun

Eight interactive cases plus one explicit noninteractive case ran on the existing
4-vCPU/8-GiB VM: kernel 6.8.0-142-generic, Docker 29.8.1, runsc
release-20260921.0/systrap. Image:
`sha256:1728145d39d1e09111580fcd3a8a4931d0d0ebc0429caeb16ff295c86108e5cf`.
Each has 2 GiB memory, UID1000, cap-drop ALL, no-new-privileges, NPROC N/N.
Node runs eight Worker threads under tmux; npm ci uses the recorded fixture
lockfile with lifecycle scripts disabled. This is not a representative large build.

Host samples now label workload, pressure allocation, full quota and recovery.
The full phase includes the management/concurrent exec checks; recovery is excluded
from both peak columns below. Peaks are sampled values, not guaranteed maxima.

| CPU | N | H=2N+128 | Pressure threads / fork | Full threads / fork |
| ---: | ---: | ---: | ---: | ---: |
| 2 | 64 | 256 | 45 / 133 | 53 / 143 |
| 2 | 128 | 384 | 46 / 263 | 50 / 271 |
| 4 | 64 | 256 | 50 / 137 | 62 / 145 |
| 4 | 128 | 384 | 51 / 263 | 55 / 274 |

The old `2*N*(C+1)+64` formula is withdrawn: CPU2/4 did not justify its multiplier.
The old CPU4 thread pressure peaks were **50 and 46**, not 51 and 48 (those included
post-pressure samples). Historical bundles remain available with this correction.
The new lower caps are tested candidates, not a host-boundary or multi-sandbox
capacity proof. Original caps were roughly 3.2–5 times fork peaks; reservation
cost must be measured against admission needs rather than presented as necessary.

All eight interactive cases reached exactly N tasks and refused further creation
(thread RuntimeError or fork errno11/EAGAIN). All 32 workload-UID concurrent execs
were refused; all 32 host-created UID1001 management execs returned exit0 and
exactly `ADMIN_FORK_OK` after `/bin/true`. Workload attempts to setuid(0/1001) still
returned EPERM. Host pids.events stayed `max 0`; no panic or OOM was observed.
After release, all eight new execs returned exit0 / `RECOVERY_EXEC_OK`, the existing
interactive shells forked again, tmux survived and pressure children were reaped.

Every one of the eight Node Worker counters advances between `workers_before`
and `workers_full`, read by the already-running probe while pressure is held.
The original bundle measured the main heartbeat only; it did not directly prove
individual worker progress. Both streams are retained without conflating them.

A positive control lowers soft below hard then restores it to hard successfully.
Soft greater than hard is an invalid setting (Python ValueError, no errno), not
evidence of capability enforcement. Raising hard is also refused with ValueError;
do not relabel that as observed EPERM. Only the setuid checks record EPERM. These
bounded checks do not prove every privilege or namespace boundary. Probe guards
stop before pressure if a prohibited operation succeeds and fail if the allocation
bound is reached without a refusal; neither condition may look like at-limit success.

## Noninteractive session risk and management boundary

The explicit CPU2/N64/H256/fork case records `shell_exit_while_full=254` and
`shell_exit=254`. The container, tmux and Node workers remain alive; four management
execs succeed at full quota and a fresh workload exec recovers after release.
The **old noninteractive shell does not recover**. This is a real session risk,
not a failed runtime rejection test. Actual agent launch/supervision behavior must
be bound to this observation before trial release; Claude lifecycle is untested.
The earlier eight-case noninteractive pilot is published too, including failed
Docker-cp heartbeat attempts, not used to claim session survival.

One workload UID plus a separately trusted management UID replaces the old
single-guest-UID invariant. Access is host-controlled; the guest receives neither
Docker socket nor a way to invoke privileged management exec. The experiment
proves this bounded path works, not complete authorization or isolation security.
Network is default bridge with npm registry egress, **not** a verified network
isolation policy; #46 remains a separate gate.

The historical host-cap clone panic is unchanged. A microVM comparison, if pursued,
must target Sentry failure containment when host clone fails, not guest inability
to fork at full quota. More headroom is not a panic repair. #8 stays open and
#10/#11 remain waiting; founder acceptance and #67/E05/E10 are not implied.

## Reproduction and provenance

```sh
git -c core.autocrlf=true archive 7ef80b33834aee16cb0f2b54c1ff71dec499eb37 | tar -x -C /private/source
cd /private/source
sudo python3 scripts/gvisor-m0-pid.py --source-commit 7ef80b33834aee16cb0f2b54c1ff71dec499eb37 --image "$IMAGE" --output /private/raw-interactive
sudo python3 scripts/gvisor-m0-pid.py --source-commit 7ef80b33834aee16cb0f2b54c1ff71dec499eb37 --image "$IMAGE" --shell-mode noninteractive --cpus 2 --guests 64 --kinds fork --output /private/raw-noninteractive
```

Use fresh private paths and the image above. Explicit core.autocrlf=true reproduces
the Windows-created archive bytes; tests recreate that archive and check measured
hashes including the lockfile. Historical six hash mismatches are line-ending
differences; source-recovery metadata records canonical Git references and CRLF
positions. Earlier uncommitted pilot sources have measured text snapshots instead.
All original source hashes are reconstructable; do not treat redacted source text
alone as the unmodified original.

Published bundles: `evidence/2026-09-27-m0-pid` (282 files), and
`evidence/2026-09-27-pid-review/{pilot,interactive,noninteractive}` (61/305/48 files),
each plus a hash manifest. Raw data remains outside repo. Republished with tool
commit `34cc81f0a4b996921edf250a6c65322dce83cd67` from #72, preserving #73 nested
exemptions and Go SysProcAttr Credential values. The two historical bundles each
retain three source-comment/code-literal redactions; rerun bundles have zero.
No opaque files or registered-secret leaks were reported; no key was supplied.

Owned test containers were label-checked and removed; host-after captures no test
containers and restored systrap-only daemon configuration. No company folders or
Docker socket were mounted. Versions were recaptured after runs. Existing #61–#63
evidence is unchanged. These are bounded sequential VM experiments, not release CI.
