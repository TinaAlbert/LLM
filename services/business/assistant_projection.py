"""企业及明确双对象咨询投影：沿用业务授权，私有ID与配置只留宿主。"""
from fastapi import HTTPException
from pydantic import Field
from typing import Literal
from datetime import datetime, timedelta, timezone

from .app import authenticate, fail, owned_trip, token_digest, token_from
from .corporate import (approval_view, authorize_approval, assigned_approver,
                        membership, project_for, validate_approval_snapshot)
from .guide_projection import Strict, Data, Price, bounded_snapshot
from .observation import pick
from .personal import view
from .reads import latest
from services.guide.knowledge import fingerprint
from services.guide.knowledge import published_text
from pydantic import field_validator
from pydantic import model_validator

DOCUMENT = {
    'source_kind': 'documented', 'source': 'product-published:travel-lab/v1/corporate-procedure',
    'revision': 2,
    'text': '企业预订在有效报价后选择预算项目，锁价后提交部门审批。员工不能批准自己的申请，'
            '仅当前指派且同部门的审批人能在原审批页面决定。批准绑定行程修订和锁价；修改后需重新核对。'
            '员工在所选行程详情（预订与票据）的“预算与审批”中提交申请；“审批工作台”仅供当前指派审批人处理待办，不能在那里替员工提交申请。'
            '付款处理中或结果未知时查询原付款，付款成功后仍需等有效票据。',
}

SHARED_DOCUMENT = {
    'source_kind': 'documented', 'source': 'product-published:travel-lab/v1/shared-coupon',
    'revision': 1,
    'text': '本人两个明确委托行程可能共用一张个人券。预报价中的优惠只是估算，不占券；'
            '第一个合格锁价原子确定券归属和最终应付额，另一个锁价需重新核对金额。'
            '券percent表示减免百分比，cap是最高减免额。锁价占用券，付款处理中或结果未知时保护原占用，'
            '付款成功消耗券；未支付且允许取消的锁价释放后才能重新竞争，仍须重新核对库存和期限。'
            '已消耗券不能通过取消恢复，付款成功仍不等于取得有效票据。',
}

# 公开规则的表达澄清，业务规则与旧投影不变；由assistant-7选择使用。
SHARED_DOCUMENT_V2 = {
    **SHARED_DOCUMENT,
    'revision': 2,
    'text': SHARED_DOCUMENT['text'] +
            '同一张券不可拆分给两单，最高减免额不是余额；首个合格锁价确定整张券归属。'
            '另一单须重新核价，不能直接相加两份各自含券优惠的预报价；重新操作仍须核对资格。',
}



class SharedPrice(Price):
    base_amount_minor: int = Field(ge=0, le=9223372036854775807)
    coupon_discount_minor: int = Field(ge=0, le=9223372036854775807)
    coupon_allocation: Literal['estimate', 'not_applied', 'held', 'protected', 'consumed', 'released']

    @model_validator(mode='after')
    def consistent_discount(self):
        if self.amount_minor != self.base_amount_minor - self.coupon_discount_minor:
            raise ValueError('shared_price_inconsistent')
        if self.ref == 'quote' and self.coupon_allocation not in {'estimate', 'not_applied'}:
            raise ValueError('shared_allocation_inconsistent')
        if self.ref == 'lock' and self.coupon_allocation == 'estimate':
            raise ValueError('shared_allocation_inconsistent')
        return self


class SharedData(Data):
    quote: SharedPrice | None
    lock: SharedPrice | None


class SharedCoupon(Strict):
    ref: Literal['shared_coupon']
    status: Literal['absent', 'available', 'held', 'protected', 'consumed']
    percent: int | None = Field(ge=1, le=100)
    cap_minor: int | None = Field(ge=1, le=9223372036854775807)
    currency: Literal['DEMO']
    attribution: Literal['none', 'delegated_trip', 'unknown']
    trip_ref: Literal['trip_1', 'trip_2'] | None

    @model_validator(mode='after')
    def consistent_attribution(self):
        if (self.attribution == 'delegated_trip') != (self.trip_ref is not None):
            raise ValueError('shared_attribution_inconsistent')
        if self.status in {'absent', 'available'} and self.attribution != 'none':
            raise ValueError('shared_attribution_inconsistent')
        if ((self.status == 'absent' and (self.percent is not None or self.cap_minor is not None)) or
                (self.status != 'absent' and (self.percent is None or self.cap_minor is None))):
            raise ValueError('shared_coupon_inconsistent')
        return self


