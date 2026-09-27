# PID candidate and trial decision (#70)

Status: **Proposed**, 2026-09-27. No founder approval or runtime go implied.

## Decision proposed

**GO-candidate for clean rejection at full guest quota and recovery after release.**
The previous NO-GO based on an existing shell failing to fork at full NPROC is
withdrawn: #70 now treats that observation as measurement, not a pass condition.
The measured workload UID must temporarily reject new tasks while its quota is
full. Whether that product behavior is acceptable remains a founder decision.

Keep one **workload UID** (1000), cap-drop ALL, no-new-privileges and equal initial
soft/hard NPROC. Permit a distinct trusted management UID (1001), created only
through the host control plane. In eight cases, all 32 management execs forked
`/bin/true` while all 32 workload-UID execs were cleanly refused. Guest attempts
to switch to UID 1001 or root failed with EPERM. This is a candidate management
path, not complete user-namespace/escalation isolation validation. The guest must
never receive Docker socket access or credentials to invoke that host operation.

Use `H = 2*N + 128` as the **tested candidate**, with N=64/128, CPU quota=2/4,
memory=2 GiB and one sandbox at a time on the existing 4-vCPU/8-GiB VM. H is 256/384.
This replaces `2*N*(C+1)+64`: the old CPU multiplier was not empirically supported.
The new eight cases stayed below H (largest full-phase sample 274); none tested
the new host boundary. Neither formula repairs the historical host-clone panic.
Host admission still needs measured multi-sandbox and control-plane reservations;
the old caps were roughly 3.2–5 times fork peaks, an admission-density tradeoff,
not evidence of that much necessary overhead or a measured density ratio.

## Session and containment gates

Noninteractive bash **exits 254** after fork failure in the explicit rerun, while
Node/tmux and the container survive and fresh exec recovers after pressure release.
Original noninteractive pilot evidence is now published. An interactive shell
stays alive, but changing shell mode cannot establish the actual agent lifecycle
contract. Determine the product launch/session path and verify its exit handling
before trial release; no Claude process/session survival claim is made here.

MicroVM comparison is triggered by the independently observed **host clone
rejection crashing Sentry and its failure-containment scope**, not guest EAGAIN.
Guest workload can induce host tasks charged to the runtime cgroup; the measured
thread and fork mappings differ. Do not assume one guest task equals one host
task. Extra headroom avoids the measured boundary in these cases but does not
fix panic when that boundary is reached. A runtime comparison must test this
containment property; changing runtime does not remove guest quota exhaustion.

These runs use default Docker bridge networking and npm registry egress. Network
isolation/private-address/metadata protections under #46 are **not validated**.
#8 stays open; #10/#11 wait for their gates, including #67 policy and paid E05/E10.
See [measured evidence](../research/gvisor-m0-pid-results-20260927.md).
