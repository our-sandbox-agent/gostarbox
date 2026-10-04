# Go Runner

This module currently contains only the pure lifecycle-contract loader used by
the future #11 Runner. It loads `docs/contracts/runner-lifecycle.json`, rejects
malformed or invariant-breaking contracts, and models version/generation-fenced
observed-state transitions.

It also validates the pure create specification and PID policy invariants:
non-root workload UID, equal guest `nproc` soft/hard limits, a matching guest
PID limit, a larger host PID backstop, `cap-drop ALL`, and
`no-new-privileges`. The researched `H=2N+128` value is not treated as a
product capacity guarantee.

Runner-side capacity admission uses the lifecycle contract's 409
`capacity_exceeded` refusal. The TypeScript control-plane API currently uses
429 at its edge; that documented divergence is intentionally preserved rather
than silently rewritten here.

It is **not a running Runner**: it does not invoke Docker or runsc, create
processes or volumes, open a terminal, handle credentials, or persist state.
#8 remains the gate for runtime work; this package makes no runtime claim.

```sh
go test ./...
go vet ./...
```
