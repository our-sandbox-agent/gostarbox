# Go Runner

This module currently contains only the pure lifecycle-contract loader used by
the future #11 Runner. It loads `docs/contracts/runner-lifecycle.json`, rejects
malformed or invariant-breaking contracts, and models version/generation-fenced
observed-state transitions.

It is **not a running Runner**: it does not invoke Docker or runsc, create
processes or volumes, open a terminal, handle credentials, or persist state.
#8 remains the gate for runtime work; this package makes no runtime claim.

```sh
go test ./...
go vet ./...
```
