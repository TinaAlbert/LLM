"""V1-02 本人单行程的有限外发范围；不选动作、不授予费用。"""
from copy import deepcopy
from datetime import datetime, timezone
import json

from services.business.guide_projection import validate_public
from .action_contract import evidence_context
from .assistant import SYSTEM_V14, display_state
from .experiment import NAVIGATION_TOOL, MAX_INPUT_BYTES, input_size_bytes
from .knowledge import fingerprint
from .model import MODEL, MODEL_REVISION, ENDPOINT, ModelUnavailable
from .page_observation import Observation, observation_payload
from .task_budget import TaskPermitBinding, validated_q19_permit

BATCH = 'Q36'
GOALS = (
    '请带我去预订与票据页面查看当前行程。',
    '能否为我办理该产品未说明的国际签证？',
)
FEEDBACK = '请根据本轮状态继续。'
PAGES = ['welcome', 'profile', 'trips', 'booking']
CASES = [('V1-B', 1), ('V1-D', 1)]


def reject(reason='scope_invalid'):
    raise ModelUnavailable(BATCH.lower() + '_' + reason)


def same(a, b):
    return fingerprint(a) == fingerprint(b)


def scope_spec(release, observations, sequence):
    case, rounds = CASES[sequence - 1]
    return {'schema': 'v1-personal-scope-1', 'task_ref': BATCH.lower() + '-' + case,
        'case_id': case, 'group': 'personal', 'max_rounds': rounds,
        'goal': GOALS[sequence - 1], 'feedback': FEEDBACK, 'sequence': sequence,
        'initial_page': 'welcome' if case == 'V1-B' else 'booking',
        'model': MODEL, 'revision': MODEL_REVISION, 'endpoint': ENDPOINT,
        'release_version': release['version'], 'knowledge_hash': release['knowledge_hash'],
        'prompt_version': 'assistant-14', 'product_version': 'v1',
        'observations': observations, 'initial_state': 'valid_ticket',
        'fixture': {'actor': 'personal_alice', 'flight_id': 'DEMO-PEK-SHA',
            'travel_date': '2026-10-01', 'passengers': 1, 'revision': 1,
            'business_time': '2026-09-27T00:00:00+00:00', 'amount_minor': 110000},
        'limits': {'input_bytes': 12288, 'output_tokens': 512, 'task_seconds': 300,
            'batch_seconds': 1200, 'requests': 2, 'reserved_cny': '0.064'},
        'worker': 'disabled-valid-ticket-readonly-continuation', 'stop': 'first_failure_no_retry'}


class PersonalScope:
    def __init__(self, release, observations, sequence, grant_path):
        if sequence not in range(1, len(CASES) + 1) or release['prompt_version'] != 'assistant-14':
            reject()
        self.release = deepcopy(release)
        self.spec = scope_spec(release, observations, sequence)
        self.scope_hash = fingerprint(self.spec)
        self.sequence = sequence
        self.grant_path = grant_path
        self.pending = None
        self.consumed = True

    def freeze(self, payload, raw, *, history, question, page, observation, round_index):
        if not 1 <= round_index <= self.spec['max_rounds']:
            reject('round_limit')
        validate_public(raw)
        fixture = self.spec['fixture']
        trip = raw['data']['trip']
        if (any(trip[k] != fixture[k] for k in ('flight_id', 'travel_date', 'passengers', 'revision'))
                or raw['business_time'] != fixture['business_time']
                or raw['state_versions'] != {'trip': 1, 'payment_profile': 1}
                or raw['payment_profile_state'] != 'usable' or raw['flight_state'] != 'available'
                or raw['unknowns']):
            reject('state_outside_fixture')
        for kind in ('quote', 'lock', 'payment'):
            value = raw['data'][kind]
            if value and value['amount_minor'] != fixture['amount_minor']:
                reject('amount_outside_fixture')
        if round_index == 1:
            if page != self.spec['initial_page'] or history or question != self.spec['goal']:
                reject('initial_input_changed')
            if (not raw['data']['ticket'] or raw['data']['ticket']['status'] != 'valid'
                    or not raw['data']['payment'] or raw['data']['payment']['status'] != 'succeeded'
                    or not raw['data']['ticket_task'] or raw['data']['ticket_task']['status'] != 'succeeded'):
                reject('prerequisite_ticket_missing')
        elif question != FEEDBACK:
            reject('feedback_changed')
        obs = observation_payload(Observation.model_validate(observation))
        frozen = self.spec['observations'].get(page)
        if (frozen is None or obs['page'] != page or obs['title'] != frozen['title']
                or any(item not in frozen['items'] for item in obs['items'])
                or not obs.get('semantics', {}).get('selected_trip')):
            reject('observation_changed')
        if len(history) > 4:
            reject('history_changed')
        expected = {'messages': [{'role': 'system', 'content': SYSTEM_V14},
            {'role': 'user', 'content': ''}], 'tools': [NAVIGATION_TOOL], 'max_tokens': 512, 'json_output': False}
        context = {'goal': self.spec['goal'], 'current_page': page,
            'current_state': display_state(raw, 'assistant-14'), 'allowed_pages': PAGES,
            'knowledge_hash': self.release['knowledge_hash'], 'history': history, 'question': question,
            'published_learning': {'source_kind': 'published_knowledge', 'product_version': 'v1',
                'legacy_contract': False, 'conditional_structure': self.release['validation']['conditional_structure'],
                'knowledge': self.release['knowledge']},
            'current_observation': dict(obs, source_kind='client_observed'),
            'action_evidence': evidence_context(raw, self.release['knowledge_hash'])}
        expected['messages'][1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
        if not same(expected, payload) or input_size_bytes(payload) > MAX_INPUT_BYTES:
            reject('actual_input_changed')
        self.pending = deepcopy(payload)
        self.binding = TaskPermitBinding(self.spec['task_ref'], self.sequence, round_index, 1,
            fingerprint(payload), fingerprint(payload), self.scope_hash)
        self.consumed = False
        return deepcopy(context)

    def check_actual(self, value):
        if self.pending is None or self.consumed or not same(value, self.pending):
            reject('actual_input_changed')

    def authorize_actual(self, value):
        self.check_actual(value)
        self.consumed = True
        return validated_q19_permit(self.binding, self.grant_path, batch=BATCH)
