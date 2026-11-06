-- 企业 V1：服务端成员/指派、预算与审批；保留个人记录及原迁移。
CREATE TABLE organizations (
    workspace_id text NOT NULL REFERENCES workspaces(id), id text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY(workspace_id,id)
);
CREATE TABLE departments (
    workspace_id text NOT NULL, organization_id text NOT NULL, id text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY(workspace_id,organization_id,id),
    FOREIGN KEY(workspace_id,organization_id) REFERENCES organizations(workspace_id,id)
);
CREATE TABLE memberships (
    workspace_id text NOT NULL, user_id text NOT NULL,
    organization_id text NOT NULL, department_id text NOT NULL,
    employee boolean NOT NULL DEFAULT false, approver boolean NOT NULL DEFAULT false,
    active boolean NOT NULL DEFAULT true, revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
    PRIMARY KEY(workspace_id,user_id),
    FOREIGN KEY(workspace_id,user_id) REFERENCES users(workspace_id,id),
    FOREIGN KEY(workspace_id,organization_id,department_id)
        REFERENCES departments(workspace_id,organization_id,id)
);
CREATE TABLE approval_assignments (
    workspace_id text NOT NULL, applicant_id text NOT NULL, approver_id text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY(workspace_id,applicant_id),
    FOREIGN KEY(workspace_id,applicant_id) REFERENCES memberships(workspace_id,user_id),
    FOREIGN KEY(workspace_id,approver_id) REFERENCES memberships(workspace_id,user_id)
);
CREATE TABLE budget_projects (
    workspace_id text NOT NULL, id text NOT NULL,
    organization_id text NOT NULL, department_id text NOT NULL,
    total_minor bigint NOT NULL CHECK(total_minor>=0),
    reserved_minor bigint NOT NULL DEFAULT 0 CHECK(reserved_minor>=0),
    used_minor bigint NOT NULL DEFAULT 0 CHECK(used_minor>=0),
    policy_revision integer NOT NULL DEFAULT 1 CHECK(policy_revision>0),
    active boolean NOT NULL DEFAULT true,
    currency text NOT NULL DEFAULT 'DEMO' CHECK(currency='DEMO'),
    PRIMARY KEY(workspace_id,id), CHECK(reserved_minor+used_minor<=total_minor),
    FOREIGN KEY(workspace_id,organization_id,department_id)
        REFERENCES departments(workspace_id,organization_id,id)
);

ALTER TABLE trips ADD COLUMN department_id text;
ALTER TABLE trips ADD CHECK ((organization_id IS NULL) = (department_id IS NULL));
ALTER TABLE trips ADD FOREIGN KEY(workspace_id,organization_id)
    REFERENCES organizations(workspace_id,id);
ALTER TABLE trips ADD FOREIGN KEY(workspace_id,organization_id,department_id)
    REFERENCES departments(workspace_id,organization_id,id);

ALTER TABLE payment_sources DROP CONSTRAINT payment_sources_kind_check;
ALTER TABLE payment_sources ADD CHECK(kind IN ('personal','corporate'));
ALTER TABLE payment_sources ADD COLUMN organization_id text;
ALTER TABLE payment_sources ADD FOREIGN KEY(workspace_id,organization_id)
    REFERENCES organizations(workspace_id,id);
ALTER TABLE payment_sources ADD CHECK(
    (kind='personal' AND organization_id IS NULL)
    OR (kind='corporate' AND organization_id IS NOT NULL)
);
ALTER TABLE quotes ADD COLUMN payment_kind text NOT NULL DEFAULT 'personal'
    CHECK(payment_kind IN ('personal','corporate'));
ALTER TABLE booking_locks ADD COLUMN payment_kind text NOT NULL DEFAULT 'personal'
    CHECK(payment_kind IN ('personal','corporate'));

CREATE TABLE trip_budget_selections (
    workspace_id text NOT NULL, trip_id uuid NOT NULL, project_id text NOT NULL,
    revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
    PRIMARY KEY(workspace_id,trip_id),
    FOREIGN KEY(workspace_id,trip_id) REFERENCES trips(workspace_id,id),
    FOREIGN KEY(workspace_id,project_id) REFERENCES budget_projects(workspace_id,id)
);
CREATE TABLE approval_requests (
    workspace_id text NOT NULL, id uuid NOT NULL, trip_id uuid NOT NULL,
    applicant_id text NOT NULL, approver_id text NOT NULL,
    organization_id text NOT NULL, department_id text NOT NULL,
    trip_revision integer NOT NULL CHECK(trip_revision>0), lock_id uuid NOT NULL,
    project_id text NOT NULL, selection_revision integer NOT NULL CHECK(selection_revision>0),
    policy_revision integer NOT NULL CHECK(policy_revision>0),
    budget_total_minor bigint NOT NULL CHECK(budget_total_minor>=0),
    amount_minor bigint NOT NULL CHECK(amount_minor>=0),
    currency text NOT NULL CHECK(currency='DEMO'),
    revision integer NOT NULL DEFAULT 1 CHECK(revision>0),
    status text NOT NULL CHECK(status IN ('pending','approved','rejected','invalidated','cancelled','expired','failed')),
    resource_status text NOT NULL CHECK(resource_status IN ('held','protected','consumed','released')),
    PRIMARY KEY(workspace_id,id),
    FOREIGN KEY(workspace_id,trip_id) REFERENCES trips(workspace_id,id),
    FOREIGN KEY(workspace_id,applicant_id) REFERENCES memberships(workspace_id,user_id),
    FOREIGN KEY(workspace_id,approver_id) REFERENCES memberships(workspace_id,user_id),
    FOREIGN KEY(workspace_id,organization_id,department_id)
        REFERENCES departments(workspace_id,organization_id,id),
    FOREIGN KEY(workspace_id,lock_id) REFERENCES booking_locks(workspace_id,id),
    FOREIGN KEY(workspace_id,project_id) REFERENCES budget_projects(workspace_id,id)
);
CREATE UNIQUE INDEX one_live_trip_approval ON approval_requests(workspace_id,trip_id)
    WHERE resource_status IN ('held','protected','consumed');
CREATE UNIQUE INDEX one_live_lock_approval ON approval_requests(workspace_id,lock_id)
    WHERE resource_status IN ('held','protected','consumed');
ALTER TABLE payments ADD COLUMN approval_id uuid;
ALTER TABLE payments ADD FOREIGN KEY(workspace_id,approval_id)
    REFERENCES approval_requests(workspace_id,id);
