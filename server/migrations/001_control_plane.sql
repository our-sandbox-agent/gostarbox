CREATE TABLE workspaces (
  id text PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE sandboxes (
  workspace_id text NOT NULL REFERENCES workspaces(id),
  id text NOT NULL,
  version bigint NOT NULL CHECK (version > 0),
  generation bigint NOT NULL CHECK (generation > 0),
  record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
  PRIMARY KEY (workspace_id, id)
);
CREATE TABLE operations (
  workspace_id text NOT NULL,
  id text NOT NULL,
  sandbox_id text NOT NULL,
  generation bigint NOT NULL CHECK (generation > 0),
  record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
  PRIMARY KEY (workspace_id, id),
  UNIQUE (workspace_id, id, sandbox_id),
  FOREIGN KEY (workspace_id, sandbox_id) REFERENCES sandboxes(workspace_id, id)
);
CREATE TABLE resources (
  workspace_id text NOT NULL,
  id text NOT NULL,
  sandbox_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('workspace_volume', 'home_volume', 'runtime')),
  generation bigint NOT NULL CHECK (generation > 0),
  quantity jsonb NOT NULL CHECK (jsonb_typeof(quantity) = 'object'),
  PRIMARY KEY (workspace_id, id),
  FOREIGN KEY (workspace_id, sandbox_id) REFERENCES sandboxes(workspace_id, id)
);
CREATE TABLE idempotency_keys (
  workspace_id text NOT NULL,
  key text NOT NULL,
  method text NOT NULL,
  path text NOT NULL,
  fingerprint_hash text NOT NULL CHECK (length(fingerprint_hash) = 64),
  status integer NOT NULL CHECK (status IN (200, 202)),
  sandbox_id text NOT NULL,
  operation_id text,
  response jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, key),
  FOREIGN KEY (workspace_id, sandbox_id) REFERENCES sandboxes(workspace_id, id),
  FOREIGN KEY (workspace_id, operation_id, sandbox_id)
    REFERENCES operations(workspace_id, id, sandbox_id),
  CHECK ((status = 202 AND operation_id IS NOT NULL AND response IS NULL)
      OR (status = 200 AND operation_id IS NULL AND response IS NOT NULL))
);
CREATE INDEX operations_by_sandbox ON operations(workspace_id, sandbox_id);
CREATE INDEX resources_by_sandbox ON resources(workspace_id, sandbox_id);
