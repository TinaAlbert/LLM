"""工程验收控制：只重置测试库、推进已有任务、独立SQL断言。没有用户业务写入。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from services.business.config import load_settings
from services.business.db import connect, read_snapshot
from services.business.manage import check_marker, database_purpose, reset_test_database
from services.business.worker import process_payment, process_ticket, advance_time, expire


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    settings = load_settings(test=True)
    url, ws = settings.database_url, settings.workspace_id
    with connect(url) as conn:
        require(database_purpose(conn) == 'test' and ws == 'test', '只允许测试库/test')
        check_marker(conn, 'test')
    action, *args = sys.argv[1:]
    if action == 'guide-runtime-ready':
        from services.guide.windows import IsolationUnavailable, require_evidence
        try:
            require_evidence()
            return {'isolation_ready': True}
        except IsolationUnavailable:
            return {'isolation_ready': False}
    if action == 'reset':
        reset_test_database(url, 'llm_business_test', fixture=args[0])
        return {'reset':'test', 'fixture':args[0]}
    if action == 'advance':
        advance_time(url, ws, int(args[0]))
        expire(url, ws)
        return {'advanced': int(args[0])}
    if action in {'payment', 'ticket'}:
        with connect(url) as conn:
            # 恢复场景保留失败的历史付款；允许测试精确指定网页刚受理的原付款。
            payment_id = args[2] if len(args) > 2 else None
            payment = conn.execute('''SELECT id FROM payments WHERE workspace_id=%s AND trip_id=%s
                AND (%s::uuid IS NULL OR id=%s::uuid)''', (ws,args[0],payment_id,payment_id)).fetchone()
            require(payment is not None, '先从网页提交付款')
            task = conn.execute('SELECT id FROM ticket_tasks WHERE workspace_id=%s AND payment_id=%s', (ws,payment['id'])).fetchone()
        if action == 'payment':
            process_payment(url,ws,payment['id'],outcome=args[1] if len(args)>1 else 'succeeded')
        else:
            require(task is not None, '尚无持久出票任务')
            process_ticket(url,ws,task['id'],outcome=args[1] if len(args)>1 else 'succeeded')
        return {'progressed':action, 'original_payment':str(payment['id'])}
    if action == 'digest':
        from scripts.d3_evaluate import business_snapshot
        snapshot = business_snapshot(settings)
        # 复用固定23表独立只读连接；登录/撤销会话的认证摘要不参与业务相等判据。
        return {'digest':snapshot['sha256'],'table_count':len(snapshot['tables'])}
    with connect(url) as conn:
        read_snapshot(conn)
        conn.execute('SET TRANSACTION READ ONLY')
        if action == 'counts':
            return {table:conn.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']
                    for table in ('trips','quotes','booking_locks','payments','provider_payments','ticket_tasks','tickets')}
        if action == 'verify-recovery':
            phase, trip_id = args
            require(phase in {'failed', 'succeeded'}, '未知恢复阶段')
            trip = conn.execute('SELECT * FROM trips WHERE workspace_id=%s AND id=%s', (ws,trip_id)).fetchone()
            require(trip is not None and trip['owner_id']=='personal_alice', '恢复行程归属不符')
            rows = conn.execute('''SELECT p.id,p.status,p.amount_minor,l.id AS lock_id,l.status AS lock_status,
                l.trip_revision,l.amount_minor AS locked_minor,j.status AS job_status,
                pp.outcome,pp.captured_minor,tt.id AS task_id,tt.status AS task_status,
                tt.trip_id AS task_trip_id,k.id AS ticket_id,k.trip_revision AS ticket_revision,
                k.quantity AS ticket_quantity,k.payment_id AS ticket_payment_id,k.trip_id AS ticket_trip_id
                FROM payments p JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)
                JOIN payment_jobs j ON (j.workspace_id,j.payment_id)=(p.workspace_id,p.id)
                LEFT JOIN provider_payments pp ON (pp.workspace_id,pp.operation_id)=(p.workspace_id,p.id)
                LEFT JOIN ticket_tasks tt ON (tt.workspace_id,tt.payment_id)=(p.workspace_id,p.id)
                LEFT JOIN tickets k ON (k.workspace_id,k.task_id)=(tt.workspace_id,tt.id)
                WHERE p.workspace_id=%s AND p.trip_id=%s''', (ws,trip_id)).fetchall()
            failed = [r for r in rows if r['status']=='failed']
            require(len(failed)==1, '必须保留唯一失败付款')
            old = failed[0]
            require(old['outcome']=='failed' and old['captured_minor']==0 and old['lock_status']=='failed'
                    and old['job_status']=='done' and old['task_id'] is None and old['ticket_id'] is None,
                    '失败付款未终结、误扣款或生成票据')
            require(old['amount_minor']==old['locked_minor']==110000 and old['trip_revision']==trip['revision'],
                    '失败付款冻结金额或修订不符')
            expected_tickets = int(phase=='succeeded')
            require(len(rows)==1+expected_tickets, '恢复产生多余付款、任务或票据')
            if phase=='failed':
                require(trip['status']=='draft', '明确付款失败未回到可恢复草稿')
                active = conn.execute("SELECT count(*) AS n FROM quotes WHERE workspace_id=%s AND trip_id=%s AND status='active'", (ws,trip_id)).fetchone()['n']
                require(active==0, '失败后旧报价仍有效')
            else:
                require(trip['status']=='paid', '恢复未达到已付款状态')
                success = [r for r in rows if r['status']=='succeeded']
                require(len(success)==1, '恢复必须仅成功付款一次')
                current = success[0]
                require(current['outcome']=='succeeded' and current['captured_minor']==110000
                        and current['locked_minor']==current['amount_minor']==110000
                        and current['lock_status']=='consumed' and current['job_status']=='done'
                        and current['task_status']=='succeeded' and current['ticket_id'] is not None
                        and current['task_trip_id']==current['ticket_trip_id']==trip['id']
                        and current['ticket_payment_id']==current['id']
                        and current['trip_revision']==current['ticket_revision']==trip['revision']
                        and current['ticket_quantity']==trip['passengers']
                        and current['lock_id']!=old['lock_id'], '恢复付款、锁、任务和独立票据不一致')
            inventory = conn.execute('''SELECT reserved,sold,capacity FROM flights
                WHERE workspace_id=%s AND id=%s AND travel_date=%s''',
                (ws,trip['flight_id'],trip['travel_date'])).fetchone()
            require(inventory['reserved']==0 and inventory['sold']==expected_tickets
                    and inventory['sold']<=inventory['capacity'], '失败释放或恢复库存不守恒')
            budget = conn.execute("SELECT reserved_minor,used_minor FROM budget_projects WHERE workspace_id=%s AND id='A-eng'", (ws,)).fetchone()
            require(budget=={'reserved_minor':0,'used_minor':0}, '个人恢复误改企业预算')
            result = {'verification':'passed','phase':phase,'payments':len(rows),
                      'failed_payments':1,'tickets':expected_tickets,'captured_minor':110000*expected_tickets}
            report = ROOT/'.local'/'d3-14'/f'payment-recovery-{phase}.json'
            report.parent.mkdir(parents=True,exist_ok=True)
            report.write_text(json.dumps(result),encoding='utf-8')
            return result
        if action == 'verify':
            kind = args[0]
            expected = {'personal':110000,'corporate':100000,'large':140000,'small':148000}[kind]
            count = 2 if kind in {'large','small'} else 1
            rows = conn.execute("""SELECT t.id,t.revision,t.flight_id,l.id AS lock_id,l.trip_revision,l.amount_minor,
                l.status AS lock_status,p.id AS payment_id,p.status,pp.outcome,pp.captured_minor,
                j.status AS job_status,tt.status AS task_status,k.trip_revision AS ticket_revision,
                p.approval_id,(SELECT count(*) FROM payments x WHERE x.trip_id=t.id) AS payments,
                (SELECT count(*) FROM ticket_tasks x WHERE x.payment_id=p.id) AS tasks,
                (SELECT count(*) FROM tickets x WHERE x.payment_id=p.id) AS tickets
                FROM trips t JOIN payments p ON (p.workspace_id,p.trip_id)=(t.workspace_id,t.id)
                JOIN booking_locks l ON (l.workspace_id,l.id)=(p.workspace_id,p.lock_id)
                JOIN provider_payments pp ON (pp.workspace_id,pp.operation_id)=(p.workspace_id,p.id)
                JOIN payment_jobs j ON (j.workspace_id,j.payment_id)=(p.workspace_id,p.id)
                JOIN ticket_tasks tt ON (tt.workspace_id,tt.payment_id)=(p.workspace_id,p.id)
                JOIN tickets k ON (k.workspace_id,k.task_id)=(tt.workspace_id,tt.id)
                WHERE t.workspace_id=%s""",(ws,)).fetchall()
            require(len(rows)==count, '有效独立票据数错误')
            require(sum(r['captured_minor'] for r in rows)==expected,'真实模拟扣款总额错误')
            if count == 2:
                expected_by_flight = {'DEMO-LARGE':80000,'DEMO-SMALL':60000} if kind=='large' else {'DEMO-LARGE':100000,'DEMO-SMALL':48000}
                require({r['flight_id']:r['captured_minor'] for r in rows}==expected_by_flight,'各行程真实扣款与顺序不符')
            for row in rows:
                require(row['revision']==row['trip_revision']==row['ticket_revision'], '修订未对应')
                require(row['status']==row['outcome']==row['task_status']=='succeeded','付款/出票未成功')
                require(row['lock_status']=='consumed' and row['job_status']=='done','原资源/任务未终结')
                require(row['payments']==row['tasks']==row['tickets']==1,'重复付款/任务/票据')
                require(row['captured_minor']==row['amount_minor'],'未按冻结金额扣款')
                if kind=='corporate':
                    a=conn.execute('SELECT * FROM approval_requests WHERE id=%s',(row['approval_id'],)).fetchone()
                    require(a and a['status']=='approved' and a['resource_status']=='consumed','无有效批准')
                    require(a['lock_id']==row['lock_id'] and a['trip_revision']==row['revision'],'批准未绑定原锁/修订')
            budget=conn.execute("SELECT total_minor,reserved_minor,used_minor,policy_revision FROM budget_projects WHERE id='A-eng'").fetchone()
            require(budget=={'total_minor':200000,'reserved_minor':0,'used_minor':100000 if kind=='corporate' else 0,'policy_revision':1},'预算不守恒')
            flights=conn.execute("""SELECT capacity,reserved,sold,
                (SELECT coalesce(sum(quantity),0) FROM booking_locks l WHERE
                  (l.workspace_id,l.flight_id,l.travel_date)=(f.workspace_id,f.id,f.travel_date)
                  AND l.status='consumed') AS expected_sold
                FROM flights f""").fetchall()
            require(sum(f['sold'] for f in flights)==count and all(f['reserved']==0 and 0<=f['sold']<=f['capacity'] for f in flights),'库存不守恒')
            require(all(f['sold']==f['expected_sold'] for f in flights),'库存未对应原航段/日期的消费锁')
            if count==2:
                coupons=conn.execute('SELECT * FROM coupons').fetchall()
                require(len(coupons)==1 and coupons[0]['status']=='consumed','券未唯一消费')
                locked=conn.execute("SELECT id FROM booking_locks WHERE coupon_id=%s AND status='consumed'",(coupons[0]['id'],)).fetchall()
                require(locked==[{'id':coupons[0]['lock_id']}],'券归属错误')
                require(conn.execute("SELECT count(*) AS n FROM business_events WHERE action='coupon.consumed'").fetchone()['n']==1,'券重复消费')
            result={'verification':'passed','scenario':kind,'tickets':count,'captured_minor':expected}
            path=ROOT/'.local'/f'd1-05-browser-{kind}.json'
            path.write_text(json.dumps(result),encoding='utf-8')
            return result
    raise ValueError('未知控制动作')


if __name__=='__main__':
    try:
        print(json.dumps(main(),ensure_ascii=False))
    except Exception as error:
        # 不输出连接栈/凭证；断言仅为本文件固定文案。
        print(str(error) if isinstance(error,ValueError) else type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
