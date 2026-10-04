// Package lifecycle loads and enforces the Runner lifecycle JSON contract.
// This package is pure logic: it has no Docker, runsc, process, terminal,
// filesystem, network, secret, or persistence dependency.
package lifecycle

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"strings"
)

// State is an observed Runner state. A desired state or HTTP 202 response is
// never sufficient evidence to change one.
type State string

// Trigger identifies contract operation/evidence rows.
type Trigger string

// Outcome selects the success or Error branch when a trigger has multiple
// destinations.
type Outcome bool

const (
	OutcomeSuccess Outcome = false
	OutcomeFailure Outcome = true
)

var (
	ErrInvalidContract   = errors.New("invalid lifecycle contract")
	ErrContractRequired  = errors.New("lifecycle contract is required")
	ErrUnknownState      = errors.New("unknown lifecycle state")
	ErrUnknownTrigger    = errors.New("unknown lifecycle transition")
	ErrTerminalState     = errors.New("lifecycle state is terminal")
	ErrVersionConflict   = errors.New("expected_version mismatch")
	ErrStaleGeneration   = errors.New("stale generation")
	ErrSequenceExhausted = errors.New("lifecycle version or generation exhausted")
	ErrInvalidOperation  = errors.New("invalid operation identity")
)

// StateInfo is the state row from the JSON source of truth.
type StateInfo struct {
	Terminal *bool  `json:"terminal"`
	Entry    string `json:"entry"`
}

// Transition is one observed-state transition row.
type Transition struct {
	From        State    `json:"from"`
	To          State    `json:"to"`
	Trigger     Trigger  `json:"trigger"`
	Requires    []string `json:"requires"`
	SideEffects []string `json:"side_effects"`
	ErrorCodes  []string `json:"error_codes"`
}

// Refusal is a closed-world refusal rule. It never creates an operation.
type Refusal struct {
	ID               string `json:"id"`
	When             string `json:"when"`
	HTTPStatus       *int   `json:"http_status"`
	Code             string `json:"code"`
	CreatesOperation *bool  `json:"creates_operation"`
	Notes            string `json:"notes"`
}

// CapacityExceeded describes the admission refusal row.
type CapacityExceeded struct {
	HTTPStatus       int    `json:"http_status"`
	Code             string `json:"code"`
	CreatesOperation *bool  `json:"creates_operation"`
	Transition       any    `json:"transition"`
}

// CapacityAdmission is the admission slice of the JSON contract.
type CapacityAdmission struct {
	CheckedOn  []Trigger         `json:"checked_on"`
	Inputs     map[string]string `json:"inputs"`
	OnExceeded CapacityExceeded  `json:"on_exceeded"`
}

// Invariant is a named rule that must remain present in the contract.
type Invariant struct {
	ID        string `json:"id"`
	Statement string `json:"statement"`
}

// Contract is the decoded and validated lifecycle source of truth.
type Contract struct {
	SchemaVersion     int                 `json:"schema_version"`
	Title             string              `json:"title"`
	SourceOfTruth     []string            `json:"source_of_truth"`
	InitialState      State               `json:"initial_state"`
	States            map[State]StateInfo `json:"states"`
	Transitions       []Transition        `json:"transitions"`
	Refusals          []Refusal           `json:"refusals"`
	CapacityAdmission CapacityAdmission   `json:"capacity_admission"`
	Invariants        []Invariant         `json:"invariants"`
}

// Load reads, strictly decodes, and validates a lifecycle JSON contract.
func Load(path string) (*Contract, error) {
	if path == "" {
		return nil, fmt.Errorf("%w: path is empty", ErrInvalidContract)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Parse(raw)
}

// Parse strictly decodes and validates lifecycle JSON. Unknown fields and
// trailing data are rejected so typos cannot silently become runtime rules.
func Parse(raw []byte) (*Contract, error) {
	var contract Contract
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&contract); err != nil {
		return nil, fmt.Errorf("%w: decode JSON: %v", ErrInvalidContract, err)
	}
	var extra any
	if err := decoder.Decode(&extra); !errors.Is(err, io.EOF) {
		return nil, fmt.Errorf("%w: trailing JSON data", ErrInvalidContract)
	}
	if err := contract.Validate(); err != nil {
		return nil, err
	}
	return &contract, nil
}

