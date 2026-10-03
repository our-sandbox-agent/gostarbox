// Control-plane service per docs/contracts/control-plane-api.json.
// TypeScript/Hono port of scripts/control_plane_double.py (same contract,
// same behaviors — the Python suite remains the executable spec).
// The state machine is NOT duplicated here: every observed-state move is
// looked up in docs/contracts/runner-lifecycle.json and refuse-on-unknown
// applies. observed_state changes only through the confirm()/expireLease()/
// markReconciled() evidence hooks (stand-ins for Runner evidence): HTTP 202
// alone never moves a sandbox into a confirmed state.
import { readFileSync } from 'node:fs'
import { Hono, type Context } from 'hono'
import type { ContentfulStatusCode } from 'hono/utils/http-status'
import type {
  IdempotencyEntry,
  OperationRecord,
  PersistenceAdapter,
  Resources,
  SandboxRecord,
  Snapshot,
} from './store.js'

const ACCEPTED_TARGETS = new Set(['Active', 'Idle', 'Suspend'])
const SUSPEND_MODES = new Set(['cold'])
const CONFIRM_SCOPE = 'workspace_and_home_volumes'
// Acceptance-driven intermediate states: entering these is triggered by
// operation acceptance per the runner contract ("requires: operation accepted");
// every other transition is applied only by confirm() (Runner evidence).
const INTERMEDIATE_STATES = new Set(['Creating', 'Suspending', 'Resuming', 'Destroying'])
// ponytail: capacity is derived from observed_state, not an accounting ledger —
// Suspend releases compute only at the confirmed stop, Destroyed at confirmed removal
const COMPUTE_HELD_STATES = new Set([
  'Creating', 'Active', 'Idle', 'Suspending', 'Resuming', 'Destroying', 'Lost', 'Error',
])
const TERMINAL_TICKET_TTL_MS = 60000

// Dev default only: SANDBOX_TOKEN is the real single trusted token.
export const DEV_DEFAULT_TOKEN = 'dev-insecure-token'
export const DEFAULT_CAPACITY: Capacity = {
  milli_cpu: 8000,
  memory_bytes: 16 * 2 ** 30,
  volume_bytes: 200 * 2 ** 30,
}

export interface Capacity {
  milli_cpu: number
  memory_bytes: number
  volume_bytes: number
}

export interface ControlPlaneOptions {
  token?: string
  capacity?: Capacity
  runnerContractPath?: string | URL
}

interface LifecycleDoc {
  initial_state: string
  transitions: { from: string; to: string; trigger: string }[]
}

type ErrorBody = { error: { code: string; message: string } }
type ApiResult = { status: number; body: unknown }

function errBody(code: string, message: string): ErrorBody {
  return { error: { code, message } }
}

function refuse(status: number, code: string, message: string): never {
  throw new Refused(status, errBody(code, message))
}

class Refused extends Error {
  constructor(
    readonly status: number,
    readonly body: ErrorBody,
  ) {
    super(messageOf(body))
  }
}

function messageOf(body: ErrorBody): string {
  return body.error.message
}

