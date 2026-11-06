ALTER TABLE trips DROP CONSTRAINT trips_status_check;
ALTER TABLE trips ADD CHECK (status IN ('draft', 'payment_pending', 'paid'));
ALTER TABLE business_events DROP CONSTRAINT business_events_check;
ALTER TABLE business_events ADD CHECK (after_revision >= before_revision);
ALTER TABLE business_events DROP CONSTRAINT business_events_workspace_id_trip_id_after_revision_key;
ALTER TABLE business_events ALTER COLUMN trip_id DROP NOT NULL;
ALTER TABLE business_events ADD COLUMN object_type text NOT NULL DEFAULT 'trip';
ALTER TABLE business_events ADD COLUMN object_id uuid;
ALTER TABLE business_events ADD COLUMN executor text NOT NULL DEFAULT 'user' CHECK (executor IN ('user','worker'));
CREATE UNIQUE INDEX trip_revision_events ON business_events(workspace_id, trip_id, after_revision)
    WHERE action IN ('trip.created','trip.updated');

CREATE TABLE payment_sources (
    workspace_id text NOT NULL, ref text NOT NULL, owner_id text NOT NULL,
    kind text NOT NULL CHECK (kind='personal'), eligible boolean NOT NULL,
    PRIMARY KEY(workspace_id,ref),
    FOREIGN KEY(workspace_id,owner_id) REFERENCES users(workspace_id,id)
);
CREATE TABLE payment_profiles (
    workspace_id text NOT NULL, id uuid NOT NULL, owner_id text NOT NULL,
    source_ref text NOT NULL, revision integer NOT NULL CHECK(revision>0),
    PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,owner_id),
    FOREIGN KEY(workspace_id,owner_id) REFERENCES users(workspace_id,id),
    FOREIGN KEY(workspace_id,source_ref) REFERENCES payment_sources(workspace_id,ref)
);
CREATE TABLE flights (
    workspace_id text NOT NULL REFERENCES workspaces(id), id text NOT NULL,
    travel_date date NOT NULL, active boolean NOT NULL DEFAULT true,
    base_minor bigint NOT NULL CHECK(base_minor>=0), fee_minor bigint NOT NULL CHECK(fee_minor>=0),
    capacity integer NOT NULL CHECK(capacity>=0), reserved integer NOT NULL DEFAULT 0 CHECK(reserved>=0),
    sold integer NOT NULL DEFAULT 0 CHECK(sold>=0), CHECK(reserved+sold<=capacity),
    PRIMARY KEY(workspace_id,id,travel_date)
);
CREATE TABLE quotes (
    workspace_id text NOT NULL, id uuid NOT NULL, trip_id uuid NOT NULL,
    trip_revision integer NOT NULL, profile_id uuid NOT NULL, profile_revision integer NOT NULL,
    source_ref text NOT NULL,
    flight_id text NOT NULL, travel_date date NOT NULL, quantity integer NOT NULL CHECK(quantity>0),
    amount_minor bigint NOT NULL CHECK(amount_minor>=0), currency text NOT NULL CHECK(currency='DEMO'),
    rule_version text NOT NULL, expires_at timestamptz NOT NULL,
    status text NOT NULL CHECK(status IN ('active','invalidated','expired')),
    PRIMARY KEY(workspace_id,id),
    FOREIGN KEY(workspace_id,trip_id) REFERENCES trips(workspace_id,id),
    FOREIGN KEY(workspace_id,profile_id) REFERENCES payment_profiles(workspace_id,id),
    FOREIGN KEY(workspace_id,flight_id,travel_date) REFERENCES flights(workspace_id,id,travel_date)
);
CREATE TABLE booking_locks (
    workspace_id text NOT NULL, id uuid NOT NULL, trip_id uuid NOT NULL, quote_id uuid NOT NULL,
    trip_revision integer NOT NULL, profile_id uuid NOT NULL, profile_revision integer NOT NULL,
    source_ref text NOT NULL,
    flight_id text NOT NULL, travel_date date NOT NULL, quantity integer NOT NULL CHECK(quantity>0),
    amount_minor bigint NOT NULL CHECK(amount_minor>=0), currency text NOT NULL CHECK(currency='DEMO'),
    expires_at timestamptz NOT NULL,
    status text NOT NULL CHECK(status IN ('held','protected','consumed','cancelled','expired','invalidated','failed')),
    PRIMARY KEY(workspace_id,id),
    FOREIGN KEY(workspace_id,trip_id) REFERENCES trips(workspace_id,id),
    FOREIGN KEY(workspace_id,quote_id) REFERENCES quotes(workspace_id,id),
    FOREIGN KEY(workspace_id,profile_id) REFERENCES payment_profiles(workspace_id,id),
    FOREIGN KEY(workspace_id,flight_id,travel_date) REFERENCES flights(workspace_id,id,travel_date)
);
CREATE UNIQUE INDEX one_live_booking_lock ON booking_locks(workspace_id,trip_id)
    WHERE status IN ('held','protected','consumed');
