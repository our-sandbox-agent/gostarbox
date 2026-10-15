// Shared record shapes and the synchronous in-memory API test adapter.
// Async durable storage lives in postgres.ts; HTTP transaction wiring is #136.

export interface Resources {
  milli_cpu: number
  memory_bytes: number
  volume_bytes: number
}

export interface SandboxRecord {
  sandbox_id: string
  workspace_id: string
  desired_state: string
  observed_state: string
  generation: number
  version: number
  resources: Resources
  repo_url: string | null
  runtime_deadline_at: number | null
  volumes: { workspace: string; home: string }
  session: { id: string; cwd: string } | null
  pending_operation: string | null
  last_confirmed_at: number | null
  error: { code: string; resources: string[] } | null
  last_confirmed_state: string | null
  reconciled: boolean
  destroy_operation: string | null
  has_credential: boolean
  created_at: number
}

export interface OperationRecord {
  operation_id: string
  sandbox_id: string
  type: 'create' | 'set_state' | 'destroy'
  trigger: string
  target: Record<string, unknown>
  state: 'pending' | 'running' | 'succeeded' | 'failed'
  expected_version: number | null
  generation: number
  result: { observed_state: string; generation: number } | null
  error: { code: string; message: string } | null
  created_at: number
  updated_at: number
}

export interface IdempotencyEntry {
  key: string
  method: string
  path: string
  fingerprint: string
  status: number
  operation_id: string | null
  sandbox_id: string
  response?: unknown
}

export interface TicketRecord {
  ticket: string
  sandbox_id: string
  generation: number
  created_at: number
}

// Serializable state for restart simulation. Contains no secrets: the
// credential is only ever a has_credential boolean on the sandbox record,
// and tickets are deliberately absent (a restart drops attaches).
export interface Snapshot {
  clock: number
  seq: number
  sandboxes: Record<string, SandboxRecord>
  operations: Record<string, OperationRecord>
  idempotency: IdempotencyEntry[]
}

export interface PersistenceAdapter {
  // id / logical-clock allocation (Postgres: sequences / tx timestamps)
  nextId(prefix: string): string
  tick(): number
  now(): number
  // sandboxes
  getSandbox(id: string): SandboxRecord | undefined
  listSandboxes(): SandboxRecord[]
  putSandbox(sandbox: SandboxRecord): void
  // operations
  getOperation(id: string): OperationRecord | undefined
  putOperation(operation: OperationRecord): void
  // idempotency (persisted: replays must stay answerable after a restart)
  getIdempotency(key: string): IdempotencyEntry | undefined
  putIdempotency(entry: IdempotencyEntry): void
  // terminal tickets (never snapshotted: invalidated by restart)
  putTicket(ticket: TicketRecord): void
  // restart semantics
  snapshot(): Snapshot
  restore(snapshot: Snapshot): void
}

export class MemoryAdapter implements PersistenceAdapter {
  private clock = 0
  private seq = 0
  private sandboxes = new Map<string, SandboxRecord>()
  private operations = new Map<string, OperationRecord>()
  private idempotency = new Map<string, IdempotencyEntry>()
  private tickets = new Map<string, TicketRecord>()

  nextId(prefix: string): string {
    this.seq += 1
    return `${prefix}_${String(this.seq).padStart(6, '0')}`
  }

  tick(): number {
    this.clock += 1
    return this.clock
  }

  now(): number {
    return this.clock
  }

  getSandbox(id: string): SandboxRecord | undefined {
    return this.sandboxes.get(id)
  }

  listSandboxes(): SandboxRecord[] {
    return [...this.sandboxes.values()]
  }

  putSandbox(sandbox: SandboxRecord): void {
    this.sandboxes.set(sandbox.sandbox_id, sandbox)
  }

  getOperation(id: string): OperationRecord | undefined {
    return this.operations.get(id)
  }

  putOperation(operation: OperationRecord): void {
    this.operations.set(operation.operation_id, operation)
  }

  getIdempotency(key: string): IdempotencyEntry | undefined {
    return this.idempotency.get(key)
  }

  putIdempotency(entry: IdempotencyEntry): void {
    this.idempotency.set(entry.key, entry)
  }

  putTicket(ticket: TicketRecord): void {
    this.tickets.set(ticket.ticket, ticket)
  }

  snapshot(): Snapshot {
    return structuredClone({
      clock: this.clock,
      seq: this.seq,
      sandboxes: Object.fromEntries(this.sandboxes),
      operations: Object.fromEntries(this.operations),
      idempotency: [...this.idempotency.values()],
    })
  }

  restore(snapshot: Snapshot): void {
    this.clock = snapshot.clock
    this.seq = snapshot.seq
    this.sandboxes = new Map(Object.entries(snapshot.sandboxes))
    this.operations = new Map(Object.entries(snapshot.operations))
    this.idempotency = new Map(snapshot.idempotency.map((e) => [e.key, e]))
    this.tickets = new Map()
  }
}
