CREATE TABLE project_meta (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    project text NOT NULL CHECK (project = 'llm-business-d1'),
    purpose text NOT NULL CHECK (purpose IN ('demo', 'test'))
);

CREATE TABLE workspaces (
    id text PRIMARY KEY,
    business_time timestamptz NOT NULL,
    product_version text NOT NULL
);

CREATE TABLE users (
    workspace_id text NOT NULL REFERENCES workspaces(id),
    id text NOT NULL,
    display_name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (workspace_id, id)
);

CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    workspace_id text NOT NULL,
    user_id text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    FOREIGN KEY (workspace_id, user_id) REFERENCES users(workspace_id, id)
);

CREATE TABLE trips (
    workspace_id text NOT NULL,
    id uuid NOT NULL,
    owner_id text NOT NULL,
    organization_id text,
    flight_id text NOT NULL CHECK (length(btrim(flight_id)) BETWEEN 1 AND 80),
    travel_date date NOT NULL,
    passengers integer NOT NULL CHECK (passengers BETWEEN 1 AND 9),
    revision integer NOT NULL CHECK (revision >= 1),
    status text NOT NULL CHECK (status = 'draft'),
    price_minor bigint CHECK (price_minor >= 0),
    PRIMARY KEY (workspace_id, id),
    FOREIGN KEY (workspace_id, owner_id) REFERENCES users(workspace_id, id)
);

CREATE TABLE business_events (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id text NOT NULL,
    actor_id text NOT NULL,
    trip_id uuid NOT NULL,
    action text NOT NULL,
    before_revision integer NOT NULL CHECK (before_revision >= 0),
    after_revision integer NOT NULL CHECK (after_revision > before_revision),
    business_time timestamptz NOT NULL,
    result text NOT NULL CHECK (result = 'succeeded'),
    FOREIGN KEY (workspace_id, actor_id) REFERENCES users(workspace_id, id),
    FOREIGN KEY (workspace_id, trip_id) REFERENCES trips(workspace_id, id),
    UNIQUE (workspace_id, trip_id, after_revision)
);

CREATE TABLE idempotency (
    workspace_id text NOT NULL,
    actor_id text NOT NULL,
    action text NOT NULL,
    key text NOT NULL CHECK (length(key) BETWEEN 1 AND 128),
    parameters jsonb NOT NULL,
    response jsonb NOT NULL,
    PRIMARY KEY (workspace_id, actor_id, action, key),
    FOREIGN KEY (workspace_id, actor_id) REFERENCES users(workspace_id, id)
);