// Validate enforces structural and lifecycle invariants without creating a
// second transition table.
func (c *Contract) Validate() error {
	if c == nil {
		return ErrContractRequired
	}
	if c.SchemaVersion < 1 {
		return invalid("schema_version", "must be positive")
	}
	if strings.TrimSpace(c.Title) == "" {
		return invalid("title", "must not be empty")
	}
	if len(c.SourceOfTruth) == 0 {
		return invalid("source_of_truth", "must not be empty")
	}

	requiredStates := []State{
		"Creating", "Active", "Idle", "Suspending", "Suspend",
		"Resuming", "Destroying", "Destroyed", "Lost", "Error",
	}
	if len(c.States) != len(requiredStates) {
		return invalid("states", "does not contain the required state set")
	}
	for _, state := range requiredStates {
		info, ok := c.States[state]
		if !ok {
			return invalid("states", fmt.Sprintf("missing %s", state))
		}
		if info.Terminal == nil {
			return invalid(fmt.Sprintf("states.%s.terminal", state), "must be present")
		}
		if strings.TrimSpace(info.Entry) == "" {
			return invalid(fmt.Sprintf("states.%s.entry", state), "must not be empty")
		}
	}
	if c.InitialState != "Creating" {
		return invalid("initial_state", "must be Creating")
	}
	if !*c.States["Destroyed"].Terminal {
		return invalid("states.Destroyed.terminal", "must be true")
	}

	seen := make(map[string]struct{})
	for i, transition := range c.Transitions {
		where := fmt.Sprintf("transitions[%d]", i)
		fromInfo, ok := c.States[transition.From]
		if !ok {
			return invalid(where+".from", fmt.Sprintf("unknown state %q", transition.From))
		}
		if _, ok := c.States[transition.To]; !ok {
			return invalid(where+".to", fmt.Sprintf("unknown state %q", transition.To))
		}
		if strings.TrimSpace(string(transition.Trigger)) == "" {
			return invalid(where+".trigger", "must not be empty")
		}
		if *fromInfo.Terminal {
			return invalid(where, fmt.Sprintf("terminal state %s has an outgoing transition", transition.From))
		}
		if transition.From == "Lost" && transition.To == "Destroyed" {
			return invalid(where, "Lost must not transition directly to Destroyed")
		}
		if transition.From == "Error" &&
			transition.To != "Resuming" && transition.To != "Destroying" && transition.To != "Lost" {
			return invalid(where, "Error may only exit through Resuming, Destroying, or Lost")
		}

		outcome := outcomeFor(transition)
		key := fmt.Sprintf("%s\x00%s\x00%t", transition.From, transition.Trigger, outcome)
		if _, duplicate := seen[key]; duplicate {
			return invalid(where, "duplicate from/trigger/outcome transition")
		}
		seen[key] = struct{}{}
	}

	terminalStates := make([]State, 0, 1)
	for state, info := range c.States {
		if *info.Terminal {
			terminalStates = append(terminalStates, state)
		}
		if state == "Destroyed" {
			continue
		}
		if state == "Lost" {
			continue
		}
		if !c.hasTransition(state, "lease_expiry", "Lost") {
			return invalid("transitions", fmt.Sprintf("%s must retain lease_expiry -> Lost", state))
		}
	}
	if len(terminalStates) != 1 || terminalStates[0] != "Destroyed" {
		return invalid("states", "Destroyed must be the only terminal state")
	}

	refusalIDs := make(map[string]struct{}, len(c.Refusals))
	for i, refusal := range c.Refusals {
		where := fmt.Sprintf("refusals[%d]", i)
		if strings.TrimSpace(refusal.ID) == "" {
			return invalid(where+".id", "must not be empty")
		}
		if _, duplicate := refusalIDs[refusal.ID]; duplicate {
			return invalid(where+".id", "duplicate refusal id")
		}
		refusalIDs[refusal.ID] = struct{}{}
		if strings.TrimSpace(refusal.When) == "" {
			return invalid(where+".when", "must not be empty")
		}
		if refusal.HTTPStatus != nil && *refusal.HTTPStatus != 409 && *refusal.HTTPStatus != 422 {
			return invalid(where+".http_status", "must be 409, 422, or null")
		}
		if refusal.CreatesOperation == nil {
			return invalid(where+".creates_operation", "must be present")
		}
		if *refusal.CreatesOperation {
			return invalid(where+".creates_operation", "a refusal must not create an operation")
		}
	}

	capacity := c.CapacityAdmission
	if !containsTrigger(capacity.CheckedOn, "create") || !containsTrigger(capacity.CheckedOn, "resume") {
		return invalid("capacity_admission.checked_on", "must contain create and resume")
	}
	requiredInputs := []string{"milli_cpu", "memory_bytes", "volume_bytes"}
	if len(capacity.Inputs) != len(requiredInputs) {
		return invalid("capacity_admission.inputs", "must contain exactly the resource vector")
	}
	for _, name := range requiredInputs {
		description, ok := capacity.Inputs[name]
		if !ok || strings.TrimSpace(description) == "" {
			return invalid("capacity_admission.inputs."+name, "must be present and non-empty")
		}
	}
	exceeded := capacity.OnExceeded
	if exceeded.HTTPStatus != 409 || exceeded.Code != "capacity_exceeded" {
		return invalid("capacity_admission.on_exceeded", "must be 409 capacity_exceeded")
	}
	if exceeded.CreatesOperation == nil || *exceeded.CreatesOperation {
		return invalid("capacity_admission.on_exceeded.creates_operation", "must be false")
	}
	if exceeded.Transition != nil {
		return invalid("capacity_admission.on_exceeded.transition", "must be null")
	}

	requiredInvariants := []string{
		"destroyed_is_terminal",
		"lost_keeps_reservation",
		"lost_not_direct_destroyed",
		"generation_fencing",
		"observed_not_desired",
		"single_change_operation",
		"capacity_release_requires_confirmation",
	}
	invariantIDs := make(map[string]struct{}, len(c.Invariants))
	for i, invariant := range c.Invariants {
		where := fmt.Sprintf("invariants[%d]", i)
		if strings.TrimSpace(invariant.ID) == "" {
			return invalid(where+".id", "must not be empty")
		}
		if _, duplicate := invariantIDs[invariant.ID]; duplicate {
			return invalid(where+".id", "duplicate invariant id")
		}
		invariantIDs[invariant.ID] = struct{}{}
		if strings.TrimSpace(invariant.Statement) == "" {
			return invalid(where+".statement", "must not be empty")
		}
	}
	for _, id := range requiredInvariants {
		if _, ok := invariantIDs[id]; !ok {
			return invalid("invariants", fmt.Sprintf("missing %s", id))
		}
	}

	return nil
}

