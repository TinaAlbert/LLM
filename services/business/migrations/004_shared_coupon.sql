-- C03与single-trip工作空间隔离；保留所有既有记录及金额。
ALTER TABLE workspaces ADD COLUMN fixture_key text NOT NULL DEFAULT 'single-trip'
    CHECK(fixture_key IN ('single-trip','two-trips-one-coupon'));

CREATE TABLE coupons (
    workspace_id text NOT NULL, id uuid NOT NULL, owner_id text NOT NULL,
    percent integer NOT NULL CHECK(percent BETWEEN 1 AND 100),
    cap_minor bigint NOT NULL CHECK(cap_minor>0),
    currency text NOT NULL DEFAULT 'DEMO' CHECK(currency='DEMO'),
    status text NOT NULL DEFAULT 'available' CHECK(status IN ('available','held','protected','consumed')),
    lock_id uuid,
    PRIMARY KEY(workspace_id,id), UNIQUE(workspace_id,owner_id), UNIQUE(workspace_id,lock_id),
    FOREIGN KEY(workspace_id,owner_id) REFERENCES users(workspace_id,id),
    FOREIGN KEY(workspace_id,lock_id) REFERENCES booking_locks(workspace_id,id),
    CHECK((status='available') = (lock_id IS NULL))
);
ALTER TABLE quotes ADD COLUMN base_amount_minor bigint;
ALTER TABLE quotes ADD COLUMN coupon_discount_minor bigint NOT NULL DEFAULT 0 CHECK(coupon_discount_minor>=0);
UPDATE quotes SET base_amount_minor=amount_minor;
ALTER TABLE quotes ALTER COLUMN base_amount_minor SET NOT NULL;
ALTER TABLE quotes ADD CHECK(base_amount_minor>=0 AND amount_minor=base_amount_minor-coupon_discount_minor);

ALTER TABLE booking_locks ADD COLUMN base_amount_minor bigint;
ALTER TABLE booking_locks ADD COLUMN coupon_discount_minor bigint NOT NULL DEFAULT 0 CHECK(coupon_discount_minor>=0);
ALTER TABLE booking_locks ADD COLUMN coupon_id uuid;
ALTER TABLE booking_locks ADD COLUMN coupon_percent integer;
ALTER TABLE booking_locks ADD COLUMN coupon_cap_minor bigint;
UPDATE booking_locks SET base_amount_minor=amount_minor;
ALTER TABLE booking_locks ALTER COLUMN base_amount_minor SET NOT NULL;
ALTER TABLE booking_locks ADD FOREIGN KEY(workspace_id,coupon_id) REFERENCES coupons(workspace_id,id);
ALTER TABLE booking_locks ADD CHECK(base_amount_minor>=0 AND amount_minor=base_amount_minor-coupon_discount_minor);
ALTER TABLE booking_locks ADD CHECK(
    (coupon_id IS NULL AND coupon_discount_minor=0 AND coupon_percent IS NULL AND coupon_cap_minor IS NULL)
    OR (coupon_id IS NOT NULL AND payment_kind='personal' AND coupon_percent IS NOT NULL
        AND coupon_percent BETWEEN 1 AND 100 AND coupon_cap_minor IS NOT NULL AND coupon_cap_minor>0));
CREATE UNIQUE INDEX one_live_coupon_lock ON booking_locks(workspace_id,coupon_id)
    WHERE coupon_id IS NOT NULL AND status IN ('held','protected','consumed');