def capture_shared(settings, request, who, trip_id, delegated_trip_ids, deadline):
    """仅读取本次显式委托的两个本人对象；不搜索其他行程或替模型计算顺序答案。"""
    identifiers = tuple(str(value) for value in delegated_trip_ids)
    if len(identifiers) != 2 or len(set(identifiers)) != 2 or str(trip_id) not in identifiers:
        fail(409, 'delegated_scope_invalid')
    ws = settings.workspace_id
    with bounded_snapshot(settings.database_url, deadline) as conn:
        actor = authenticate(conn, request, ws)
        if (actor['id'] != who.actor or token_digest(token_from(request)) != who.digest or
                tuple(actor.get('roles', ())) != who.roles or
                actor.get('membership_revision', 0) != who.membership_revision):
            fail(409, 'session_changed')
        # 不存在/他人对象采用同一授权错误；先授权全部对象再读取关联资源。
        trips = [owned_trip(conn, ws, actor['id'], identifier) for identifier in identifiers]
        workspace = conn.execute('SELECT business_time,fixture_key FROM workspaces WHERE id=%s', (ws,)).fetchone()
        if (actor.get('organization_id') is not None or actor.get('roles') or
                any(trip['organization_id'] is not None for trip in trips) or
                workspace['fixture_key'] != 'two-trips-one-coupon'):
            fail(409, 'guide_scope_unsupported')
        private, visible, lock_refs = {}, [], {}
        for index, trip in enumerate(trips, 1):
            ref = f'trip_{index}'
            rows, data = {'trip': trip}, {}
            chosen = pick('trip', trip)
            chosen.pop('id')
            data['trip'] = {'ref': 'trip', **chosen}
            for kind, action in (('quote', 'quote.created'), ('lock', 'lock.created'),
                                 ('payment', 'payment.accepted'), ('ticket_task', 'ticket_task.created'),
                                 ('ticket', 'ticket.issued')):
                row = latest(conn, ws, trip['id'], kind, action)
                rows[kind] = row
                data[kind] = None
                if row is not None:
                    result = view(conn, ws, kind, row)
                    chosen = pick(kind, result)
                    chosen.pop('id')
                    if kind in {'quote', 'lock'}:
                        allocation = 'not_applied'
                        if kind == 'quote' and row['coupon_discount_minor']:
                            allocation = 'estimate'
                        elif kind == 'lock' and row['coupon_id'] is not None:
                            allocation = row['status'] if row['status'] in {'held', 'protected', 'consumed'} else 'released'
                        chosen.update(base_amount_minor=result['base_amount_minor'],
                                      coupon_discount_minor=result['coupon_discount_minor'],
                                      coupon_allocation=allocation)
                    data[kind] = {'ref': kind, **chosen}
                    if kind == 'lock':
                        lock_refs[str(row['id'])] = ref
            data = SharedData.model_validate(data).model_dump(mode='json')
            current = data['lock'] if data['lock'] and data['lock']['valid'] else data['quote']
            visible.append({'ref': ref, 'data': data,
                            'unknowns': [] if current and current['valid'] else ['current_quote_amount']})
            rows['flight'] = conn.execute('SELECT * FROM flights WHERE workspace_id=%s AND id=%s AND travel_date=%s',
                                          (ws, trip['flight_id'], trip['travel_date'])).fetchone()
            private[ref] = rows
        coupon = conn.execute('SELECT * FROM coupons WHERE workspace_id=%s AND owner_id=%s',
                              (ws, actor['id'])).fetchone()
        coupon_ref = lock_refs.get(str(coupon['lock_id'])) if coupon else None
        shared = SharedCoupon.model_validate({
            'ref': 'shared_coupon', 'status': coupon['status'] if coupon else 'absent',
            'percent': coupon['percent'] if coupon else None, 'cap_minor': coupon['cap_minor'] if coupon else None,
            'currency': 'DEMO', 'trip_ref': coupon_ref,
            'attribution': 'none' if not coupon or coupon['status'] == 'available' else (
                'delegated_trip' if coupon_ref else 'unknown'),
        }).model_dump(mode='json')
        # 来源归属/库存/资格/价格配置等即使没有revision变化，也必须让晚到结果失效。
        profile = conn.execute('''SELECT p.*,s.kind,s.eligible,s.organization_id,s.owner_id AS source_owner_id
            FROM payment_profiles p LEFT JOIN payment_sources s
            ON (s.workspace_id,s.ref)=(p.workspace_id,p.source_ref)
            WHERE p.workspace_id=%s AND p.owner_id=%s''', (ws, actor['id'])).fetchone()
        public = {
            'contract_version': 'assistant-shared-1', 'product_id': 'travel-lab', 'product_version': 'v1',
            'rule_version': 'booking-v1', 'policy_version': 'subject-assignment-v1',
            'feature_config_version': 'shared-coupon-1', 'source_kind': 'observed',
            'environment': 'local-demo', 'simulated_identity': 'synthetic_personal',
            'disclosure_scope': 'current-session', 'selected_trip': True,
            'selected_ref': f'trip_{identifiers.index(str(trip_id)) + 1}',
            'delegation': 'two_explicit_owned_trips', 'delegated_trips': visible, 'shared_coupon': shared,
            'state_versions': {'trips': {row['ref']: row['data']['trip']['revision'] for row in visible},
                               'payment_profile': profile['revision'] if profile else 0},
            'business_time': workspace['business_time'].isoformat(),
            'assumptions': ['synthetic_demo_account', 'simulated_business_services'],
            'unknowns': ['unexecuted_order_outcomes'], 'documented': SHARED_DOCUMENT,
        }
        import json
        digest = fingerprint(json.loads(json.dumps({'public': public, 'actor': actor, 'workspace': ws,
            'session': who.digest, 'private': private, 'profile': profile, 'coupon': coupon}, default=str)))
        observed = datetime.now(timezone.utc)
        return {**public, 'observed_at': observed.isoformat(),
                'valid_until': (observed + timedelta(seconds=30)).isoformat()}, digest