function isPositiveInt(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

// Canonical JSON with recursively sorted keys (idempotency fingerprints must
// be order-independent); the credential field never participates.
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort()
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

export interface OperationView {
  operation_id: string
  sandbox_id: string
  type: string
  target: Record<string, unknown>
  state: string
  expected_version: number | null
  generation: number
  result: { observed_state: string; generation: number } | null
  error: { code: string; message: string } | null
  retryable: boolean
  created_at: number
  updated_at: number
}

export class ControlPlane {
  readonly token: string
  readonly capacity: Capacity
  protected readonly store: PersistenceAdapter
  private readonly initialState: string
  // `${from}\u0000${trigger}` -> success targets (a pair can carry both the
  // success target and Error)
  private readonly transitions = new Map<string, string[]>()
  private readonly errorCapable = new Set<string>()

  constructor(store: PersistenceAdapter, opts: ControlPlaneOptions = {}) {
    this.store = store
    this.token = opts.token ?? process.env.SANDBOX_TOKEN ?? DEV_DEFAULT_TOKEN
    this.capacity = opts.capacity ?? DEFAULT_CAPACITY
    const url = opts.runnerContractPath ?? new URL('../../docs/contracts/runner-lifecycle.json', import.meta.url)
    const doc = JSON.parse(readFileSync(url, 'utf8')) as LifecycleDoc
    this.initialState = doc.initial_state
    for (const t of doc.transitions) {
      const key = `${t.from}\u0000${t.trigger}`
      const targets = this.transitions.get(key) ?? []
      targets.push(t.to)
      this.transitions.set(key, targets)
      if (t.to === 'Error') this.errorCapable.add(t.from)
    }
  }

  // ------------------------------------------------------------ endpoints
  createSandbox(body: Record<string, unknown>, key: string | undefined): ApiResult {
    const entry = this.idempotencyCheck('POST', '/v1/sandboxes', key, body)
    if (entry) return this.idempotentReplay(entry)
    if (body.agent !== 'claude') refuse(422, 'invalid', 'only agent=claude is accepted')
    const resources = this.validatedResources(body)
    const repoUrl = body.repo_url
    if (repoUrl !== null && repoUrl !== undefined && (typeof repoUrl !== 'string' || !repoUrl.startsWith('https://'))) {
      refuse(422, 'invalid', 'repo_url must be an https URL')
    }
    const deadline = body.runtime_deadline_at
    if (deadline !== null && deadline !== undefined && !isPositiveInt(deadline)) {
      refuse(422, 'invalid', 'runtime_deadline_at must be a positive UTC ms epoch or null')
    }
    const left = this.capacityLeft()
    if (
      resources.milli_cpu > left.milli_cpu ||
      resources.memory_bytes > left.memory_bytes ||
      resources.volume_bytes > left.volume_bytes
    ) {
      // ponytail: API edge maps admission rejection to 429 per the #76 slice;
      // runner-lifecycle.json records 409 runner-side — reconcile at #11
      refuse(429, 'capacity_exceeded', 'admission rejected: insufficient host capacity')
    }
    const sid = this.store.nextId('sbx')
    const sb: SandboxRecord = {
      sandbox_id: sid,
      workspace_id: 'ws_default',
      desired_state: 'Active',
      observed_state: this.initialState,
      generation: 1,
      version: 1,
      resources,
      repo_url: (repoUrl as string | null | undefined) ?? null,
      runtime_deadline_at: (deadline as number | null | undefined) ?? null,
      volumes: { workspace: this.store.nextId('vol'), home: this.store.nextId('vol') },
      session: null,
      pending_operation: null,
      last_confirmed_at: null,
      error: null,
      last_confirmed_state: null,
      reconciled: false,
      destroy_operation: null,
      has_credential: false,
      created_at: this.store.tick(),
    }
    this.applyCreateObservation(sb)
    const op = this.newOperation(sb, 'create', null, null)
    this.store.putSandbox(sb)
    this.remember(key, 'POST', '/v1/sandboxes', body, 202, op)
    return { status: 202, body: { sandbox_id: sid, operation: this.opView(op) } }
  }

  listSandboxes(): ApiResult {
    return {
      status: 200,
      body: {
        sandboxes: this.store
          .listSandboxes()
          .filter((sb) => sb.observed_state !== 'Destroyed')
          .map((sb) => ({
            sandbox_id: sb.sandbox_id,
            observed_state: sb.observed_state,
            desired_state: sb.desired_state,
            generation: sb.generation,
          })),
      },
    }
  }

  inspect(sandboxId: string): ApiResult {
    return { status: 200, body: this.sandboxView(this.sandbox(sandboxId)) }
  }

  changeState(sandboxId: string, body: Record<string, unknown>, key: string | undefined): ApiResult {
    const sb = this.sandbox(sandboxId)
    const path = `/v1/sandboxes/${sb.sandbox_id}/state`
    const entry = this.idempotencyCheck('POST', path, key, body)
    if (entry) return this.idempotentReplay(entry)
    const target = body.state
    if (target !== 'Active' && target !== 'Idle' && target !== 'Suspend') {
      refuse(422, 'invalid', 'state must be one of Active, Idle, Suspend')
    }
    const rawMode = body.suspend_mode
    const mode = rawMode === undefined ? 'cold' : rawMode
    if (typeof mode !== 'string' || !SUSPEND_MODES.has(mode)) {
      refuse(422, 'unsupported_suspend_mode', 'first-version API accepts suspend_mode=cold only')
    }
    const expected = body.expected_version
    if (typeof expected !== 'number' || !Number.isInteger(expected)) {
      refuse(422, 'invalid', 'expected_version integer required')
    }
    if (!this.versionOk(sb, expected)) {
      refuse(409, 'version_conflict', 'expected_version mismatch; re-read the sandbox')
    }
    if (sb.desired_state === target && sb.observed_state === target && sb.pending_operation === null) {
      return { status: 200, body: { sandbox: this.sandboxView(sb) } }
    }
    const pending = sb.pending_operation ? this.store.getOperation(sb.pending_operation) : undefined
    if (pending) {
      if (pending.target.state === target) {
        this.remember(key, 'POST', path, body, 202, pending)
        return { status: 202, body: { operation: this.opView(pending) } }
      }
      refuse(409, 'opposite_operation', 'another change operation is already pending')
    }
    const refusal = this.stateRefusal(sb, target)
    if (refusal) refuse(refusal.status, refusal.code, refusal.message)
    const trigger = this.triggerFor(sb.observed_state, target)
    if (!this.transitionsFor(sb.observed_state, trigger)) {
      refuse(409, 'opposite_operation', `operation ${target} is not defined for state ${sb.observed_state}`)
    }
    const credential = body.credential
    if (trigger === 'resume' && !credential) {
      refuse(409, 'credentials_required', 'resume needs the credential re-sent; no operation created')
    }
    if (trigger === 'resume') {
      const left = this.capacityLeft()
      if (sb.resources.milli_cpu > left.milli_cpu || sb.resources.memory_bytes > left.memory_bytes) {
        refuse(429, 'capacity_exceeded', 'admission rejected: insufficient host capacity')
      }
    }
    const op = this.newOperation(sb, 'set_state', trigger, { state: target, suspend_mode: mode })
    const intermediate = this.successTarget(sb.observed_state, trigger)
    if (trigger === 'resume') sb.generation += 1 // cold resume allocates a new generation
    if (intermediate !== undefined && INTERMEDIATE_STATES.has(intermediate)) sb.observed_state = intermediate
    sb.desired_state = target
    sb.version += 1
    this.storeCredential(sb, op, credential)
    this.store.putSandbox(sb)
    this.remember(key, 'POST', path, body, 202, op)
    return { status: 202, body: { operation: this.opView(op) } }
  }

  destroy(sandboxId: string, body: Record<string, unknown>, key: string | undefined): ApiResult {
    const sb = this.sandbox(sandboxId)
    const path = `/v1/sandboxes/${sb.sandbox_id}`
    const entry = this.idempotencyCheck('DELETE', path, key, body)
    if (entry) return this.idempotentReplay(entry)
    const expected = body.expected_version
    if (typeof expected !== 'number' || !Number.isInteger(expected)) {
      refuse(422, 'invalid', 'expected_version integer required')
    }
    if (body.confirm_scope !== CONFIRM_SCOPE) {
      refuse(422, 'invalid', `confirm_scope must explicitly be ${CONFIRM_SCOPE}`)
    }
    if (!this.versionOk(sb, expected)) {
      refuse(409, 'version_conflict', 'expected_version mismatch; re-read the sandbox')
    }
    if (sb.observed_state === 'Destroyed' && sb.destroy_operation) {
      const op = this.operation(sb.destroy_operation)
      this.remember(key, 'DELETE', path, body, 202, op)
      return { status: 202, body: { operation: this.opView(op) } } // repeat destroy: same op
    }
    if (sb.observed_state === 'Destroying' && sb.pending_operation) {
      const op = this.store.getOperation(sb.pending_operation)
      if (op?.type === 'destroy') {
        this.remember(key, 'DELETE', path, body, 202, op)
        return { status: 202, body: { operation: this.opView(op) } }
      }
    }
    if (!this.transitionsFor(sb.observed_state, 'destroy')) {
      refuse(409, 'opposite_operation', `destroy is not accepted while ${sb.observed_state}`)
    }
    if ((sb.observed_state === 'Error' || sb.observed_state === 'Lost') && !sb.reconciled) {
      refuse(409, 'opposite_operation', 'reconciliation/fencing must precede destroy')
    }
    const op = this.newOperation(sb, 'destroy', 'destroy', { scope: CONFIRM_SCOPE })
    sb.observed_state = 'Destroying'
    sb.desired_state = 'Destroyed'
    sb.version += 1
    sb.destroy_operation = op.operation_id
    this.store.putSandbox(sb)
    this.remember(key, 'DELETE', path, body, 202, op)
    return { status: 202, body: { operation: this.opView(op) } }
  }

  pollOperation(operationId: string): ApiResult {
    return { status: 200, body: this.opView(this.operation(operationId)) }
  }

  issueTicket(sandboxId: string): ApiResult {
    const sb = this.sandbox(sandboxId)
    if (sb.observed_state === 'Destroyed') refuse(404, 'not_found', 'sandbox not found')
    const ticket = this.store.nextId('tt')
    this.store.putTicket({
      ticket,
      sandbox_id: sb.sandbox_id,
      generation: sb.generation,
      created_at: this.store.now(),
    })
    return {
      status: 200,
      body: {
        ticket,
        sandbox_id: sb.sandbox_id,
        generation: sb.generation,
        expires_in_ms: TERMINAL_TICKET_TTL_MS,
      },
    }
  }

  // ------------------------------------------------- Runner-evidence hooks
  // Stand-ins for Runner evidence until #11; a request timeout never implies
  // not-executed — the operation stays non-terminal until these resolve it.
  advance(operationId: string): OperationView {
    const op = this.operation(operationId)
    if (op.state === 'pending') {
      op.state = 'running'
      op.updated_at = this.store.tick()
      this.store.putOperation(op)
    }
    return this.opView(op)
  }

  confirm(operationId: string, error?: { code?: string; message?: string }): OperationView {
    const op = this.operation(operationId)
    if (op.state === 'succeeded' || op.state === 'failed') return this.opView(op)
    const sb = this.sandbox(op.sandbox_id)
    const trigger = op.trigger
    let toState: string
    if (error !== undefined) {
      if (!this.errorCapable.has(sb.observed_state)) {
        throw new Error(`no ${sb.observed_state}->Error transition in contract`)
      }
      toState = 'Error'
      op.state = 'failed'
      op.error = { code: error.code ?? 'operation_failed', message: error.message ?? '' }
      sb.last_confirmed_state = sb.observed_state
      sb.error = { code: op.error.code, resources: [] }
    } else {
      const success = this.successTarget(sb.observed_state, trigger)
      if (success === undefined) {
        throw new Error(`no transition ${sb.observed_state} --${trigger}--> in contract`)
      }
      toState = success
      op.state = 'succeeded'
      op.result = { observed_state: toState, generation: sb.generation }
    }
    sb.observed_state = toState
    if (toState === 'Active' && trigger === 'create') {
      sb.session = { id: `claude-session-${sb.sandbox_id}`, cwd: '/workspace' }
    }
    if (toState === 'Suspend') sb.has_credential = false // runtime tmpfs cleared at confirmed stop
    if (toState === 'Destroyed') sb.session = null
    if (sb.pending_operation === operationId) sb.pending_operation = null
    sb.last_confirmed_at = this.store.tick()
    sb.version += 1
    sb.reconciled = false
    op.updated_at = this.store.tick()
    this.store.putOperation(op)
    this.store.putSandbox(sb)
    return this.opView(op)
  }

  expireLease(sandboxId: string): Record<string, unknown> {
    const sb = this.sandbox(sandboxId)
    const toState = this.successTarget(sb.observed_state, 'lease_expiry')
    if (toState === undefined) {
      throw new Error(`no lease_expiry transition from ${sb.observed_state}`)
    }
    sb.observed_state = toState
    sb.reconciled = false
    sb.version += 1
    this.store.putSandbox(sb)
    return this.sandboxView(sb)
  }

  markReconciled(sandboxId: string): Record<string, unknown> {
    const sb = this.sandbox(sandboxId)
    sb.reconciled = true
    if (sb.pending_operation) {
      const op = this.store.getOperation(sb.pending_operation)
      if (op && (op.state === 'pending' || op.state === 'running')) {
        // reconciliation resolves the outstanding operation's outcome;
        // a timeout never implied not-executed, this is the verdict
        op.state = 'failed'
        op.error = { code: 'lost_reconciled', message: 'lease expired; outcome resolved by reconciliation' }
        op.updated_at = this.store.tick()
        this.store.putOperation(op)
      }
      sb.pending_operation = null
    }
    this.store.putSandbox(sb)
    return this.sandboxView(sb)
  }

  snapshot(): Snapshot {
    return this.store.snapshot()
  }

  restore(snapshot: Snapshot): void {
    this.store.restore(snapshot)
  }

  // -------------------------------------------------------------- helpers
  // public: the bearer check, and the AuthBypassMutant guard mutation point
  authorized(authorization: string | undefined): boolean {
    return authorization === `Bearer ${this.token}`
  }

  private sandbox(id: string): SandboxRecord {
    const sb = this.store.getSandbox(id)
    if (!sb) refuse(404, 'not_found', 'sandbox not found')
    return sb
  }

  private operation(id: string): OperationRecord {
    const op = this.store.getOperation(id)
    if (!op) refuse(404, 'not_found', 'operation not found')
    return op
  }

  protected validatedResources(body: Record<string, unknown>): Resources {
    const resources = body.resources
    if (!isPlainObject(resources)) refuse(422, 'invalid', 'resources object required')
    const out = { milli_cpu: 0, memory_bytes: 0, volume_bytes: 0 }
    for (const key of ['milli_cpu', 'memory_bytes', 'volume_bytes'] as const) {
      const value = resources[key]
      if (!isPositiveInt(value)) refuse(422, 'invalid', `resources.${key} must be a positive integer`)
      out[key] = value
    }
    return out
  }

  private triggerFor(observed: string, target: string): string {
    if (target === 'Suspend') return 'suspend'
    if (target === 'Idle') return 'set_idle'
    return observed === 'Idle' ? 'set_active' : 'resume'
  }

  private transitionsFor(observed: string, trigger: string): string[] | undefined {
    return this.transitions.get(`${observed}\u0000${trigger}`)
  }

  private successTarget(observed: string, trigger: string): string | undefined {
    const targets = (this.transitionsFor(observed, trigger) ?? []).filter((t) => t !== 'Error')
    return targets[0]
  }

  protected stateRefusal(
    sb: SandboxRecord,
    _target: string,
  ): { status: number; code: string; message: string } | undefined {
    const observed = sb.observed_state
    if (observed === 'Destroyed') {
      return {
        status: 409,
        code: 'opposite_operation',
        message: 'Destroyed is terminal; repeat destroy returns the existing operation',
      }
    }
    if (INTERMEDIATE_STATES.has(observed)) {
      return { status: 409, code: 'opposite_operation', message: `no state change accepted while ${observed}` }
    }
    if ((observed === 'Error' || observed === 'Lost') && !sb.reconciled) {
      return {
        status: 409,
        code: 'opposite_operation',
        message: `${observed} requires reconciliation/fencing before changes`,
      }
    }
    return undefined
  }

  protected applyCreateObservation(sb: SandboxRecord): void {
    sb.observed_state = this.initialState // Creating until Runner evidence
  }

  protected versionOk(sb: SandboxRecord, expected: number): boolean {
    return expected === sb.version
  }

  protected fingerprint(body: Record<string, unknown>): string {
    const stripped: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(body)) {
      if (k !== 'credential') stripped[k] = v
    }
    return canonicalJson(stripped)
  }

  protected idempotentLookup(key: string): IdempotencyEntry | undefined {
    return this.store.getIdempotency(key)
  }

  protected storeCredential(sb: SandboxRecord, _op: OperationRecord, credential: unknown): void {
    // secrets never enter the sandbox record, operation payload or snapshot
    sb.has_credential = Boolean(credential)
  }

  protected reservedCompute(): { milli_cpu: number; memory_bytes: number } {
    const out = { milli_cpu: 0, memory_bytes: 0 }
    for (const sb of this.store.listSandboxes()) {
      if (COMPUTE_HELD_STATES.has(sb.observed_state)) {
        out.milli_cpu += sb.resources.milli_cpu
        out.memory_bytes += sb.resources.memory_bytes
      }
    }
    return out
  }

  private capacityLeft() {
    const reserved = this.reservedCompute()
    const volume = this.store
      .listSandboxes()
      .filter((sb) => sb.observed_state !== 'Destroyed')
      .reduce((sum, sb) => sum + sb.resources.volume_bytes, 0)
    return {
      milli_cpu: this.capacity.milli_cpu - reserved.milli_cpu,
      memory_bytes: this.capacity.memory_bytes - reserved.memory_bytes,
      volume_bytes: this.capacity.volume_bytes - volume,
    }
  }

  private newOperation(
    sb: SandboxRecord,
    opType: 'create' | 'set_state' | 'destroy',
    trigger: string | null,
    target: Record<string, unknown> | null,
  ): OperationRecord {
    const op: OperationRecord = {
      operation_id: this.store.nextId('op'),
      sandbox_id: sb.sandbox_id,
      type: opType,
      trigger: trigger ?? opType,
      target: target ?? { agent: 'claude', resources: { ...sb.resources } },
      state: 'pending',
      expected_version: opType === 'create' ? null : sb.version,
      generation: sb.generation,
      result: null,
      error: null,
      created_at: this.store.tick(),
      updated_at: this.store.now(),
    }
    this.store.putOperation(op)
    sb.pending_operation = op.operation_id
    return op
  }

  private idempotencyCheck(
    method: string,
    path: string,
    key: string | undefined,
    body: Record<string, unknown>,
  ): IdempotencyEntry | undefined {
    if (!key) refuse(422, 'invalid', 'Idempotency-Key header required')
    const entry = this.idempotentLookup(key)
    if (!entry) return undefined
    if (entry.method !== method || entry.path !== path || entry.fingerprint !== this.fingerprint(body)) {
      refuse(409, 'body_conflict', 'Idempotency-Key replayed with a different body')
    }
    return entry
  }

  private remember(
    key: string | undefined,
    method: string,
    path: string,
    body: Record<string, unknown>,
    status: number,
    op: OperationRecord,
  ): void {
    if (!key) return
    this.store.putIdempotency({
      key,
      method,
      path,
      fingerprint: this.fingerprint(body),
      status,
      operation_id: op.operation_id,
      sandbox_id: op.sandbox_id,
    })
  }

  private idempotentReplay(entry: IdempotencyEntry): ApiResult {
    const op = this.operation(entry.operation_id)
    const body =
      entry.method === 'POST' && entry.path === '/v1/sandboxes'
        ? { sandbox_id: entry.sandbox_id, operation: this.opView(op) }
        : { operation: this.opView(op) }
    return { status: entry.status, body }
  }

  opView(op: OperationRecord): OperationView {
    return {
      operation_id: op.operation_id,
      sandbox_id: op.sandbox_id,
      type: op.type,
      target: structuredClone(op.target),
      state: op.state,
      expected_version: op.expected_version,
      generation: op.generation,
      result: structuredClone(op.result),
      error: structuredClone(op.error),
      retryable: op.state === 'failed',
      created_at: op.created_at,
      updated_at: op.updated_at,
    }
  }

  sandboxView(sb: SandboxRecord): Record<string, unknown> {
    const view: Record<string, unknown> = {
      sandbox_id: sb.sandbox_id,
      workspace_id: sb.workspace_id,
      desired_state: sb.desired_state,
      observed_state: sb.observed_state,
      generation: sb.generation,
      version: sb.version,
      pending_operation: sb.pending_operation ? this.opView(this.operation(sb.pending_operation)) : null,
      last_confirmed_at: sb.last_confirmed_at,
      volumes: { ...sb.volumes },
      runtime_deadline_at: sb.runtime_deadline_at,
      session: structuredClone(sb.session),
    }
    if (sb.observed_state === 'Error') {
      view.last_confirmed_state = sb.last_confirmed_state
      view.error = structuredClone(sb.error)
    }
    if (sb.observed_state === 'Active' || sb.observed_state === 'Idle') {
      view.hold = null
      view.next_transition_at = null
    }
    return view
  }
}

