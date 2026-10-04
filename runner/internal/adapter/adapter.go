// Package adapter defines the pre-runtime Runner port and a deterministic fake.
// It does not implement Docker, runsc, process, filesystem, terminal, network,
// secret, or persistence behavior.
package adapter

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"github.com/our-sandbox-agent/gostarbox/runner/internal/lifecycle"
)

var (
	ErrInvalidRequest    = errors.New("invalid adapter request")
	ErrSandboxExists     = errors.New("sandbox already exists")
	ErrSandboxNotFound   = errors.New("sandbox not found")
	ErrOperationNotFound = errors.New("operation not found")
	ErrOperationMismatch = errors.New("operation does not match this request")
	ErrOperationPending  = errors.New("another operation is already pending")
)

// Adapter is the future runtime boundary. Calls accept pure operation
// identity and return observed snapshots; they never represent desired state
// as confirmed runtime state.
type Adapter interface {
	Create(context.Context, CreateRequest) (Instance, error)
	Submit(context.Context, SubmitRequest) (Instance, error)
	Confirm(context.Context, ConfirmRequest) (Instance, error)
	Inspect(context.Context, string) (Instance, error)
}

var _ Adapter = (*Fake)(nil)

// CreateRequest contains pure create identity and specification.
type CreateRequest struct {
	SandboxID   string
	OperationID string
	Spec        lifecycle.SandboxSpec
}

// SubmitRequest registers a change operation against the current version and
// generation. Submission alone does not confirm runtime work.
type SubmitRequest struct {
	SandboxID       string
	OperationID     string
	Trigger         lifecycle.Trigger
	ExpectedVersion uint64
	Generation      uint64
}

// ConfirmRequest supplies Runner-confirmed evidence for an existing pending
// operation. Intermediate confirmations may advance one contract row at a time.
type ConfirmRequest struct {
	SandboxID       string
	OperationID     string
	Trigger         lifecycle.Trigger
	Outcome         lifecycle.Outcome
	ExpectedVersion uint64
	Generation      uint64
}

// Instance is an immutable observed adapter view.
type Instance struct {
	SandboxID          string
	State              lifecycle.State
	Version            uint64
	Generation         uint64
	PendingOperationID string
}

type operationRecord struct {
	sandboxID       string
	trigger         lifecycle.Trigger
	expectedVersion uint64
	generation      uint64
	completed       bool
	finalOutcome    lifecycle.Outcome
	finalState      lifecycle.State
	finalVersion    uint64
	finalGeneration uint64
}

// Fake is an in-memory test double. It is not a runtime adapter and must not
// be deployed as one.
type Fake struct {
	contract  *lifecycle.Contract
	available lifecycle.ResourceVector

	mu         sync.Mutex
	sandboxes  map[string]lifecycle.Sandbox
	operations map[string]operationRecord
	pending    map[string]string
	resources  map[string]lifecycle.ResourceVector
	used       lifecycle.ResourceVector
}

// NewFake validates its inputs and returns a deterministic Adapter. A zero
// available vector is valid and refuses all resource-consuming creates.
func NewFake(
	contract *lifecycle.Contract,
	available lifecycle.ResourceVector,
) (*Fake, error) {
	if contract == nil {
		return nil, lifecycle.ErrContractRequired
	}
	if err := contract.Validate(); err != nil {
		return nil, err
	}
	if available.MilliCPU < 0 || available.MemoryBytes < 0 || available.VolumeBytes < 0 {
		return nil, fmt.Errorf("%w: available capacity must be non-negative", ErrInvalidRequest)
	}
	return &Fake{
		contract:   contract,
		available:  available,
		sandboxes:  make(map[string]lifecycle.Sandbox),
		operations: make(map[string]operationRecord),
		pending:    make(map[string]string),
		resources:  make(map[string]lifecycle.ResourceVector),
	}, nil
}

