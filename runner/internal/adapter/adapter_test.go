package adapter

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"github.com/our-sandbox-agent/gostarbox/runner/internal/lifecycle"
)

func testContract(t *testing.T) *lifecycle.Contract {
	t.Helper()
	path := filepath.Join("..", "..", "..", "docs", "contracts", "runner-lifecycle.json")
	contract, err := lifecycle.Load(path)
	if err != nil {
		t.Fatalf("load lifecycle contract: %v", err)
	}
	return contract
}

func testSpec() lifecycle.SandboxSpec {
	return lifecycle.SandboxSpec{
		Agent:     "claude",
		Resources: lifecycle.ResourceVector{MilliCPU: 500, MemoryBytes: 1000, VolumeBytes: 2000},
		PID: lifecycle.PIDPolicy{
			WorkloadUID:         1000,
			GuestPidsLimit:      64,
			NprocSoft:           64,
			NprocHard:           64,
			HostPidsMax:         256,
			DropAllCapabilities: true,
			NoNewPrivileges:     true,
		},
	}
}

func testFake(t *testing.T, available lifecycle.ResourceVector) *Fake {
	t.Helper()
	fake, err := NewFake(testContract(t), available)
	if err != nil {
		t.Fatalf("NewFake: %v", err)
	}
	return fake
}

func TestFakeCreateValidatesAndIsIdempotent(t *testing.T) {
	t.Parallel()

	fake := testFake(t, lifecycle.ResourceVector{
		MilliCPU: 1000, MemoryBytes: 2000, VolumeBytes: 4000,
	})
	spec := testSpec()
	invalid := spec
	invalid.Agent = "codex"
	if _, err := fake.Create(context.Background(), CreateRequest{
		SandboxID: "invalid", OperationID: "op-invalid", Spec: invalid,
	}); !errors.Is(err, lifecycle.ErrInvalidSpec) {
		t.Fatalf("invalid spec: got %v", err)
	}

	created, err := fake.Create(context.Background(), CreateRequest{
		SandboxID: "sbx-1", OperationID: "op-create-1", Spec: spec,
	})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if created.State != "Creating" || created.Version != 1 || created.Generation != 1 ||
		created.PendingOperationID != "op-create-1" {
		t.Fatalf("unexpected created instance: %+v", created)
	}

	replayed, err := fake.Create(context.Background(), CreateRequest{
		SandboxID: "sbx-1", OperationID: "op-create-1", Spec: spec,
	})
	if err != nil {
		t.Fatalf("replay Create: %v", err)
	}
	if replayed != created {
		t.Fatalf("replay changed instance: got %+v, want %+v", replayed, created)
	}

	if _, err := fake.Create(context.Background(), CreateRequest{
		SandboxID: "sbx-other", OperationID: "op-create-1", Spec: spec,
	}); !errors.Is(err, ErrOperationMismatch) {
		t.Fatalf("cross-sandbox operation replay: got %v", err)
	}
	if _, err := fake.Create(context.Background(), CreateRequest{
		SandboxID: "sbx-1", OperationID: "op-create-2", Spec: spec,
	}); !errors.Is(err, ErrSandboxExists) {
		t.Fatalf("duplicate sandbox: got %v", err)
	}
}