func invalid(where, message string) error {
	return fmt.Errorf("%w: %s: %s", ErrInvalidContract, where, message)
}

func outcomeFor(transition Transition) Outcome {
	return Outcome(transition.To == "Error")
}

func (c *Contract) hasTransition(from State, trigger Trigger, to State) bool {
	for _, transition := range c.Transitions {
		if transition.From == from && transition.Trigger == trigger && transition.To == to {
			return true
		}
	}
	return false
}

func containsTrigger(values []Trigger, want Trigger) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

// CanTransition reports whether a from/trigger/outcome row exists.
func (c *Contract) CanTransition(from State, trigger Trigger, outcome Outcome) bool {
	return c.transition(from, trigger, outcome) != nil
}

func (c *Contract) transition(from State, trigger Trigger, outcome Outcome) *Transition {
	for i := range c.Transitions {
		row := &c.Transitions[i]
		if row.From == from && row.Trigger == trigger && outcomeFor(*row) == outcome {
			return row
		}
	}
	return nil
}

func (t Transition) allocatesGeneration() bool {
	for _, effect := range t.SideEffects {
		if strings.Contains(effect, "allocate a new generation") {
			return true
		}
	}
	return false
}

// Operation is pure operation identity. Creating it neither executes work nor
// confirms an observed state.
type Operation struct {
	ID              string
	Trigger         Trigger
	ExpectedVersion uint64
	Generation      uint64
}