class Approval(Strict):
    revision: int = Field(ge=1)
    trip_revision: int = Field(ge=1)
    effective_status: Literal['pending', 'approved', 'rejected', 'cancelled', 'expired', 'invalidated', 'failed']
    resource_status: Literal['held', 'protected', 'consumed', 'released']
    valid: bool
    applicability: Literal['not_approved', 'current', 'stale', 'unavailable', 'payment_in_progress', 'used_for_payment']


class BudgetSelection(Strict):
    status: Literal['unselected', 'selected', 'unavailable']
    revision: int = Field(ge=0)


class Assigned(Strict):
    ref: str = Field(pattern=r'^assigned_[1-5]$')
    revision: int = Field(ge=1)
    trip_revision: int = Field(ge=1)
    current_trip_revision: int = Field(ge=1)
    flight_id: str
    travel_date: str
    passengers: int = Field(ge=1, le=9)
    amount_minor: int = Field(ge=0, le=9223372036854775807)
    currency: Literal['DEMO']
    effective_status: Literal['pending', 'expired']
    valid: bool

    @field_validator('flight_id')
    @classmethod
    def safe_label(cls, value):
        return published_text(value)


def _handoff(conn, ws, member):
    try:
        assigned_approver(conn, ws, member)
        return 'assigned_department_approver'
    except HTTPException as error:
        if error.status_code != 404:
            raise
        return 'unavailable'