CREATE TABLE payments (
    workspace_id text NOT NULL, id uuid NOT NULL, trip_id uuid NOT NULL, lock_id uuid NOT NULL,
    amount_minor bigint NOT NULL CHECK(amount_minor>=0), currency text NOT NULL CHECK(currency='DEMO'),
    status text NOT NULL CHECK(status IN ('processing','unknown','succeeded','failed')),
    accepted_at timestamptz NOT NULL,
    PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,lock_id),
    FOREIGN KEY(workspace_id,trip_id) REFERENCES trips(workspace_id,id),
    FOREIGN KEY(workspace_id,lock_id) REFERENCES booking_locks(workspace_id,id)
);
CREATE UNIQUE INDEX one_live_payment ON payments(workspace_id,trip_id) WHERE status<>'failed';
CREATE TABLE payment_jobs (
    workspace_id text NOT NULL, payment_id uuid NOT NULL,
    status text NOT NULL CHECK(status IN ('pending','awaiting_reconciliation','done')),
    PRIMARY KEY(workspace_id,payment_id),
    FOREIGN KEY(workspace_id,payment_id) REFERENCES payments(workspace_id,id)
);
CREATE TABLE provider_payments (
    workspace_id text NOT NULL, operation_id uuid NOT NULL,
    outcome text NOT NULL CHECK(outcome IN ('succeeded','failed')),
    amount_minor bigint NOT NULL, captured_minor bigint NOT NULL,
    CHECK ((outcome='succeeded' AND captured_minor=amount_minor AND amount_minor>=0)
        OR (outcome='failed' AND captured_minor=0)),
    PRIMARY KEY(workspace_id,operation_id),
    FOREIGN KEY(workspace_id,operation_id) REFERENCES payments(workspace_id,id)
);
CREATE TABLE ticket_tasks (
    workspace_id text NOT NULL, id uuid NOT NULL, payment_id uuid NOT NULL, trip_id uuid NOT NULL,
    status text NOT NULL CHECK(status IN ('pending','succeeded','failed')),
    PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,payment_id),
    FOREIGN KEY(workspace_id,payment_id) REFERENCES payments(workspace_id,id),
    FOREIGN KEY(workspace_id,trip_id) REFERENCES trips(workspace_id,id)
);
CREATE TABLE tickets (
    workspace_id text NOT NULL, id uuid NOT NULL, task_id uuid NOT NULL, payment_id uuid NOT NULL,
    trip_id uuid NOT NULL, trip_revision integer NOT NULL, quantity integer NOT NULL CHECK(quantity>0),
    certificate text NOT NULL,
    PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,task_id), UNIQUE(workspace_id,payment_id),
    FOREIGN KEY(workspace_id,task_id) REFERENCES ticket_tasks(workspace_id,id),
    FOREIGN KEY(workspace_id,payment_id) REFERENCES payments(workspace_id,id),
    FOREIGN KEY(workspace_id,trip_id) REFERENCES trips(workspace_id,id)
);
