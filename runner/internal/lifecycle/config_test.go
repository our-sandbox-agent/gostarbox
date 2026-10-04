package lifecycle

import (
	"errors"
	"testing"
)

func validSpec() SandboxSpec {
	managementUID := uint64(2000)
	return SandboxSpec{
		Agent:     "claude",
		Resources: ResourceVector{MilliCPU: 1000, MemoryBytes: 2 << 30, VolumeBytes: 10 << 30},
		PID: PIDPolicy{
			WorkloadUID:         1000,
			ManagementUID:       &managementUID,
			GuestPidsLimit:      64,
			NprocSoft:           64,
			NprocHard:           64,
			HostPidsMax:         256,
			DropAllCapabilities: true,
			NoNewPrivileges:     true,
		},
	}
}

func TestValidateSandboxSpecAcceptsCandidateWithoutTreatingItAsGuarantee(t *testing.T) {
	t.Parallel()

	spec := validSpec()
	if spec.PID.HostPidsMax != 2*spec.PID.GuestPidsLimit+128 {
		t.Fatal("test fixture should use the documented candidate only")
	}
	if err := ValidateSandboxSpec(spec); err != nil {
		t.Fatalf("ValidateSandboxSpec: %v", err)
	}

	// The candidate formula is not a product invariant. A larger confirmed
	// host backstop must also be valid.
	spec.PID.GuestPidsLimit = 128
	spec.PID.NprocSoft = 128
	spec.PID.NprocHard = 128
	spec.PID.HostPidsMax = 512
	if err := ValidateSandboxSpec(spec); err != nil {
		t.Fatalf("ValidateSandboxSpec(non-candidate backstop): %v", err)
	}
}

func TestValidateSandboxSpecRejectsEveryHardInvariant(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name   string
		mutate func(*SandboxSpec)
		field  string
	}{
		{
			name: "unsupported agent",
			mutate: func(s *SandboxSpec) {
				s.Agent = "codex"
			},
			field: "agent",
		},
		{
			name: "zero cpu",
			mutate: func(s *SandboxSpec) {
				s.Resources.MilliCPU = 0
			},
			field: "milli_cpu",
		},
		{
			name: "negative memory",
			mutate: func(s *SandboxSpec) {
				s.Resources.MemoryBytes = -1
			},
			field: "memory_bytes",
		},
		{
			name: "zero volume",
			mutate: func(s *SandboxSpec) {
				s.Resources.VolumeBytes = 0
			},
			field: "volume_bytes",
		},
		{
			name: "root workload uid",
			mutate: func(s *SandboxSpec) {
				s.PID.WorkloadUID = 0
			},
			field: "pid.workload_uid",
		},
		{
			name: "zero management uid",
			mutate: func(s *SandboxSpec) {
				uid := uint64(0)
				s.PID.ManagementUID = &uid
			},
			field: "pid.management_uid",
		},
		{
			name: "management uid equals workload uid",
			mutate: func(s *SandboxSpec) {
				uid := uint64(1000)
				s.PID.ManagementUID = &uid
			},
			field: "pid.management_uid",
		},
		{
			name: "missing nproc",
			mutate: func(s *SandboxSpec) {
				s.PID.NprocSoft = 0
				s.PID.NprocHard = 0
			},
			field: "pid.nproc",
		},
		{
			name: "soft differs from hard",
			mutate: func(s *SandboxSpec) {
				s.PID.NprocSoft = 63
			},
			field: "pid.nproc",
		},
		{
			name: "pids limit missing",
			mutate: func(s *SandboxSpec) {
				s.PID.GuestPidsLimit = 0
			},
			field: "pid.guest_pids_limit",
		},
		{
			name: "pids limit disagrees with nproc",
			mutate: func(s *SandboxSpec) {
				s.PID.GuestPidsLimit = 63
			},
			field: "pid.guest_pids_limit",
		},
		{
			name: "host backstop equals guest limit",
			mutate: func(s *SandboxSpec) {
				s.PID.HostPidsMax = 64
			},
			field: "pid.host_pids_max",
		},
		{
			name: "host backstop below guest limit",
			mutate: func(s *SandboxSpec) {
				s.PID.HostPidsMax = 63
			},
			field: "pid.host_pids_max",
		},
		{
			name: "capabilities retained",
			mutate: func(s *SandboxSpec) {
				s.PID.DropAllCapabilities = false
			},
			field: "pid.drop_all_capabilities",
		},
		{
			name: "new privileges allowed",
			mutate: func(s *SandboxSpec) {
				s.PID.NoNewPrivileges = false
			},
			field: "pid.no_new_privileges",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			spec := validSpec()
			test.mutate(&spec)
			err := ValidateSandboxSpec(spec)
			if !errors.Is(err, ErrInvalidSpec) {
				t.Fatalf("error: got %v, want %v", err, ErrInvalidSpec)
			}
			var specErr *SpecValidationError
			if test.field != "milli_cpu" && test.field != "memory_bytes" && test.field != "volume_bytes" {
				if !errors.As(err, &specErr) {
					t.Fatalf("error is not SpecValidationError: %T", err)
				}
				if specErr.Field != test.field {
					t.Fatalf("field: got %q, want %q", specErr.Field, test.field)
				}
			}
		})
	}
}

