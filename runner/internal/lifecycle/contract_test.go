package lifecycle

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func goldenContract(t *testing.T) *Contract {
	t.Helper()
	path := filepath.Join("..", "..", "..", "docs", "contracts", "runner-lifecycle.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read golden contract: %v", err)
	}
	contract, err := Parse(raw)
	if err != nil {
		t.Fatalf("parse golden contract: %v", err)
	}
	return contract
}

func TestGoldenContractLoadsAndRepresentsCoreRows(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	if contract.InitialState != "Creating" {
		t.Fatalf("initial state: got %q, want Creating", contract.InitialState)
	}
	if len(contract.States) != 10 {
		t.Fatalf("state count: got %d, want 10", len(contract.States))
	}
	if len(contract.Transitions) != 31 {
		t.Fatalf("transition count: got %d, want 31", len(contract.Transitions))
	}
	if len(contract.Refusals) != 12 {
		t.Fatalf("refusal count: got %d, want 12", len(contract.Refusals))
	}
	if !contract.CanTransition("Active", "suspend", OutcomeSuccess) {
		t.Fatal("Active suspend success transition is missing")
	}
	if !contract.CanTransition("Lost", "destroy", OutcomeSuccess) {
		t.Fatal("Lost -> Destroying reconciliation transition is missing")
	}
	if lostDestroysDirectly(contract) {
		t.Fatal("Lost unexpectedly has a direct Destroyed transition")
	}
}

func lostDestroysDirectly(contract *Contract) bool {
	for _, transition := range contract.Transitions {
		if transition.From == "Lost" && transition.To == "Destroyed" {
			return true
		}
	}
	return false
}

func TestLoadedContractTransitionsPreserveFencing(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	for _, row := range contract.Transitions {
		t.Run(fmt.Sprintf("%s_%s_%s", row.From, row.Trigger, row.To), func(t *testing.T) {
			t.Parallel()

			before := Sandbox{
				Contract:   contract,
				State:      row.From,
				Version:    42,
				Generation: 7,
			}
			after, err := before.Transition(row.Trigger, outcomeFor(row), before.Version, before.Generation)
			if err != nil {
				t.Fatalf("Transition: %v", err)
			}
			if after.State != row.To {
				t.Fatalf("state: got %s, want %s", after.State, row.To)
			}
			if after.Version != before.Version+1 {
				t.Fatalf("version: got %d, want %d", after.Version, before.Version+1)
			}
			wantGeneration := before.Generation
			if row.allocatesGeneration() {
				wantGeneration++
			}
			if after.Generation != wantGeneration {
				t.Fatalf("generation: got %d, want %d", after.Generation, wantGeneration)
			}
			if before.State != row.From || before.Version != 42 || before.Generation != 7 {
				t.Fatalf("receiver mutated: %+v", before)
			}
		})
	}
}

func TestTransitionRefusalsLeaveSnapshotUnchanged(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	tests := []struct {
		name            string
		state           State
		trigger         Trigger
		outcome         Outcome
		expectedVersion uint64
		generation      uint64
		want            error
	}{
		{
			name:            "destroyed is terminal",
			state:           "Destroyed",
			trigger:         "destroy",
			outcome:         OutcomeSuccess,
			expectedVersion: 9,
			generation:      2,
			want:            ErrTerminalState,
		},
		{
			name:            "expected version mismatch",
			state:           "Active",
			trigger:         "suspend",
			outcome:         OutcomeSuccess,
			expectedVersion: 8,
			generation:      2,
			want:            ErrVersionConflict,
		},
		{
			name:            "stale generation",
			state:           "Active",
			trigger:         "suspend",
			outcome:         OutcomeSuccess,
			expectedVersion: 9,
			generation:      1,
			want:            ErrStaleGeneration,
		},
		{
			name:            "unknown trigger for state",
			state:           "Active",
			trigger:         "resume",
			outcome:         OutcomeSuccess,
			expectedVersion: 9,
			generation:      2,
			want:            ErrUnknownTrigger,
		},
		{
			name:            "failure row requested as success",
			state:           "Active",
			trigger:         "memory_termination_confirmed",
			outcome:         OutcomeSuccess,
			expectedVersion: 9,
			generation:      2,
			want:            ErrUnknownTrigger,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			before := Sandbox{
				Contract:   contract,
				State:      test.state,
				Version:    9,
				Generation: 2,
			}
			after, err := before.Transition(test.trigger, test.outcome, test.expectedVersion, test.generation)
			if !errors.Is(err, test.want) {
				t.Fatalf("error: got %v, want %v", err, test.want)
			}
			if after != before {
				t.Fatalf("failed transition changed snapshot: got %+v, want %+v", after, before)
			}
		})
	}
}

