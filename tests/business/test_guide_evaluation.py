"""工程判定器的真实PG独立终态；无模型调用，不计AI闭环或基线成绩。"""
import pytest

from scripts import d3_evaluate as evaluation
from services.business import worker
from test_personal import lock, pay, quote, ready

pytestmark = pytest.mark.integration


def independently_check(settings, trip):
    """判定器另开只读快照；独立连接确认原23表没有被它改变。"""
    before = evaluation.business_snapshot(settings)
    report = evaluation.verify_ticket(trip['id'])
    after = evaluation.business_snapshot(settings)
    assert before['tables'] == after['tables']
    assert before['authentication'] == after['authentication']
    assert report['model_quality'] == report['web_loop'] == 'not_assessed'
    assert set(report) == {'schema', 'kind', 'checked_at', 'object_sha256', 'checks',
                           'independent_ticket_valid', 'model_quality', 'web_loop'}
    return report


def finish_original_path(settings, client, alice, create):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    assert payment['status'] == 'processing'
    paid = worker.process_payment(settings.database_url, 'test', payment['id'])
    assert paid['status'] == 'succeeded'
    task = worker.process_ticket(settings.database_url, 'test', paid['ticket_task_id'])
    assert task['status'] == 'succeeded'
    return trip


def finish_after_confirmed_failure(settings, client, alice, create):
    """复用原业务的明确失败恢复：新报价/新锁/新付款，历史失败扣款为0。"""
    trip, _, _, booking = ready(client, alice, create)
    failed_payment = pay(client, alice, booking)
    failed = worker.process_payment(settings.database_url, 'test', failed_payment['id'], 'failed')
    assert failed['status'] == 'failed'
    assert independently_check(settings, trip)['independent_ticket_valid'] is False
    new_quote = quote(client, alice, trip, key='recovery-quote')
    new_lock = lock(client, alice, new_quote, key='recovery-lock')
    new_payment = pay(client, alice, new_lock, key='recovery-pay')
    paid = worker.process_payment(settings.database_url, 'test', new_payment['id'])
    assert paid['status'] == 'succeeded'
    task = worker.process_ticket(settings.database_url, 'test', paid['ticket_task_id'])
    assert task['status'] == 'succeeded'
    return trip, failed_payment


def test_processing_unknown_paid_without_ticket_and_failed_ticket_never_complete(
    settings, client, alice, create, sql,
):
    trip, _, _, booking = ready(client, alice, create)
    payment = pay(client, alice, booking)
    assert payment['status'] == 'processing'
    processing = independently_check(settings, trip)
    assert processing['checks'] == {'one_joined_ticket': False, 'inventory_conserved': True}
    assert processing['independent_ticket_valid'] is False

    unknown = worker.process_payment(settings.database_url, 'test', payment['id'], 'unknown')
    assert unknown['status'] == 'unknown'
    assert independently_check(settings, trip)['independent_ticket_valid'] is False

    paid = worker.process_payment(settings.database_url, 'test', payment['id'])
    assert paid['status'] == 'succeeded'
    assert sql("SELECT status FROM ticket_tasks WHERE workspace_id='test'") == [{'status': 'pending'}]
    assert independently_check(settings, trip)['independent_ticket_valid'] is False

    failed = worker.process_ticket(settings.database_url, 'test', paid['ticket_task_id'], 'failed')
    assert failed['status'] == 'failed'
    assert sql("SELECT count(*) AS n FROM tickets WHERE workspace_id='test'") == [{'n': 0}]
    assert independently_check(settings, trip)['independent_ticket_valid'] is False


def test_original_api_and_worker_ticket_passes_independent_checks(settings, client, alice, create):
    trip = finish_original_path(settings, client, alice, create)
    report = independently_check(settings, trip)
    assert report['independent_ticket_valid'] is True
    assert all(report['checks'].values())
    assert report['checks']['paid_trip'] is True
    assert report['checks']['frozen_route_matches_current_trip'] is True
    assert report['checks']['nonempty_ticket_certificate'] is True