func TestFakeCapacityReservesAndReleasesOnlyAfterConfirmedDestroy(t *testing.T) {
	t.Parallel()

	spec := testSpec()
	exactlyOne := lifecycle.ResourceVector{
		MilliCPU:    spec.Resources.MilliCPU,
		MemoryBytes: spec.Resources.MemoryBytes,
		VolumeBytes: spec.Resources.VolumeBytes,
	}
	fake := testFake(t, exactlyOne)
	ctx := context.Background()

	first, err := fake.Create(ctx, CreateRequest{
		SandboxID: "sbx-1", OperationID: "op-create-1", Spec: spec,
	})
	if err != nil {
		t.Fatalf("first create: %v", err)
	}
	if _, err := fake.Create(ctx, CreateRequest{
		SandboxID: "sbx-2", OperationID: "op-create-2", Spec: spec,
	}); err == nil {
		t.Fatal("second create unexpectedly admitted")
	} else {
		var refusal *lifecycle.RefusalError
		if !errors.As(err, &refusal) || refusal.Code != "capacity_exceeded" {
			t.Fatalf("second create should be capacity refusal, got %v", err)
		}
	}

	active, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-create-1", Trigger: "create",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: first.Version,
		Generation: first.Generation,
	})
	if err != nil {
		t.Fatalf("confirm create: %v", err)
	}
	if _, err := fake.Submit(ctx, SubmitRequest{
		SandboxID: "sbx-1", OperationID: "op-destroy-1", Trigger: "destroy",
		ExpectedVersion: active.Version, Generation: active.Generation,
	}); err != nil {
		t.Fatalf("submit destroy: %v", err)
	}
	destroying, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-destroy-1", Trigger: "destroy",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: active.Version,
		Generation: active.Generation,
	})
	if err != nil {
		t.Fatalf("confirm destroy first phase: %v", err)
	}
	if destroying.State != "Destroying" || destroying.PendingOperationID != "op-destroy-1" {
		t.Fatalf("unexpected intermediate destroy: %+v", destroying)
	}
	if _, err := fake.Create(ctx, CreateRequest{
		SandboxID: "sbx-2", OperationID: "op-create-2", Spec: spec,
	}); err == nil {
		t.Fatal("capacity unexpectedly released while Destroying")
	} else {
		var refusal *lifecycle.RefusalError
		if !errors.As(err, &refusal) || refusal.Code != "capacity_exceeded" {
			t.Fatalf("capacity must remain reserved while Destroying, got %v", err)
		}
	}

	destroyed, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-destroy-1", Trigger: "destroy",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: destroying.Version,
		Generation: destroying.Generation,
	})
	if err != nil {
		t.Fatalf("confirm destroy final phase: %v", err)
	}
	if destroyed.State != "Destroyed" || destroyed.PendingOperationID != "" {
		t.Fatalf("unexpected final destroy: %+v", destroyed)
	}
	if _, err := fake.Create(ctx, CreateRequest{
		SandboxID: "sbx-2", OperationID: "op-create-2", Spec: spec,
	}); err != nil {
		t.Fatalf("create after confirmed release: %v", err)
	}
}

func TestFakeCreateSuspendResumeAdvancesGeneration(t *testing.T) {
	t.Parallel()

	fake := testFake(t, lifecycle.ResourceVector{
		MilliCPU: 1000, MemoryBytes: 2000, VolumeBytes: 4000,
	})
	ctx := context.Background()
	spec := testSpec()

	creating, err := fake.Create(ctx, CreateRequest{
		SandboxID: "sbx-1", OperationID: "op-create", Spec: spec,
	})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	active, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-create", Trigger: "create",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: creating.Version,
		Generation: creating.Generation,
	})
	if err != nil {
		t.Fatalf("confirm create: %v", err)
	}
	if active.State != "Active" || active.Version != 2 || active.Generation != 1 ||
		active.PendingOperationID != "" {
		t.Fatalf("unexpected active instance: %+v", active)
	}

	if _, err := fake.Submit(ctx, SubmitRequest{
		SandboxID: "sbx-1", OperationID: "op-suspend", Trigger: "suspend",
		ExpectedVersion: active.Version, Generation: active.Generation,
	}); err != nil {
		t.Fatalf("submit suspend: %v", err)
	}
	suspending, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-suspend", Trigger: "suspend",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: active.Version,
		Generation: active.Generation,
	})
	if err != nil {
		t.Fatalf("confirm suspend first phase: %v", err)
	}
	suspended, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-suspend", Trigger: "suspend",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: suspending.Version,
		Generation: suspending.Generation,
	})
	if err != nil {
		t.Fatalf("confirm suspend final phase: %v", err)
	}
	if suspended.State != "Suspend" || suspended.Version != 4 || suspended.Generation != 1 ||
		suspended.PendingOperationID != "" {
		t.Fatalf("unexpected suspended instance: %+v", suspended)
	}

	if _, err := fake.Submit(ctx, SubmitRequest{
		SandboxID: "sbx-1", OperationID: "op-resume", Trigger: "resume",
		ExpectedVersion: suspended.Version, Generation: suspended.Generation,
	}); err != nil {
		t.Fatalf("submit resume: %v", err)
	}
	resuming, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-resume", Trigger: "resume",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: suspended.Version,
		Generation: suspended.Generation,
	})
	if err != nil {
		t.Fatalf("confirm resume first phase: %v", err)
	}
	if resuming.Generation != 2 {
		t.Fatalf("cold resume generation: got %d, want 2", resuming.Generation)
	}
	if _, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-resume", Trigger: "resume",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: resuming.Version,
		Generation: 1,
	}); !errors.Is(err, lifecycle.ErrStaleGeneration) {
		t.Fatalf("stale generation: got %v", err)
	}
	resumed, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-resume", Trigger: "resume",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: resuming.Version,
		Generation: resuming.Generation,
	})
	if err != nil {
		t.Fatalf("confirm resume final phase: %v", err)
	}
	if resumed.State != "Active" || resumed.Version != 6 || resumed.Generation != 2 {
		t.Fatalf("unexpected resumed instance: %+v", resumed)
	}
}