func TestOperationIdentityIsVersionAndGenerationFenced(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	sandbox := Sandbox{Contract: contract, State: "Active", Version: 12, Generation: 4}
	operation, err := sandbox.NewOperation("operation-1", "suspend")
	if err != nil {
		t.Fatalf("NewOperation: %v", err)
	}
	if operation.ID != "operation-1" || operation.Trigger != "suspend" ||
		operation.ExpectedVersion != 12 || operation.Generation != 4 {
		t.Fatalf("unexpected operation: %+v", operation)
	}

	if _, err := sandbox.NewOperation("", "suspend"); !errors.Is(err, ErrInvalidOperation) {
		t.Fatalf("empty ID error: got %v, want %v", err, ErrInvalidOperation)
	}
	if _, err := sandbox.NewOperation("operation-2", "resume"); !errors.Is(err, ErrInvalidOperation) {
		t.Fatalf("unknown trigger error: got %v, want %v", err, ErrInvalidOperation)
	}
	if sandbox.Version != 12 || sandbox.Generation != 4 || sandbox.State != "Active" {
		t.Fatalf("operation allocation mutated sandbox: %+v", sandbox)
	}
}

func TestParseRejectsUnknownFieldsAndTrailingData(t *testing.T) {
	t.Parallel()

	if _, err := Parse([]byte(`{"unexpected":true}`)); !errors.Is(err, ErrInvalidContract) {
		t.Fatalf("unknown field error: got %v", err)
	}
	valid, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "contracts", "runner-lifecycle.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Parse(append(valid, []byte("{}")...)); !errors.Is(err, ErrInvalidContract) {
		t.Fatalf("trailing data error: got %v", err)
	}
}

func TestContractValidationMutationsAreRejected(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		mutate func(*Contract)
	}{
		{
			name: "missing state",
			mutate: func(c *Contract) {
				delete(c.States, "Destroyed")
			},
		},
		{
			name: "destroyed is not terminal",
			mutate: func(c *Contract) {
				terminal := false
				info := c.States["Destroyed"]
				info.Terminal = &terminal
				c.States["Destroyed"] = info
			},
		},
		{
			name: "terminal state has an outgoing edge",
			mutate: func(c *Contract) {
				c.Transitions = append(c.Transitions, Transition{
					From: "Destroyed", To: "Error", Trigger: "lease_expiry",
				})
			},
		},
		{
			name: "lost goes directly to destroyed",
			mutate: func(c *Contract) {
				c.Transitions = append(c.Transitions, Transition{
					From: "Lost", To: "Destroyed", Trigger: "destroy",
				})
			},
		},
		{
			name: "duplicate outcome edge",
			mutate: func(c *Contract) {
				c.Transitions = append(c.Transitions, Transition{
					From: "Active", To: "Idle", Trigger: "set_idle",
				})
			},
		},
		{
			name: "refusal creates operation",
			mutate: func(c *Contract) {
				creates := true
				c.Refusals[0].CreatesOperation = &creates
			},
		},
		{
			name: "capacity vector loses an input",
			mutate: func(c *Contract) {
				delete(c.CapacityAdmission.Inputs, "memory_bytes")
			},
		},
		{
			name: "required invariant is removed",
			mutate: func(c *Contract) {
				c.Invariants = c.Invariants[1:]
			},
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			contract := goldenContract(t)
			if err := contract.Validate(); err != nil {
				t.Fatalf("baseline contract invalid: %v", err)
			}
			test.mutate(contract)
			if err := contract.Validate(); !errors.Is(err, ErrInvalidContract) {
				t.Fatalf("mutation was accepted: got %v, want %v", err, ErrInvalidContract)
			}
		})
	}
}

func TestResumeAllocatesNewGenerationFromEveryRecoverableState(t *testing.T) {
	t.Parallel()
	contract := goldenContract(t)
	for _, state := range []State{"Suspend", "Error", "Lost"} {
		t.Run(string(state), func(t *testing.T) {
			t.Parallel()
			before := Sandbox{Contract: contract, State: state, Version: 12, Generation: 4}
			after, err := before.Transition("resume", OutcomeSuccess, 12, 4)
			if err != nil {
				t.Fatalf("resume: %v", err)
			}
			// Assert the contract promise independently of allocatesGeneration.
			if after.State != "Resuming" || after.Version != 13 || after.Generation != 5 {
				t.Fatalf("resume must allocate a new fenced execution generation: %+v", after)
			}
		})
	}
}
