"""V14导航生成协议：零模型；旧Q35原候选不补工具，正式交付守卫不放宽。"""
import asyncio
import hashlib
import json

import pytest

from services.guide.action_contract import (
    EVIDENCE_CONTRACT_VERSION, action_digest, evidence_context, validate_evidence_contract,
)
from services.guide.assistant import SYSTEM_V13, SYSTEM_V14, prompt_system
from test_assistant import knowledge, observation
from test_evidence_contract41 import harness, candidate, configure, request
from test_reply_receipts30 import app_for, post, receipt


# 来源：Q35 run-04/model-runs/9ccf333e7e7e098608c603a6b5e6e4eb.json。
# 原始content、basis、证据与tool_calls=[]保留；不将trips改成booking或补工具。
Q35_CONTENT = '{"v":2,"action":{"kind":"navigate","target":"task","entry":"navigate","params":{"page":"trips"}},"basis":{"state":"657c4f8d6cf0eebed44632dac5b273c91477be43465202de26065a1daea9a1d4","knowledge":"ee7bc9edd40d698b2fd401a4f5b0fca119519258882a68e9c56a98d88bd9b14b"},"evidence":[{"id":"selected_trip","value":true},{"id":"trip.ticket.status","value":"valid"}],"gap":null}'


def q35_state():
    # 从已外发current_state恢复被显示层转换的模拟金额；state摘要须与原basis逐字相同。
    return {
        'product_id': 'travel-lab', 'product_version': 'v1', 'contract_version': 'd3-personal-1',
        'knowledge_version': 'personal-guide-1', 'rule_version': 'booking-v1',
        'policy_version': 'subject-assignment-v1', 'feature_config_version': 'personal-1',
        'source_kind': 'observed', 'environment': 'local-demo', 'simulated_identity': 'synthetic_personal',
        'disclosure_scope': 'current-session',
        'assumptions': ['synthetic_demo_account', 'simulated_business_services'], 'unknowns': [],
        'business_time': '2026-09-27T00:00:00+00:00', 'state_versions': {'trip': 1, 'payment_profile': 1},
        'payment_profile_state': 'usable', 'flight_state': 'available',
        'data': {
            'trip': {'ref': 'trip', 'revision': 1, 'flight_id': 'DEMO-PEK-SHA',
                'travel_date': '2026-10-01', 'passengers': 1, 'status': 'paid'},
            'quote': {'ref': 'quote', 'trip_revision': 1, 'effective_status': 'active', 'valid': True,
                'expires_at': '2026-09-27T00:05:00+00:00', 'price_basis': 'estimate',
                'amount_minor': 110000, 'currency': 'DEMO'},
            'lock': {'ref': 'lock', 'trip_revision': 1, 'effective_status': 'consumed', 'valid': False,
                'expires_at': '2026-09-27T00:05:00+00:00', 'price_basis': 'frozen',
                'amount_minor': 110000, 'currency': 'DEMO'},
            'payment': {'ref': 'payment', 'status': 'succeeded', 'amount_minor': 110000, 'currency': 'DEMO'},
            'ticket_task': {'ref': 'ticket_task', 'status': 'succeeded'},
            'ticket': {'ref': 'ticket', 'status': 'valid', 'trip_revision': 1},
        },
        'observed_at': '2026-11-06T13:27:12.905563+00:00',
        'valid_until': '2026-11-06T13:27:42.905563+00:00',
    }


def test_v14_preserves_v13_and_adds_public_mapping_and_profile_tool_protocol():
    assert prompt_system('assistant-13') == SYSTEM_V13
    assert prompt_system('assistant-14') == SYSTEM_V14
    assert SYSTEM_V14.startswith(SYSTEM_V13)
    extra = SYSTEM_V14[len(SYSTEM_V13):]
    assert all(mapping in extra for mapping in (
        'welcome=欢迎与身份', 'profile=付款资料', 'trips=行程与报价', 'booking=预订与票据',
    ))
    assert 'content' in extra and '工具通道' in extra and 'navigate' in extra
    assert '"kind":"navigate"' in extra and '"page":"profile"' in extra
    assert all(key in extra for key in ('"v":2', '"basis"', '"evidence"', '"gap"'))
    assert 'page必须一致' in extra and '非导航动作不调用工具' in extra
    assert all(hidden not in extra for hidden in (
        '110000', '1100.00', '2026-09-27', '请带我去预订与票据页面查看当前行程',
        'worker', 'Q35', 'task_seconds', 'max_rounds',
    ))


def test_complete_original_q35_candidate_still_fails_without_tool_repair():
    assert hashlib.sha256(Q35_CONTENT.encode()).hexdigest() == (
        'b40526a30ae2196fd992b58da79858a63051ebd76c9c091474471a48304f6586'
    )
    value = json.loads(Q35_CONTENT)
    state = q35_state()
    assert action_digest(state) == value['basis']['state']
    context = evidence_context(state, value['basis']['knowledge'])
    with pytest.raises(ValueError, match='action_tool_mismatch'):
        validate_evidence_contract(Q35_CONTENT, state,
            ['welcome', 'profile', 'trips', 'booking'], [], context)


@pytest.mark.parametrize('mode', ['valid', 'missing', 'wrong_page'])
def test_v14_profile_formal_delivery_requires_matching_real_tool_shape(tmp_path, monkeypatch, mode):
    async def run():
        h = harness(tmp_path)
        release = h.releases.save_candidate(knowledge(),
            [observation('profile'), observation('trips')], prompt_version='assistant-14')
        h.releases.publish(release['version'])
        value = candidate(h, 'navigate')
        page = 'trips' if mode == 'wrong_page' else 'profile'
        calls = [] if mode == 'missing' else [
            {'id': 'synthetic-profile-tool', 'name': 'navigate', 'arguments': {'page': page}},
        ]
        configure(h, value, calls)
        result = await post(app_for(monkeypatch, h), request())
        report = json.loads(next(h.report_dir.glob('*.json')).read_text(encoding='utf-8'))
        assert h.calls[0]['messages'][0]['content'] == SYSTEM_V14
        assert json.loads(report['candidate']['content']) == value
        assert report['candidate']['tool_calls'] == calls
        assert report['prompt_version'] == 'assistant-14' and 'browser_ack' not in report
        if mode != 'valid':
            assert result.status_code == 503 and receipt(h)['reason'] == 'action_tool_mismatch'
            assert all(session.pending is None and not session.history for session in h.sessions.values())
            return
        assert result.status_code == 200
        formal = result.json()
        assert formal['action_contract'] == value
        assert formal['action_contract_version'] == EVIDENCE_CONTRACT_VERSION
        assert formal['navigation_status'] == 'proposed'
        assert formal['tool']['arguments'] == {'page': 'profile'}
        assert formal['action_sha256'] == action_digest(value)
    asyncio.run(run())