func TestFakeSubmitEnforcesSinglePendingOperationAndFencing(t *testing.T) {
	t.Parallel()

	fake := testFake(t, lifecycle.ResourceVector{
		MilliCPU: 1000, MemoryBytes: 2000, VolumeBytes: 4000,
	})
	ctx := context.Background()
	creating, err := fake.Create(ctx, CreateRequest{
		SandboxID: "sbx-1", OperationID: "op-create", Spec: testSpec(),
	})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	active, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-create", Trigger: "create",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: creating.Version,
		Generation: creating.Generation,
	})
	if err != nil {
		t.Fatalf("confirm create: %v", err)
	}

	submitted, err := fake.Submit(ctx, SubmitRequest{
		SandboxID: "sbx-1", OperationID: "op-suspend", Trigger: "suspend",
		ExpectedVersion: active.Version, Generation: active.Generation,
	})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	if submitted.PendingOperationID != "op-suspend" {
		t.Fatalf("pending operation: got %q", submitted.PendingOperationID)
	}
	if _, err := fake.Submit(ctx, SubmitRequest{
		SandboxID: "sbx-1", OperationID: "op-destroy", Trigger: "destroy",
		ExpectedVersion: active.Version, Generation: active.Generation,
	}); !errors.Is(err, ErrOperationPending) {
		t.Fatalf("conflicting submit: got %v", err)
	}
	if _, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "sbx-1", OperationID: "op-suspend", Trigger: "suspend",
		Outcome: lifecycle.OutcomeSuccess, ExpectedVersion: active.Version + 1,
		Generation: active.Generation,
	}); !errors.Is(err, lifecycle.ErrVersionConflict) {
		t.Fatalf("version fencing: got %v", err)
	}

	inspected, err := fake.Inspect(ctx, "sbx-1")
	if err != nil {
		t.Fatalf("Inspect: %v", err)
	}
	if inspected != submitted {
		t.Fatalf("failed confirmation changed instance: got %+v, want %+v", inspected, submitted)
	}
}

func TestFakeRequestAndLookupErrors(t *testing.T) {
	t.Parallel()

	fake := testFake(t, lifecycle.ResourceVector{
		MilliCPU: 1000, MemoryBytes: 2000, VolumeBytes: 4000,
	})
	ctx := context.Background()
	if _, err := fake.Inspect(ctx, "missing"); !errors.Is(err, ErrSandboxNotFound) {
		t.Fatalf("missing sandbox: got %v", err)
	}
	if _, err := fake.Confirm(ctx, ConfirmRequest{
		SandboxID: "missing", OperationID: "missing", Trigger: "create",
	}); !errors.Is(err, ErrOperationNotFound) {
		t.Fatalf("missing operation: got %v", err)
	}
	if _, err := fake.Submit(ctx, SubmitRequest{}); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("empty submit: got %v", err)
	}

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := fake.Inspect(cancelled, "any"); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("cancelled context: got %v", err)
	}
}