// Sandbox is the minimal versioned, generation-fenced observed-state snapshot.
type Sandbox struct {
	Contract   *Contract
	State      State
	Version    uint64
	Generation uint64
}

// NewSandbox validates a pure snapshot. It does not claim a runtime resource.
func NewSandbox(contract *Contract, state State, version, generation uint64) (Sandbox, error) {
	if contract == nil {
		return Sandbox{}, ErrContractRequired
	}
	if _, ok := contract.States[state]; !ok {
		return Sandbox{}, fmt.Errorf("%w: %q", ErrUnknownState, state)
	}
	return Sandbox{Contract: contract, State: state, Version: version, Generation: generation}, nil
}

// CanTransition checks only the loaded contract table. Version and generation
// are still enforced by Transition.
func (s Sandbox) CanTransition(trigger Trigger, outcome Outcome) bool {
	if s.Contract == nil {
		return false
	}
	return s.Contract.CanTransition(s.State, trigger, outcome)
}

// NewOperation returns pending operation identity bound to the current
// expected version and fencing generation.
func (s Sandbox) NewOperation(id string, trigger Trigger) (Operation, error) {
	if s.Contract == nil {
		return Operation{}, ErrContractRequired
	}
	if strings.TrimSpace(id) == "" {
		return Operation{}, fmt.Errorf("%w: ID must not be empty", ErrInvalidOperation)
	}
	found := false
	for _, transition := range s.Contract.Transitions {
		if transition.From == s.State && transition.Trigger == trigger {
			found = true
			break
		}
	}
	if !found {
		return Operation{}, fmt.Errorf(
			"%w: %s --%s--> is not defined", ErrInvalidOperation, s.State, trigger,
		)
	}
	return Operation{
		ID:              id,
		Trigger:         trigger,
		ExpectedVersion: s.Version,
		Generation:      s.Generation,
	}, nil
}

// Transition applies one Runner-confirmed row from the loaded JSON contract.
// It returns a new snapshot and never mutates the receiver.
func (s Sandbox) Transition(
	trigger Trigger,
	outcome Outcome,
	expectedVersion uint64,
	generation uint64,
) (Sandbox, error) {
	if s.Contract == nil {
		return s, ErrContractRequired
	}
	info, ok := s.Contract.States[s.State]
	if !ok {
		return s, fmt.Errorf("%w: %q", ErrUnknownState, s.State)
	}
	if info.Terminal != nil && *info.Terminal {
		return s, fmt.Errorf("%w: %s", ErrTerminalState, s.State)
	}
	if expectedVersion != s.Version {
		return s, fmt.Errorf("%w: have %d, got %d", ErrVersionConflict, s.Version, expectedVersion)
	}
	if generation != s.Generation {
		return s, fmt.Errorf("%w: have %d, got %d", ErrStaleGeneration, s.Generation, generation)
	}

	transition := s.Contract.transition(s.State, trigger, outcome)
	if transition == nil {
		return s, fmt.Errorf(
			"%w: %s --%s--> (%s)", ErrUnknownTrigger, s.State, trigger, outcomeName(outcome),
		)
	}
	if s.Version == math.MaxUint64 {
		return s, ErrSequenceExhausted
	}
	if s.Generation == math.MaxUint64 && transition.allocatesGeneration() {
		return s, ErrSequenceExhausted
	}

	next := Sandbox{
		Contract:   s.Contract,
		State:      transition.To,
		Version:    s.Version + 1,
		Generation: s.Generation,
	}
	if transition.allocatesGeneration() {
		next.Generation++
	}
	return next, nil
}

func outcomeName(outcome Outcome) string {
	if outcome == OutcomeFailure {
		return "failure"
	}
	return "success"
}