export type ControlPlaneApp = Hono & { cp: ControlPlane }

async function jsonBody(c: Context): Promise<Record<string, unknown>> {
  const text = await c.req.text()
  if (!text) return {}
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    refuse(422, 'invalid', 'request body must be JSON')
  }
  if (!isPlainObject(parsed)) refuse(422, 'invalid', 'request body must be a JSON object')
  return parsed
}

function respond(c: Context, result: ApiResult): Response {
  return c.json(result.body, result.status as ContentfulStatusCode)
}

// createApp(store) wires a fresh ControlPlane over the adapter; passing a
// ControlPlane directly (contract-test mutants) reuses that instance.
export function createApp(source: PersistenceAdapter | ControlPlane, opts: ControlPlaneOptions = {}): ControlPlaneApp {
  const cp = source instanceof ControlPlane ? source : new ControlPlane(source, opts)
  const app = new Hono() as ControlPlaneApp
  app.cp = cp
  app.use('*', async (c, next) => {
    if (!cp.authorized(c.req.header('Authorization'))) {
      return c.json(errBody('unauthorized', 'missing or invalid bearer token'), 401)
    }
    await next()
  })
  app.post('/v1/sandboxes', async (c) =>
    respond(c, cp.createSandbox(await jsonBody(c), c.req.header('Idempotency-Key'))))
  app.get('/v1/sandboxes', (c) => respond(c, cp.listSandboxes()))
  app.get('/v1/sandboxes/:id', (c) => respond(c, cp.inspect(c.req.param('id'))))
  app.post('/v1/sandboxes/:id/state', async (c) =>
    respond(c, cp.changeState(c.req.param('id'), await jsonBody(c), c.req.header('Idempotency-Key'))))
  app.delete('/v1/sandboxes/:id', async (c) =>
    respond(c, cp.destroy(c.req.param('id'), await jsonBody(c), c.req.header('Idempotency-Key'))))
  app.post('/v1/sandboxes/:id/terminal-ticket', (c) => respond(c, cp.issueTicket(c.req.param('id'))))
  app.get('/v1/operations/:id', (c) => respond(c, cp.pollOperation(c.req.param('id'))))
  app.notFound((c) => c.json(errBody('not_found', 'unknown endpoint'), 404))
  app.onError((error, c) => {
    if (error instanceof Refused) return c.json(error.body, error.status as ContentfulStatusCode)
    return c.json(errBody('internal', 'control-plane bug'), 500)
  })
  return app
}
