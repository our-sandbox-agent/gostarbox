package lifecycle

import (
	"errors"
	"fmt"
	"strings"
)

var (
	ErrInvalidSpec                = errors.New("invalid sandbox specification")
	ErrInvalidResourceVector      = errors.New("invalid resource vector")
	ErrUnsupportedCapacityTrigger = errors.New("capacity admission trigger is not enabled by the contract")
)

// ResourceVector is the exact admission vector named by the lifecycle
// contract. Integer values avoid floating-point accounting.
type ResourceVector struct {
	MilliCPU    int64
	MemoryBytes int64
	VolumeBytes int64
}

// PIDPolicy captures the pre-runtime invariants learned by #8/#70/#71. It is
// configuration validation only; setting these fields does not prove that a
// container, rlimit, cgroup, or UID boundary was applied.
type PIDPolicy struct {
	// WorkloadUID must be non-root. A single workload UID is required.
	WorkloadUID uint64
	// ManagementUID may optionally model the proposed host-only second UID.
	// It must differ from WorkloadUID. This syntactic check grants no guest
	// or host authorization.
	ManagementUID *uint64

	// GuestPidsLimit is the guest-side cgroup/container PID limit.
	GuestPidsLimit uint64
	// NprocSoft and NprocHard are the guest ulimit values. Both are required;
	// a pids limit alone is not sufficient.
	NprocSoft uint64
	NprocHard uint64
	// HostPidsMax is the host backstop. It must exceed the guest limit.
	// H=2N+128 is only a research candidate, not a formula enforced or
	// promoted into a product capacity guarantee by this package.
	HostPidsMax uint64

	DropAllCapabilities bool
	NoNewPrivileges     bool
}

// SandboxSpec is the pure, pre-runtime create/resume specification. It does
// not create a runtime resource or validate repository contents.
type SandboxSpec struct {
	Agent     string
	Resources ResourceVector
	PID       PIDPolicy
}

// SpecValidationError reports the first invalid specification field.
type SpecValidationError struct {
	Field  string
	Reason string
}

func (e *SpecValidationError) Error() string {
	return fmt.Sprintf("%s: %s: %s", ErrInvalidSpec, e.Field, e.Reason)
}

func (e *SpecValidationError) Unwrap() error {
	return ErrInvalidSpec
}

// ValidateSandboxSpec enforces create shape and the hard PID invariants. It
// intentionally does not invent quotas, host capacity, or management identity
// authorization.
func ValidateSandboxSpec(spec SandboxSpec) error {
	if spec.Agent != "claude" {
		return invalidSpec("agent", "only claude is supported by the first version")
	}
	if err := validateRequestVector(spec.Resources); err != nil {
		return fmt.Errorf("%w: %w", ErrInvalidSpec, err)
	}

	pid := spec.PID
	if pid.WorkloadUID == 0 {
		return invalidSpec("pid.workload_uid", "workload must use a non-root UID")
	}
	if pid.ManagementUID != nil {
		if *pid.ManagementUID == 0 {
			return invalidSpec("pid.management_uid", "host-only management UID must be non-zero")
		}
		if *pid.ManagementUID == pid.WorkloadUID {
			return invalidSpec("pid.management_uid", "management UID must differ from the single workload UID")
		}
	}
	if pid.NprocSoft == 0 || pid.NprocHard == 0 {
		return invalidSpec("pid.nproc", "guest nproc soft and hard limits are both required")
	}
	if pid.NprocSoft != pid.NprocHard {
		return invalidSpec("pid.nproc", "soft must equal hard")
	}
	if pid.GuestPidsLimit == 0 {
		return invalidSpec("pid.guest_pids_limit", "guest pids limit is required")
	}
	if pid.GuestPidsLimit != pid.NprocSoft || pid.GuestPidsLimit != pid.NprocHard {
		return invalidSpec("pid.guest_pids_limit", "guest pids limit and nproc limits must agree")
	}
	if pid.HostPidsMax <= pid.NprocHard {
		return invalidSpec("pid.host_pids_max", "host backstop must be greater than the guest limit")
	}
	if !pid.DropAllCapabilities {
		return invalidSpec("pid.drop_all_capabilities", "all guest capabilities must be dropped")
	}
	if !pid.NoNewPrivileges {
		return invalidSpec("pid.no_new_privileges", "no-new-privileges must be enabled")
	}
	return nil
}

// RefusalError is a typed, no-operation contract refusal. HTTPStatus zero
// means that the contract deliberately defines no direct HTTP status.
type RefusalError struct {
	ID          string
	Code        string
	HTTPStatus  int
	Message     string
	ContractRow Refusal
}

