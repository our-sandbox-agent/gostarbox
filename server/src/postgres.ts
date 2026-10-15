import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import type { Pool, PoolClient } from 'pg'
import type { SandboxRecord, OperationRecord, Resources, IdempotencyEntry } from './store.js'

const migrationURL = new URL('../migrations/001_control_plane.sql', import.meta.url)
const migrationID = '001_control_plane'
const digest = (value: string) => createHash('sha256').update(value).digest('hex')

export class StoreConflict extends Error {}
export interface ResourceRecord {
  resource_id: string
  sandbox_id: string
  kind: 'workspace_volume' | 'home_volume' | 'runtime'
  generation: number
  quantity: Resources
}
export type StoredIdempotency = Omit<IdempotencyEntry, 'fingerprint'> & { fingerprintHash: string }

function positive(value: number): void {
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error('positive safe integer required')
}
function identifier(value: string): void {
  if (!value || value.length > 256) throw new Error('nonempty identifier of at most 256 characters required')
}
function quantities(value: Resources): Resources {
  for (const n of [value.milli_cpu, value.memory_bytes, value.volume_bytes]) {
    if (!Number.isSafeInteger(n) || n < 0) throw new Error('nonnegative safe resource quantity required')
  }
  return { milli_cpu: value.milli_cpu, memory_bytes: value.memory_bytes, volume_bytes: value.volume_bytes }
}
// Defense in depth for flexible public JSON fields, not a free-text secret detector.
function publicJSON(value: unknown): unknown {
  if (value === undefined) return null
  if (Array.isArray(value)) return value.map(publicJSON)
  if (value !== null && typeof value === 'object') {
    const out: Record<string, unknown> = {}
    for (const [key, entry] of Object.entries(value)) {
      if (/^(credential|authorization|api[_-]?key|token|secret|password)$/i.test(key)) {
        throw new Error('credential field forbidden in durable public data')
      }
      out[key] = publicJSON(entry)
    }
    return out
  }
  return value
}

// Separate asynchronous boundary; deliberately NOT the synchronous PersistenceAdapter.
// Caller owns the pool. No production connection or migrations run on import/startup.
export class PostgresStore {
  private readonly schema: string
  constructor(private readonly pool: Pool, schema = 'sandbox_cp') {
    if (!/^[a-z][a-z0-9_]{0,62}$/.test(schema)) throw new Error('invalid database schema')
    this.schema = schema
  }

  async migrate(): Promise<void> {
    const sql = await readFile(migrationURL, 'utf8')
    const client = await this.pool.connect()
    try {
      await client.query('BEGIN')
      // Concurrent initializers serialize before schema/table creation.
      await client.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`gostarbox:migration:${this.schema}`])
      await client.query(`CREATE SCHEMA IF NOT EXISTS "${this.schema}"`)
      await this.searchPath(client)
      await client.query('CREATE TABLE IF NOT EXISTS schema_migrations (id text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
      const existing = await client.query('SELECT checksum FROM schema_migrations WHERE id=$1', [migrationID])
      if (existing.rowCount) {
        if (existing.rows[0].checksum !== digest(sql)) throw new Error('applied migration checksum mismatch')
      } else {
        await client.query(sql)
        await client.query('INSERT INTO schema_migrations(id,checksum) VALUES($1,$2)', [migrationID, digest(sql)])
      }
      await client.query('COMMIT')
    } catch (error) {
      await client.query('ROLLBACK')
      throw error
    } finally { client.release() }
  }

  private async searchPath(client: PoolClient): Promise<void> {
    await client.query("SELECT set_config('search_path', $1, true)", [this.schema])
  }

  async transaction<T>(workspaceID: string, work: (tx: PostgresTransaction) => Promise<T>): Promise<T> {
    identifier(workspaceID)
    const client = await this.pool.connect()
    const tx = new PostgresTransaction(client, workspaceID)
    try {
      await client.query('BEGIN')
      await this.searchPath(client)
      await client.query('INSERT INTO workspaces(id) VALUES($1) ON CONFLICT DO NOTHING', [workspaceID])
      // Serialize one workspace's mutations and idempotency decisions across connections.
      // Cross-workspace host capacity needs a separate admission lock in the API slice.
      await client.query('SELECT id FROM workspaces WHERE id=$1 FOR UPDATE', [workspaceID])
      const result = await work(tx)
      tx.finish()
      const commit = await client.query('COMMIT')
      if (commit.command !== 'COMMIT') throw new StoreConflict('transaction aborted before commit')
      return result
    } catch (error) {
      tx.finish()
      await client.query('ROLLBACK')
      throw error
    } finally { client.release() }
  }
}

export class PostgresTransaction {
  private active = true
  constructor(private readonly client: PoolClient, private readonly workspaceID: string) {}
  finish(): void { this.active = false }
  private async query(sql: string, params: unknown[]) {
    if (!this.active) throw new Error('transaction is no longer active')
    return this.client.query(sql, params)
  }