// Create validates a pure specification, performs contract admission, and
// atomically records one Creating snapshot and pending create operation.
func (f *Fake) Create(ctx context.Context, request CreateRequest) (Instance, error) {
	if err := checkContext(ctx); err != nil {
		return Instance{}, err
	}
	if request.SandboxID == "" || request.OperationID == "" {
		return Instance{}, fmt.Errorf("%w: sandbox and operation IDs are required", ErrInvalidRequest)
	}
	if err := lifecycle.ValidateSandboxSpec(request.Spec); err != nil {
		return Instance{}, err
	}

	f.mu.Lock()
	defer f.mu.Unlock()

	if record, ok := f.operations[request.OperationID]; ok {
		if record.sandboxID != request.SandboxID || record.trigger != "create" {
			return Instance{}, fmt.Errorf(
				"%w: operation %s belongs to %s", ErrOperationMismatch, request.OperationID, record.sandboxID,
			)
		}
		return f.instanceLocked(request.SandboxID), nil
	}
	if _, ok := f.sandboxes[request.SandboxID]; ok {
		return Instance{}, fmt.Errorf("%w: %s", ErrSandboxExists, request.SandboxID)
	}

	remaining := f.remainingLocked()
	if err := f.contract.AdmitCapacity("create", request.Spec.Resources, remaining); err != nil {
		return Instance{}, err
	}
	sandbox, err := lifecycle.NewSandbox(f.contract, "Creating", 1, 1)
	if err != nil {
		return Instance{}, err
	}
	f.sandboxes[request.SandboxID] = sandbox
	f.operations[request.OperationID] = operationRecord{
		sandboxID:       request.SandboxID,
		trigger:         "create",
		expectedVersion: sandbox.Version,
		generation:      sandbox.Generation,
	}
	f.pending[request.SandboxID] = request.OperationID
	f.resources[request.SandboxID] = request.Spec.Resources
	f.addUsedLocked(request.Spec.Resources)
	return f.instanceLocked(request.SandboxID), nil
}

// Submit registers a change operation if the contract defines its trigger and
// no other operation is pending. It does not change observed state.
func (f *Fake) Submit(ctx context.Context, request SubmitRequest) (Instance, error) {
	if err := checkContext(ctx); err != nil {
		return Instance{}, err
	}
	if request.SandboxID == "" || request.OperationID == "" {
		return Instance{}, fmt.Errorf("%w: sandbox and operation IDs are required", ErrInvalidRequest)
	}

	f.mu.Lock()
	defer f.mu.Unlock()

	sandbox, ok := f.sandboxes[request.SandboxID]
	if !ok {
		return Instance{}, fmt.Errorf("%w: %s", ErrSandboxNotFound, request.SandboxID)
	}
	if record, ok := f.operations[request.OperationID]; ok {
		if record.sandboxID != request.SandboxID || record.trigger != request.Trigger ||
			record.expectedVersion != request.ExpectedVersion || record.generation != request.Generation {
			return Instance{}, fmt.Errorf("%w: operation %s", ErrOperationMismatch, request.OperationID)
		}
		return f.instanceLocked(request.SandboxID), nil
	}
	if pendingID, ok := f.pending[request.SandboxID]; ok {
		return Instance{}, fmt.Errorf(
			"%w: %s", ErrOperationPending, pendingID,
		)
	}
	operation, err := sandbox.NewOperation(request.OperationID, request.Trigger)
	if err != nil {
		return Instance{}, err
	}
	if operation.ExpectedVersion != request.ExpectedVersion || operation.Generation != request.Generation {
		return Instance{}, fmt.Errorf("%w: stale version or generation", ErrOperationMismatch)
	}
	f.operations[request.OperationID] = operationRecord{
		sandboxID:       request.SandboxID,
		trigger:         request.Trigger,
		expectedVersion: operation.ExpectedVersion,
		generation:      operation.Generation,
	}
	f.pending[request.SandboxID] = request.OperationID
	return f.instanceLocked(request.SandboxID), nil
}