def _employee(conn, ws, actor, trip_id, now):
    trip = owned_trip(conn, ws, actor['id'], trip_id)
    member = membership(conn, ws, actor['id'], required=True, employee=True)
    if trip['organization_id'] is None:
        fail(409, 'guide_scope_unsupported')
    data, private = {}, {'trip': trip}
    def project(kind, row):
        if row is None:
            return None
        private[kind] = row
        selected = pick(kind, row)
        selected.pop('id')
        return {'ref': kind, **selected}
    data['trip'] = project('trip', trip)
    for kind, action in (('quote', 'quote.created'), ('lock', 'lock.created'),
                         ('payment', 'payment.accepted'), ('ticket_task', 'ticket_task.created'),
                         ('ticket', 'ticket.issued')):
        row = latest(conn, ws, trip['id'], kind, action)
        private[kind] = row
        data[kind] = project(kind, view(conn, ws, kind, row)) if row else None
    data = Data.model_validate(data).model_dump(mode='json')
    selection = conn.execute('SELECT * FROM trip_budget_selections WHERE workspace_id=%s AND trip_id=%s',
                             (ws, trip['id'])).fetchone()
    private['selection'] = selection
    budget = {'status': 'unselected', 'revision': 0}
    if selection:
        budget = {'status': 'selected', 'revision': selection['revision']}
        try:
            private['project'] = project_for(conn, ws, member, selection['project_id'])
        except HTTPException as error:
            if error.status_code != 404:
                raise
            budget['status'] = 'unavailable'
    approval = latest(conn, ws, trip['id'], 'approval', 'approval.submitted')
    private['approval_row'] = approval
    summary = None
    if approval:
        authorize_approval(conn, ws, actor['id'], approval)
        result = approval_view(conn, ws, approval)
        applicability = 'not_approved'
        if result['effective_status'] in {'expired', 'invalidated', 'cancelled'} or approval['trip_revision'] != trip['revision']:
            applicability = 'stale'
        elif approval['status'] == 'approved':
            if approval['resource_status'] == 'consumed':
                applicability = 'used_for_payment'
            elif approval['resource_status'] == 'protected':
                applicability = 'payment_in_progress'
            else:
                booking = conn.execute('SELECT * FROM booking_locks WHERE workspace_id=%s AND id=%s',
                                        (ws, approval['lock_id'])).fetchone()
                try:
                    # 复用原资格/指派/选择/锁价验证，只取有限事务锁，不执行写入；模型等待前回滚释放。
                    if booking is None:
                        fail(409, 'state_changed')
                    validate_approval_snapshot(conn, ws, trip, booking, approval, now)
                    applicability = 'current'
                except HTTPException as error:
                    if error.status_code not in {404, 409}:
                        raise
                    applicability = 'unavailable'
        summary = Approval.model_validate({
            **{key: result[key] for key in ('revision', 'trip_revision', 'effective_status', 'valid')},
            'resource_status': approval['resource_status'], 'applicability': applicability,
        }).model_dump(mode='json')
    handoff = _handoff(conn, ws, member)
    current = data['lock'] if data['lock'] and data['lock']['valid'] else data['quote']
    return {'data': {**data, 'approval': summary,
                     'budget_selection': BudgetSelection.model_validate(budget).model_dump(mode='json')},
            'approval_handoff': handoff,
            'unknowns': ([] if current and current['valid'] else ['current_quote_amount']) + ['approver_name'],
            'state_versions': {'membership': actor['membership_revision'], 'trip': trip['revision']}}, private


def _assigned(conn, ws, actor):
    membership(conn, ws, actor['id'], required=True, approver=True)
    # 先按当前指派限定候选，再逐条使用原授权；匿名序号不是业务对象标识。
    rows = conn.execute('''SELECT a.* FROM approval_requests a JOIN approval_assignments x
        ON (x.workspace_id,x.applicant_id,x.approver_id)=(a.workspace_id,a.applicant_id,a.approver_id)
        JOIN memberships m ON (m.workspace_id,m.user_id)=(a.workspace_id,a.applicant_id)
        JOIN users u ON (u.workspace_id,u.id)=(a.workspace_id,a.applicant_id)
        JOIN trips t ON (t.workspace_id,t.id)=(a.workspace_id,a.trip_id)
        WHERE a.workspace_id=%s AND a.approver_id=%s AND a.applicant_id<>%s
        AND a.status='pending' AND x.active AND m.active AND m.employee AND u.active
        AND (m.organization_id,m.department_id)=(a.organization_id,a.department_id)
        AND (t.organization_id,t.department_id,t.owner_id)=(a.organization_id,a.department_id,a.applicant_id)
        AND (a.organization_id,a.department_id)=(%s,%s)
        ORDER BY a.id LIMIT 6''', (ws, actor['id'], actor['id'], actor['organization_id'], actor['department_id'])).fetchall()
    visible, private = [], []
    for row in rows:
        try:
            authorize_approval(conn, ws, actor['id'], row)
        except HTTPException as error:
            if error.status_code != 404:
                raise
            continue
        private.append(row)
        if len(visible) == 5:
            continue
        result = approval_view(conn, ws, row)
        trip = conn.execute('SELECT revision FROM trips WHERE workspace_id=%s AND id=%s',
                            (ws, row['trip_id'])).fetchone()
        visible.append(Assigned.model_validate({
            'ref': f'assigned_{len(visible) + 1}', 'current_trip_revision': trip['revision'],
            **{key: result[key] for key in ('revision', 'trip_revision', 'flight_id', 'travel_date',
                                           'passengers', 'amount_minor', 'currency', 'effective_status', 'valid')},
        }).model_dump(mode='json'))
    return {'assigned_approvals': visible, 'assigned_list_truncated': len(private) > 5,
            'unknowns': ['applicant_payment_and_ticket_state'],
            'state_versions': {'membership': actor['membership_revision']}}, private