  async getSandbox(id: string): Promise<SandboxRecord | undefined> {
    const r = await this.query('SELECT record FROM sandboxes WHERE workspace_id=$1 AND id=$2', [this.workspaceID, id])
    return r.rows[0]?.record
  }
  async listSandboxes(): Promise<SandboxRecord[]> {
    const r = await this.query('SELECT record FROM sandboxes WHERE workspace_id=$1 ORDER BY id', [this.workspaceID])
    return r.rows.map(row => row.record)
  }
  async putSandbox(sb: SandboxRecord, expectedVersion: number | null): Promise<void> {
    identifier(sb.sandbox_id); positive(sb.version); positive(sb.generation)
    if (sb.workspace_id !== this.workspaceID) throw new Error('workspace mismatch')
    // Explicit projection: runtime credentials and unknown input fields never serialize.
    const record: SandboxRecord = {
      sandbox_id: sb.sandbox_id, workspace_id: this.workspaceID,
      desired_state: sb.desired_state, observed_state: sb.observed_state,
      version: sb.version, generation: sb.generation, resources: quantities(sb.resources),
      repo_url: sb.repo_url, runtime_deadline_at: sb.runtime_deadline_at,
      volumes: {workspace: sb.volumes.workspace, home: sb.volumes.home},
      session: sb.session ? {id: sb.session.id, cwd: sb.session.cwd} : null,
      pending_operation: sb.pending_operation, last_confirmed_at: sb.last_confirmed_at,
      error: publicJSON(sb.error) as SandboxRecord['error'],
      last_confirmed_state: sb.last_confirmed_state, reconciled: sb.reconciled,
      destroy_operation: sb.destroy_operation, has_credential: false, created_at: sb.created_at,
    }
    if (expectedVersion === null) {
      const r = await this.query('INSERT INTO sandboxes(workspace_id,id,version,generation,record) VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING',
        [this.workspaceID, sb.sandbox_id, sb.version, sb.generation, record])
      if (r.rowCount !== 1) throw new StoreConflict('sandbox already exists')
    } else {
      positive(expectedVersion)
      if (sb.version !== expectedVersion + 1) throw new StoreConflict('version must advance exactly once')
      const r = await this.query('UPDATE sandboxes SET version=$3,generation=$4,record=$5 WHERE workspace_id=$1 AND id=$2 AND version=$6 AND generation <= $4',
        [this.workspaceID, sb.sandbox_id, sb.version, sb.generation, record, expectedVersion])
      if (r.rowCount !== 1) throw new StoreConflict('stale version or generation')
    }
  }

  async getOperation(id: string): Promise<OperationRecord | undefined> {
    const r = await this.query('SELECT record FROM operations WHERE workspace_id=$1 AND id=$2', [this.workspaceID, id])
    return r.rows[0]?.record
  }
  async putOperation(op: OperationRecord): Promise<void> {
    identifier(op.operation_id); positive(op.generation)
    const record: OperationRecord = {
      operation_id: op.operation_id, sandbox_id: op.sandbox_id, type: op.type, trigger: op.trigger,
      target: publicJSON(op.target) as OperationRecord['target'], state: op.state,
      expected_version: op.expected_version, generation: op.generation,
      result: publicJSON(op.result) as OperationRecord['result'], error: publicJSON(op.error) as OperationRecord['error'],
      created_at: op.created_at, updated_at: op.updated_at,
    }
    const r = await this.query(`INSERT INTO operations(workspace_id,id,sandbox_id,generation,record) VALUES($1,$2,$3,$4,$5)
      ON CONFLICT(workspace_id,id) DO UPDATE SET record=EXCLUDED.record
      WHERE operations.sandbox_id=EXCLUDED.sandbox_id AND operations.generation=EXCLUDED.generation
      AND operations.record->>'type'=EXCLUDED.record->>'type' AND operations.record->>'trigger'=EXCLUDED.record->>'trigger'
      AND operations.record->'target'=EXCLUDED.record->'target'
      AND operations.record->'expected_version'=EXCLUDED.record->'expected_version'`,
    [this.workspaceID, op.operation_id, op.sandbox_id, op.generation, record])
    if (r.rowCount !== 1) throw new StoreConflict('operation identity mismatch')
  }

  async putResource(resource: ResourceRecord): Promise<void> {
    identifier(resource.resource_id); positive(resource.generation)
    const r = await this.query(`INSERT INTO resources(workspace_id,id,sandbox_id,kind,generation,quantity) VALUES($1,$2,$3,$4,$5,$6)
      ON CONFLICT(workspace_id,id) DO UPDATE SET quantity=EXCLUDED.quantity
      WHERE resources.sandbox_id=EXCLUDED.sandbox_id AND resources.kind=EXCLUDED.kind AND resources.generation=EXCLUDED.generation`,
    [this.workspaceID, resource.resource_id, resource.sandbox_id, resource.kind, resource.generation, quantities(resource.quantity)])
    if (r.rowCount !== 1) throw new StoreConflict('resource identity mismatch')
  }
  async listResources(sandboxID: string): Promise<ResourceRecord[]> {
    const r = await this.query('SELECT id AS resource_id,sandbox_id,kind,generation::float8 AS generation,quantity FROM resources WHERE workspace_id=$1 AND sandbox_id=$2 ORDER BY id', [this.workspaceID, sandboxID])
    return r.rows
  }

  async putIdempotency(entry: IdempotencyEntry): Promise<void> {
    identifier(entry.key)
    // Persist only the hash, never the canonical request text (which may include secrets).
    const r = await this.query(`INSERT INTO idempotency_keys(workspace_id,key,method,path,fingerprint_hash,status,sandbox_id,operation_id,response)
      VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING`,
    [this.workspaceID, entry.key, entry.method, entry.path, digest(entry.fingerprint), entry.status,
      entry.sandbox_id, entry.operation_id, entry.status === 200 ? publicJSON(entry.response) : null])
    if (r.rowCount !== 1) throw new StoreConflict('idempotency key already reserved')
  }
  async getIdempotency(key: string): Promise<StoredIdempotency | undefined> {
    const r = await this.query(`SELECT key,method,path,fingerprint_hash AS "fingerprintHash",status,sandbox_id,operation_id,response
      FROM idempotency_keys WHERE workspace_id=$1 AND key=$2`, [this.workspaceID, key])
    return r.rows[0]
  }
}