// Confirm applies one Runner-confirmed lifecycle row. Multi-phase operations
// remain pending until their contract-defined terminal outcome is reached.
func (f *Fake) Confirm(ctx context.Context, request ConfirmRequest) (Instance, error) {
	if err := checkContext(ctx); err != nil {
		return Instance{}, err
	}
	if request.SandboxID == "" || request.OperationID == "" {
		return Instance{}, fmt.Errorf("%w: sandbox and operation IDs are required", ErrInvalidRequest)
	}

	f.mu.Lock()
	defer f.mu.Unlock()

	record, ok := f.operations[request.OperationID]
	if !ok {
		return Instance{}, fmt.Errorf("%w: %s", ErrOperationNotFound, request.OperationID)
	}
	if record.sandboxID != request.SandboxID || record.trigger != request.Trigger {
		return Instance{}, fmt.Errorf("%w: operation %s", ErrOperationMismatch, request.OperationID)
	}
	if record.completed {
		if record.finalOutcome != request.Outcome {
			return Instance{}, fmt.Errorf("%w: completed outcome differs", ErrOperationMismatch)
		}
		return Instance{
			SandboxID:  request.SandboxID,
			State:      record.finalState,
			Version:    record.finalVersion,
			Generation: record.finalGeneration,
		}, nil
	}

	sandbox, ok := f.sandboxes[request.SandboxID]
	if !ok {
		return Instance{}, fmt.Errorf("%w: %s", ErrSandboxNotFound, request.SandboxID)
	}
	next, err := sandbox.Transition(
		request.Trigger, request.Outcome, request.ExpectedVersion, request.Generation,
	)
	if err != nil {
		return Instance{}, err
	}
	f.sandboxes[request.SandboxID] = next

	if operationComplete(request.Trigger, next.State) {
		record.completed = true
		record.finalOutcome = request.Outcome
		record.finalState = next.State
		record.finalVersion = next.Version
		record.finalGeneration = next.Generation
		f.operations[request.OperationID] = record
		delete(f.pending, request.SandboxID)
		if next.State == "Destroyed" {
			f.releaseLocked(record.sandboxID)
		}
	}
	return f.instanceLocked(request.SandboxID), nil
}

// Inspect returns an observed snapshot without mutating it.
func (f *Fake) Inspect(ctx context.Context, sandboxID string) (Instance, error) {
	if err := checkContext(ctx); err != nil {
		return Instance{}, err
	}
	if sandboxID == "" {
		return Instance{}, fmt.Errorf("%w: sandbox ID is required", ErrInvalidRequest)
	}

	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.sandboxes[sandboxID]; !ok {
		return Instance{}, fmt.Errorf("%w: %s", ErrSandboxNotFound, sandboxID)
	}
	return f.instanceLocked(sandboxID), nil
}

func (f *Fake) instanceLocked(sandboxID string) Instance {
	sandbox := f.sandboxes[sandboxID]
	return Instance{
		SandboxID:          sandboxID,
		State:              sandbox.State,
		Version:            sandbox.Version,
		Generation:         sandbox.Generation,
		PendingOperationID: f.pending[sandboxID],
	}
}

func (f *Fake) remainingLocked() lifecycle.ResourceVector {
	return lifecycle.ResourceVector{
		MilliCPU:    f.available.MilliCPU - f.used.MilliCPU,
		MemoryBytes: f.available.MemoryBytes - f.used.MemoryBytes,
		VolumeBytes: f.available.VolumeBytes - f.used.VolumeBytes,
	}
}

func (f *Fake) addUsedLocked(resources lifecycle.ResourceVector) {
	f.used.MilliCPU += resources.MilliCPU
	f.used.MemoryBytes += resources.MemoryBytes
	f.used.VolumeBytes += resources.VolumeBytes
}

func (f *Fake) releaseLocked(sandboxID string) {
	resources, ok := f.resources[sandboxID]
	if !ok {
		return
	}
	f.used.MilliCPU -= resources.MilliCPU
	f.used.MemoryBytes -= resources.MemoryBytes
	f.used.VolumeBytes -= resources.VolumeBytes
	delete(f.resources, sandboxID)
}

func operationComplete(trigger lifecycle.Trigger, state lifecycle.State) bool {
	switch trigger {
	case "create":
		return state == "Active" || state == "Error"
	case "suspend":
		return state == "Suspend" || state == "Error"
	case "resume":
		return state == "Active" || state == "Error"
	case "destroy":
		return state == "Destroyed" || state == "Error"
	case "set_idle":
		return state == "Idle" || state == "Error"
	case "set_active":
		return state == "Active" || state == "Error"
	case "memory_termination_confirmed", "lease_expiry":
		return state == "Error" || state == "Lost"
	default:
		return false
	}
}

func checkContext(ctx context.Context) error {
	if ctx == nil {
		return fmt.Errorf("%w: context is required", ErrInvalidRequest)
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("%w: %w", ErrInvalidRequest, err)
	}
	return nil
}