def capture(settings, request, who, trip_id, deadline):
    ws = settings.workspace_id
    with bounded_snapshot(settings.database_url, deadline) as conn:
        actor = authenticate(conn, request, ws)
        if actor['id'] != who.actor or token_digest(token_from(request)) != who.digest:
            fail(409, 'session_changed')
        if tuple(actor['roles']) != who.roles or actor['membership_revision'] != who.membership_revision:
            fail(409, 'session_changed')
        workspace = conn.execute('SELECT business_time,fixture_key FROM workspaces WHERE id=%s', (ws,)).fetchone()
        if workspace['fixture_key'] != 'single-trip':
            fail(409, 'guide_scope_unsupported')
        if trip_id is not None:
            fields, private = _employee(conn, ws, actor, trip_id, workspace['business_time'])
        elif 'approver' in actor['roles']:
            fields, private = _assigned(conn, ws, actor)
        else:
            fields, private = {'unknowns': ['current_trip_state', 'current_quote_amount']}, None
        public = {'contract_version': 'assistant-corporate-1', 'product_version': 'v1',
                  'source_kind': 'observed', 'environment': 'local-demo',
                  'simulated_identity': 'synthetic_corporate', 'selected_trip': trip_id is not None,
                  'current_roles': actor['roles'], 'business_time': workspace['business_time'].isoformat(),
                  'assumptions': ['synthetic_demo_account', 'simulated_business_services'],
                  'documented': DOCUMENT, **fields}
        # 无修订号的撤权/资源配置也使结果失效；仅摘要留宿主，不发送这些私有行。
        configuration = {}
        if trip_id is not None:
            configuration['profile'] = conn.execute('''SELECT p.*,s.kind,s.eligible,s.organization_id,s.owner_id AS source_owner_id
                FROM payment_profiles p LEFT JOIN payment_sources s
                ON (s.workspace_id,s.ref)=(p.workspace_id,p.source_ref)
                WHERE p.workspace_id=%s AND p.owner_id=%s''', (ws, actor['id'])).fetchone()
            assignment = conn.execute('SELECT * FROM approval_assignments WHERE workspace_id=%s AND applicant_id=%s',
                                      (ws, actor['id'])).fetchone()
            configuration['assignment'] = assignment
            if assignment:
                configuration['assignee'] = conn.execute('''SELECT m.*,u.active AS user_active
                    FROM memberships m JOIN users u ON (u.workspace_id,u.id)=(m.workspace_id,m.user_id)
                    WHERE m.workspace_id=%s AND m.user_id=%s''', (ws, assignment['approver_id'])).fetchone()
            current_trip = private['trip']
            configuration['flight'] = conn.execute('SELECT * FROM flights WHERE workspace_id=%s AND id=%s AND travel_date=%s',
                        (ws, current_trip['flight_id'], current_trip['travel_date'])).fetchone()
        # default=str仅用于私有摘要；public只含上面的严格叶字段。
        import json
        digest = fingerprint(json.loads(json.dumps({'public': public, 'actor': actor, 'workspace': ws,
                              'session': who.digest, 'private': private, 'configuration': configuration}, default=str)))
        observed = datetime.now(timezone.utc)
        return {**public, 'observed_at': observed.isoformat(),
                'valid_until': (observed + timedelta(seconds=30)).isoformat()}, digest