func (e *RefusalError) Error() string {
	if e.Code != "" {
		return fmt.Sprintf("%s: %s: %s", e.ID, e.Code, e.Message)
	}
	return e.ID + ": " + e.Message
}

// CreatesOperation always reports false. Keeping this explicit prevents an
// adapter from accidentally treating a refusal as pending work.
func (e *RefusalError) CreatesOperation() bool {
	return false
}

// NewRefusal returns a typed refusal loaded by ID from the JSON contract.
func (c *Contract) NewRefusal(id, message string) (*RefusalError, error) {
	if c == nil {
		return nil, ErrContractRequired
	}
	if strings.TrimSpace(id) == "" {
		return nil, fmt.Errorf("%w: refusal ID is empty", ErrInvalidContract)
	}
	for _, row := range c.Refusals {
		if row.ID != id {
			continue
		}
		if row.CreatesOperation == nil || *row.CreatesOperation {
			return nil, fmt.Errorf("%w: refusal %s must not create an operation", ErrInvalidContract, id)
		}
		status := 0
		if row.HTTPStatus != nil {
			status = *row.HTTPStatus
		}
		return &RefusalError{
			ID:          row.ID,
			Code:        row.Code,
			HTTPStatus:  status,
			Message:     message,
			ContractRow: row,
		}, nil
	}
	return nil, fmt.Errorf("%w: unknown refusal %s", ErrInvalidContract, id)
}

// RefusalForError maps pure lifecycle errors to their contract refusal rows.
// It returns false for errors with no exact contract mapping rather than
// inventing an HTTP meaning.
func (c *Contract) RefusalForError(err error) (*RefusalError, bool) {
	if c == nil || err == nil {
		return nil, false
	}

	var id string
	switch {
	case errors.Is(err, ErrVersionConflict):
		id = "version-or-operation-conflict"
	case errors.Is(err, ErrStaleGeneration):
		id = "stale-generation"
	case errors.Is(err, ErrUnknownTrigger):
		id = "unknown-operation"
	case errors.Is(err, ErrInvalidSpec):
		id = "create-validation"
	default:
		return nil, false
	}

	refusal, refusalErr := c.NewRefusal(id, err.Error())
	if refusalErr != nil {
		return nil, false
	}
	return refusal, true
}

// AdmitCapacity performs pure integer admission for a contract-enabled
// trigger. Runner-side success/failure uses the contract's 409
// capacity_exceeded row. The current TypeScript control-plane API
// intentionally exposes 429 at its edge; this method must not silently
// rewrite either side of that documented divergence.
func (c *Contract) AdmitCapacity(
	trigger Trigger,
	request ResourceVector,
	available ResourceVector,
) error {
	if c == nil {
		return ErrContractRequired
	}
	if !containsTrigger(c.CapacityAdmission.CheckedOn, trigger) {
		return fmt.Errorf("%w: %s", ErrUnsupportedCapacityTrigger, trigger)
	}
	if err := validateRequestVector(request); err != nil {
		return err
	}
	if err := validateAvailableVector(available); err != nil {
		return err
	}

	cpu := request.MilliCPU > available.MilliCPU
	memory := request.MemoryBytes > available.MemoryBytes
	volume := request.VolumeBytes > available.VolumeBytes
	if !cpu && !memory && !volume {
		return nil
	}

	fields := make([]string, 0, 3)
	if cpu {
		fields = append(fields, "milli_cpu")
	}
	if memory {
		fields = append(fields, "memory_bytes")
	}
	if volume {
		fields = append(fields, "volume_bytes")
	}
	message := "requested " + strings.Join(fields, ", ") + " exceeds available host capacity"
	refusal, err := c.NewRefusal("capacity-exceeded", message)
	if err != nil {
		return err
	}
	return refusal
}

func validateRequestVector(vector ResourceVector) error {
	if vector.MilliCPU <= 0 {
		return invalidResource("milli_cpu", "request must be positive")
	}
	if vector.MemoryBytes <= 0 {
		return invalidResource("memory_bytes", "request must be positive")
	}
	if vector.VolumeBytes <= 0 {
		return invalidResource("volume_bytes", "request must be positive")
	}
	return nil
}

func validateAvailableVector(vector ResourceVector) error {
	if vector.MilliCPU < 0 || vector.MemoryBytes < 0 || vector.VolumeBytes < 0 {
		return invalidResource("available", "capacity must be non-negative")
	}
	return nil
}

func invalidResource(field, reason string) error {
	return fmt.Errorf("%w: %s: %s", ErrInvalidResourceVector, field, reason)
}

func invalidSpec(field, reason string) error {
	return &SpecValidationError{Field: field, Reason: reason}
}
