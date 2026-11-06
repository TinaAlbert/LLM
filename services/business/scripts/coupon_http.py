"""C03真实HTTP两单流程；工程侧推进原worker，独立SQL判定，保留可恢复ID。"""

import argparse
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg

from services.business.config import load_settings
from services.business.db import connect, read_snapshot
from services.business.manage import check_marker, database_purpose
from services.business.worker import process_payment, process_ticket
from services.business.scripts.corporate_http import login, request_json, require


def verify(settings, client, alice, result):
    ws = settings.workspace_id
    expected_total = 140000 if ws == 'demo-coupon-large' else 148000
    with connect(settings.database_url) as conn:
        read_snapshot(conn)
        rows = conn.execute("""SELECT t.id,t.revision,l.id AS lock_id,l.trip_revision,
            l.status AS lock_status,p.id AS payment_id,p.status,p.amount_minor,
            pp.outcome,pp.captured_minor,j.status AS job_status,tt.status AS task_status,
            k.id AS ticket_id,k.trip_revision AS ticket_revision,
            (SELECT count(*) FROM payments x WHERE x.workspace_id=t.workspace_id AND x.trip_id=t.id) AS payments,
            (SELECT count(*) FROM ticket_tasks x WHERE x.workspace_id=p.workspace_id AND x.payment_id=p.id) AS tasks,
            (SELECT count(*) FROM tickets x WHERE x.workspace_id=p.workspace_id AND x.payment_id=p.id) AS tickets
            FROM trips t JOIN payments p ON (p.workspace_id,p.trip_id)=(t.workspace_id,t.id)
            JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)
            JOIN provider_payments pp ON (pp.workspace_id,pp.operation_id)=(p.workspace_id,p.id)
            JOIN payment_jobs j ON (j.workspace_id,j.payment_id)=(p.workspace_id,p.id)
            JOIN ticket_tasks tt ON (tt.workspace_id,tt.payment_id)=(p.workspace_id,p.id)
            JOIN tickets k ON (k.workspace_id,k.task_id)=(tt.workspace_id,tt.id)
            WHERE t.workspace_id=%s ORDER BY t.flight_id""", (ws,)).fetchall()
        require(len(rows) == 2, '独立SQL未找到两张对应票据')
        require({str(row['id']) for row in rows} == set(result['trip_ids']), '票据不是本次两行程')
        require(sum(row['captured_minor'] for row in rows) == expected_total, '实际总扣款不符')
        for row in rows:
            require(row['revision'] == row['trip_revision'] == row['ticket_revision'] == 1, '修订不一致')
            require(row['status'] == row['outcome'] == row['task_status'] == 'succeeded', '付款或出票未成功')
            require(row['lock_status'] == 'consumed' and row['job_status'] == 'done', '资源/任务未终结')
            require(row['payments'] == row['tasks'] == row['tickets'] == 1, '存在重复付款或任务')
            require(row['amount_minor'] == row['captured_minor'], '扣款与冻结金额不符')
        coupon = conn.execute('SELECT * FROM coupons WHERE workspace_id=%s', (ws,)).fetchall()
        require(len(coupon) == 1 and coupon[0]['status'] == 'consumed', '券未唯一消费')
        locks = conn.execute("SELECT id FROM booking_locks WHERE workspace_id=%s AND coupon_id=%s AND status='consumed'",
                             (ws, coupon[0]['id'])).fetchall()
        require(locks == [{'id': coupon[0]['lock_id']}], '券归属与已消费锁不一致')
        require(conn.execute("SELECT count(*) AS n FROM business_events WHERE workspace_id=%s AND action='coupon.consumed'",
                             (ws,)).fetchone()['n'] == 1, '券消费不止一次')
        flights = conn.execute('SELECT capacity,reserved,sold FROM flights WHERE workspace_id=%s', (ws,)).fetchall()
        require(len(flights) == 2 and all(row == {'capacity': 2, 'reserved': 0, 'sold': 1} for row in flights), '库存不守恒')
    bob = login(client, 'personal_bob')
    for row in rows:
        ticket = request_json(client, 'get', '/tickets/' + str(row['ticket_id']), alice)
        require(ticket['status'] == 'valid', 'HTTP票据不可重读')
        require(client.get('/tickets/' + str(row['ticket_id']), headers=bob).status_code == 404, '跨用户票据泄露')
    require(client.get('/coupons/' + str(coupon[0]['id']), headers=bob).status_code == 404, '跨用户券泄露')
    return {'workspace': ws, 'tickets': 2, 'captured_minor': expected_total, 'coupon_consumed': 1, 'verification': 'passed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'verify'])
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    settings = load_settings()
    ws = settings.workspace_id
    require(ws in {'demo-coupon-large', 'demo-coupon-small'}, '只允许隔离的开发券工作空间')
    with connect(settings.database_url) as conn:
        require(database_purpose(conn) == 'demo', '只允许项目开发库')
        check_marker(conn, 'demo')
    record = Path(__file__).resolve().parents[3] / '.local' / (ws + '-http-result.json')
    if record.exists():
        result = json.loads(record.read_text(encoding='utf-8'))
        require(result['workspace'] == ws, '记录工作空间不符')
    else:
        require(args.action == 'run', '尚无本工作空间运行记录')
        result = {'workspace': ws, 'run_id': uuid4().hex, 'stage': 'starting'}
        with record.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, indent=2)

    def save():
        record.write_text(json.dumps(result, indent=2), encoding='utf-8')

    with httpx.Client(base_url=f'http://127.0.0.1:{args.port}', trust_env=False, timeout=15) as client:
        alice = login(client, 'personal_alice')
        # 防止CLI选择的空间与8000上实际API配置不同；只在工程侧核对会话摘要。
        digest = hashlib.sha256(alice['Authorization'].removeprefix('Bearer ').encode()).hexdigest()
        with connect(settings.database_url) as conn:
            require(conn.execute('SELECT 1 FROM sessions WHERE workspace_id=%s AND token_hash=%s',
                                 (ws, digest)).fetchone() is not None, 'API实际工作空间与CLI不匹配；未执行业务写入')
        if args.action == 'run' and result['stage'] != 'issued':
            def write(path, body, suffix, method='post'):
                return request_json(client, method, path, {**alice, 'Idempotency-Key': result['run_id'] + suffix},
                                    body=body, statuses=(200, 201, 202))
            write('/payment-profile', {'source_ref': 'demo_personal_alice', 'expected_revision': 0}, 'bind', 'put')
            trips = [write('/trips', {'flight_id': 'DEMO-' + size, 'travel_date': '2026-10-01', 'passengers': 1}, size)
                     for size in ('LARGE', 'SMALL')]
            result['trip_ids'] = [trip['id'] for trip in trips]
            save()
            quotes = [write(f"/trips/{trip['id']}/quotes", {'expected_revision': 1}, 'quote-' + str(i))
                      for i, trip in enumerate(trips)]
            order = (0, 1) if ws == 'demo-coupon-large' else (1, 0)
            bookings = [write(f"/quotes/{quotes[i]['id']}/lock", {}, 'lock-' + str(i)) for i in order]
            expected = (80000, 60000) if order == (0, 1) else (48000, 100000)
            require(tuple(row['amount_minor'] for row in bookings) == expected, '锁价金额不符')
            result.update(stage='locked', lock_ids=[row['id'] for row in bookings])
            save()
            payments = [write(f"/locks/{row['id']}/payments", {'amount_minor': row['amount_minor']}, 'pay-' + str(i))
                        for i, row in enumerate(bookings)]
            result.update(stage='accepted', payment_ids=[row['id'] for row in payments])
            save()
            for payment in payments:
                settled = process_payment(settings.database_url, ws, payment['id'])
                process_ticket(settings.database_url, ws, settled['ticket_task_id'])
            result['stage'] = 'issued'
            save()
        print(json.dumps(verify(settings, client, alice, result), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except psycopg.Error:
        raise SystemExit('PostgreSQL核验失败；检查保留实例与迁移，不打印连接详情。')
    except (ValueError, httpx.HTTPError) as error:
        raise SystemExit(str(error) if isinstance(error, ValueError) else 'HTTP连接失败；检查本地API。')