func TestAdmitCapacityUsesRunnerContractRefusal(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	request := ResourceVector{MilliCPU: 500, MemoryBytes: 1024, VolumeBytes: 4096}
	available := ResourceVector{MilliCPU: 1000, MemoryBytes: 2048, VolumeBytes: 8192}
	for _, trigger := range []Trigger{"create", "resume"} {
		if err := contract.AdmitCapacity(trigger, request, available); err != nil {
			t.Fatalf("AdmitCapacity(%s): %v", trigger, err)
		}
	}

	available.MilliCPU = 499
	err := contract.AdmitCapacity("create", request, available)
	var refusal *RefusalError
	if !errors.As(err, &refusal) {
		t.Fatalf("error: got %v, want %T", err, refusal)
	}
	if refusal.ID != "capacity-exceeded" || refusal.Code != "capacity_exceeded" {
		t.Fatalf("unexpected refusal: %+v", refusal)
	}
	if refusal.HTTPStatus != 409 {
		t.Fatalf("runner HTTP status: got %d, want 409", refusal.HTTPStatus)
	}
	if refusal.HTTPStatus == 429 {
		t.Fatal("runner-side contract status must not be silently rewritten to the control-plane edge status")
	}
	if refusal.CreatesOperation() {
		t.Fatal("capacity refusal must not create an operation")
	}
}

func TestAdmitCapacityRejectsUnsupportedTriggerAndMalformedVectors(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	request := ResourceVector{MilliCPU: 1, MemoryBytes: 1, VolumeBytes: 1}
	available := ResourceVector{MilliCPU: 1, MemoryBytes: 1, VolumeBytes: 1}

	if err := contract.AdmitCapacity("suspend", request, available); !errors.Is(
		err, ErrUnsupportedCapacityTrigger,
	) {
		t.Fatalf("unsupported trigger: got %v", err)
	}
	if err := contract.AdmitCapacity("create", ResourceVector{}, available); !errors.Is(
		err, ErrInvalidResourceVector,
	) {
		t.Fatalf("invalid request: got %v", err)
	}
	negative := ResourceVector{MilliCPU: -1, MemoryBytes: 1, VolumeBytes: 1}
	if err := contract.AdmitCapacity("resume", request, negative); !errors.Is(
		err, ErrInvalidResourceVector,
	) {
		t.Fatalf("invalid available capacity: got %v", err)
	}
}

func TestRefusalForErrorUsesExactContractRows(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	tests := []struct {
		name       string
		err        error
		id         string
		httpStatus int
	}{
		{
			name:       "version conflict",
			err:        ErrVersionConflict,
			id:         "version-or-operation-conflict",
			httpStatus: 409,
		},
		{
			name:       "stale generation has no direct HTTP status",
			err:        ErrStaleGeneration,
			id:         "stale-generation",
			httpStatus: 0,
		},
		{
			name:       "unknown operation",
			err:        ErrUnknownTrigger,
			id:         "unknown-operation",
			httpStatus: 409,
		},
		{
			name:       "create validation",
			err:        ValidateSandboxSpec(SandboxSpec{Agent: "codex"}),
			id:         "create-validation",
			httpStatus: 422,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			refusal, ok := contract.RefusalForError(test.err)
			if !ok {
				t.Fatalf("no refusal mapped for %v", test.err)
			}
			if refusal.ID != test.id {
				t.Fatalf("ID: got %q, want %q", refusal.ID, test.id)
			}
			if refusal.HTTPStatus != test.httpStatus {
				t.Fatalf("HTTP status: got %d, want %d", refusal.HTTPStatus, test.httpStatus)
			}
			if refusal.CreatesOperation() {
				t.Fatal("mapped refusal must not create an operation")
			}
		})
	}

	if _, ok := contract.RefusalForError(ErrTerminalState); ok {
		t.Fatal("terminal replay must not be inventively mapped as an unknown operation")
	}
}

func TestNewRefusalRejectsUnknownAndOperationCreatingRows(t *testing.T) {
	t.Parallel()

	contract := goldenContract(t)
	if _, err := contract.NewRefusal("does-not-exist", "message"); !errors.Is(
		err, ErrInvalidContract,
	) {
		t.Fatalf("unknown refusal: got %v", err)
	}

	creates := true
	contract.Refusals[0].CreatesOperation = &creates
	if _, err := contract.NewRefusal(contract.Refusals[0].ID, "message"); !errors.Is(
		err, ErrInvalidContract,
	) {
		t.Fatalf("operation-creating refusal: got %v", err)
	}
}