func TestFakeCreateRejectsChangedIdentity(t *testing.T) {
	changes := map[string]func(*lifecycle.SandboxSpec){
		"cpu":            func(s *lifecycle.SandboxSpec) { s.Resources.MilliCPU++ },
		"memory":         func(s *lifecycle.SandboxSpec) { s.Resources.MemoryBytes++ },
		"volume":         func(s *lifecycle.SandboxSpec) { s.Resources.VolumeBytes++ },
		"workload uid":   func(s *lifecycle.SandboxSpec) { s.PID.WorkloadUID++ },
		"management uid": func(s *lifecycle.SandboxSpec) { uid := uint64(2000); s.PID.ManagementUID = &uid },
		"host pids":      func(s *lifecycle.SandboxSpec) { s.PID.HostPidsMax++ },
		"guest policy":   func(s *lifecycle.SandboxSpec) { s.PID.GuestPidsLimit++; s.PID.NprocSoft++; s.PID.NprocHard++ },
	}
	for name, change := range changes {
		t.Run(name, func(t *testing.T) {
			fake := testFake(t, lifecycle.ResourceVector{MilliCPU: 1000, MemoryBytes: 2000, VolumeBytes: 4000})
			request := CreateRequest{SandboxID: "sbx", OperationID: "create", Spec: testSpec()}
			original, err := fake.Create(context.Background(), request)
			if err != nil {
				t.Fatal(err)
			}
			change(&request.Spec)
			if _, err := fake.Create(context.Background(), request); !errors.Is(err, ErrOperationMismatch) {
				t.Fatalf("changed identity accepted: %v", err)
			}
			got, err := fake.Inspect(context.Background(), "sbx")
			if err != nil || got != original {
				t.Fatalf("rejection changed state: %+v %v", got, err)
			}
		})
	}
}

func TestFakeCreateOwnsManagementUIDAndComparesValues(t *testing.T) {
	fake := testFake(t, lifecycle.ResourceVector{MilliCPU: 1000, MemoryBytes: 2000, VolumeBytes: 4000})
	uid := uint64(2000)
	request := CreateRequest{SandboxID: "sbx", OperationID: "create", Spec: testSpec()}
	request.Spec.PID.ManagementUID = &uid
	original, err := fake.Create(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	uid = 3000
	if _, err := fake.Create(context.Background(), request); !errors.Is(err, ErrOperationMismatch) {
		t.Fatalf("caller mutation changed stored identity: %v", err)
	}
	separateUID := uint64(2000)
	request.Spec.PID.ManagementUID = &separateUID
	replayed, err := fake.Create(context.Background(), request)
	if err != nil || replayed != original {
		t.Fatalf("equal UID value replay rejected: %+v %v", replayed, err)
	}
}

func TestFakeCreateReplayStillRejectsInvalidSecurityPolicy(t *testing.T) {
	for _, field := range []string{"capabilities", "privileges"} {
		t.Run(field, func(t *testing.T) {
			fake := testFake(t, lifecycle.ResourceVector{MilliCPU: 1000, MemoryBytes: 2000, VolumeBytes: 4000})
			request := CreateRequest{SandboxID: "sbx", OperationID: "create", Spec: testSpec()}
			if _, err := fake.Create(context.Background(), request); err != nil {
				t.Fatal(err)
			}
			if field == "capabilities" {
				request.Spec.PID.DropAllCapabilities = false
			} else {
				request.Spec.PID.NoNewPrivileges = false
			}
			if _, err := fake.Create(context.Background(), request); !errors.Is(err, lifecycle.ErrInvalidSpec) {
				t.Fatalf("invalid security replay accepted: %v", err)
			}
		})
	}
}