def test_confirmed_failed_payment_then_new_payment_ticket_is_valid(
    settings, client, alice, create, sql,
):
    trip, _ = finish_after_confirmed_failure(settings, client, alice, create)
    assert sql("SELECT status,count(*) AS n FROM payments WHERE workspace_id='test' GROUP BY status ORDER BY status") == [
        {'status': 'failed', 'n': 1}, {'status': 'succeeded', 'n': 1}]
    assert sql("SELECT sum(captured_minor) AS captured FROM provider_payments WHERE workspace_id='test'") == [
        {'captured': 110000}]
    report = independently_check(settings, trip)
    assert report['independent_ticket_valid'] is True
    assert report['checks']['unique_payment_task_ticket'] is True
    assert report['checks']['prior_failed_payments_have_no_capture'] is True
    assert report['checks']['frozen_amount_captured_once'] is True


@pytest.mark.parametrize('history', ['extra_capture', 'missing_provider_result'])
def test_prior_failure_cannot_hide_capture_or_unverified_provider_result(
    settings, client, alice, create, sql, history,
):
    trip, failed_payment = finish_after_confirmed_failure(settings, client, alice, create)
    assert independently_check(settings, trip)['independent_ticket_valid'] is True
    if history == 'extra_capture':
        changed = sql("UPDATE provider_payments SET outcome='succeeded',captured_minor=amount_minor "
            "WHERE workspace_id='test' AND operation_id=%s RETURNING operation_id", (failed_payment['id'],))
    else:
        changed = sql("DELETE FROM provider_payments WHERE workspace_id='test' AND operation_id=%s "
            "RETURNING operation_id", (failed_payment['id'],))
    assert len(changed) == 1
    report = independently_check(settings, trip)
    assert report['independent_ticket_valid'] is False
    assert report['checks']['prior_failed_payments_have_no_capture'] is False
    assert report['checks']['frozen_amount_captured_once'] is (history != 'extra_capture')
    # 当前成功付款/唯一任务和票仍成立，失败仅来自独立历史扣款/确定性检查。
    assert report['checks']['unique_payment_task_ticket'] is True
    assert report['checks']['successful_payment_and_ticket'] is True
    assert report['checks']['inventory_conserved'] is True


@pytest.mark.parametrize(('corruption', 'failed_check'), [
    ('flight', 'frozen_route_matches_current_trip'),
    ('date', 'frozen_route_matches_current_trip'),
    ('empty_certificate', 'nonempty_ticket_certificate'),
    ('blank_certificate', 'nonempty_ticket_certificate'),
    ('control_whitespace_certificate', 'nonempty_ticket_certificate'),
    ('old_revision', 'current_revision'),
    ('unpaid_trip', 'paid_trip'),
])
def test_completed_rows_do_not_hide_invalid_ticket_snapshot(
    settings, client, alice, create, sql, corruption, failed_check,
):
    trip = finish_original_path(settings, client, alice, create)
    assert independently_check(settings, trip)['independent_ticket_valid'] is True
    # 仅工程侧在受保护test注入不一致；不是运行助手能力或任何模型反馈。
    statements = {
        'flight': "UPDATE trips SET flight_id='OTHER-FLIGHT' WHERE workspace_id='test' AND id=%s RETURNING id",
        'date': "UPDATE trips SET travel_date='2026-10-02' WHERE workspace_id='test' AND id=%s RETURNING id",
        'empty_certificate': "UPDATE tickets SET certificate='' WHERE workspace_id='test' AND trip_id=%s RETURNING id",
        'blank_certificate': "UPDATE tickets SET certificate='   ' WHERE workspace_id='test' AND trip_id=%s RETURNING id",
        'control_whitespace_certificate': "UPDATE tickets SET certificate=chr(9)||chr(10)||chr(13) WHERE workspace_id='test' AND trip_id=%s RETURNING id",
        'old_revision': "UPDATE trips SET revision=revision+1 WHERE workspace_id='test' AND id=%s RETURNING id",
        'unpaid_trip': "UPDATE trips SET status='draft' WHERE workspace_id='test' AND id=%s RETURNING id",
    }
    assert len(sql(statements[corruption], (trip['id'],))) == 1
    report = independently_check(settings, trip)
    assert report['independent_ticket_valid'] is False
    assert report['checks'][failed_check] is False
    # 这些异常都保持实际成功付款、票据关联及库存守恒，用于定位判据遗漏。
    assert report['checks']['one_joined_ticket'] is True
    assert report['checks']['successful_payment_and_ticket'] is True
    assert report['checks']['ticket_associations_match'] is True
    assert report['checks']['inventory_conserved'] is True
