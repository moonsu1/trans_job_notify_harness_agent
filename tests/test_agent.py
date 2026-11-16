import json
from types import SimpleNamespace as NS

import pytest

from career_scout.agent import Agent, AgentError, assessment_wire_schema, jd_passages
from career_scout.core import now


class Responses:
    def __init__(self, assessment, invalid_first=False, error=False):
        self.assessment = assessment
        self.calls = []
        self.invalid_first = invalid_first
        self.error = error
        self.fit_calls = 0

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise ConnectionError('simulated')
        if kwargs.get('text', {}).get('format', {}).get('name') == 'independent_role_scope':
            scope = dict(self.assessment['role_scope'])
            scope.update(duty_quotes=[1], ai_is_core=True, ai_only_optional=False,
                         primary_deliverable='AI Agent 서비스')
            return NS(usage=NS(input_tokens=1000, output_tokens=400), output=[],
                      status='completed', output_text=json.dumps(scope, ensure_ascii=False))
        self.fit_calls += 1
        text = json.dumps(self.assessment, ensure_ascii=False)
        if self.invalid_first and self.fit_calls == 1:
            text = '{}'
        return NS(usage=NS(input_tokens=1000, output_tokens=400), output=[], status='completed', output_text=text)


def test_api_usage_settled_and_repair_bounded(config, store, job, assessment):
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    responses = Responses(assessment, invalid_first=True)
    agent.client = NS(responses=responses)
    result = agent.analyze(job)
    assert result.employer == 'SK텔레콤'
    fit_calls = [c for c in responses.calls if c['text']['format']['name'] == 'job_assessment']
    assert len(fit_calls) == 2
    assert fit_calls[0]['store'] is False
    assert fit_calls[0]['reasoning'] == {'effort': 'none'}
    assert fit_calls[1]['reasoning'] == {'effort': 'low'}
    assert store.spend(now().isoformat()[:10]) == pytest.approx(.00765)


def test_api_unknown_failure_not_free(config, store, job):
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    agent.client = NS(responses=Responses(None, error=True))
    with pytest.raises(AgentError):
        agent.analyze(job)
    assert store.spend(now().isoformat()[:10]) > 0
    assert store.db.execute('SELECT status FROM usage').fetchone()[0] == 'uncertain'


def test_role_classifier_has_no_profile_and_caches_by_jd(config, store, job, assessment):
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    responses = Responses(assessment)
    agent.client = NS(responses=responses)
    assert agent.classify_role(job).decision == 'target'
    assert agent.classify_role(job).decision == 'target'
    assert len(responses.calls) == 1
    assert set(json.loads(responses.calls[0]['input'])) == {'title', 'jd_passages'}
    job.body += ' 추가된 실제 JD 업무.'
    agent.classify_role(job)
    assert len(responses.calls) == 2


def test_optional_ai_cannot_trigger_fit_scoring(config, store, job, assessment):
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    responses = Responses(assessment)

    def optional_response(**kwargs):
        result = responses.create(**kwargs)
        raw = json.loads(result.output_text)
        raw.update(ai_is_core=False, ai_only_optional=True)
        result.output_text = json.dumps(raw)
        return result

    agent.client = NS(responses=NS(create=optional_response))
    assert agent.analyze(job).role_scope.decision == 'out_of_scope'
    assert responses.fit_calls == 0


def test_korean_particles_do_not_hide_llm_development():
    from career_scout.policy import AI_PRODUCT
    assert AI_PRODUCT.search('LLM과 멀티모달 모델로 프로덕션 시스템을 개발해요')
    assert AI_PRODUCT.search('RAG를 통한 검색과 평가 시스템 구축')


def test_actual_legal_employer_enforces_si_exclusion(config, store, job, assessment):
    job.body += ' 실제 채용 법인: 현대오토에버'
    assessment['employer'] = '현대오토에버'
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    agent.client = NS(responses=Responses(assessment))
    result = agent.analyze(job)
    assert result.scope_failure == 'SI·컨설팅 등 제외 기업'


def test_wire_schema_prevents_duplicate_categories(config, store, job, assessment):
    schema = assessment_wire_schema()['properties']['evidence']
    assert schema['type'] == 'object'
    assert set(schema['required']) == {'role', 'technical', 'production', 'leadership'}
    assert schema['additionalProperties'] is False
    assessment['evidence'] = {e['category']: {k: v for k, v in e.items() if k != 'category'}
                              for e in assessment['evidence']}
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    agent.client = NS(responses=Responses(assessment))
    assert len(agent.analyze(job).evidence) == 4


def test_passage_ids_resolve_to_actual_jd_and_profile_ids_are_enum(config, store, job, assessment):
    passages = jd_passages(job)
    schema = assessment_wire_schema(passages, config[1])
    ids = schema['properties']['evidence']['properties']['role']['properties']['profile_id']['enum']
    assert 'work_experience_months' in ids and '75' not in ids
    assessment['evidence'] = {e['category']: {**{k: v for k, v in e.items() if k not in ('category', 'jd_quote')},
                                            'jd_quote_id': 1} for e in assessment['evidence']}
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    agent.client = NS(responses=Responses(assessment))
    result = agent.analyze(job)
    assert all(e.jd_quote == passages[1]['text'] for e in result.evidence)
    assert passages[1]['text'] in job.body


def test_summary_passage_ids_and_unverified_metadata(config, store, job, assessment):
    passages = jd_passages(job)
    assessment['duties'] = [1]
    assessment['required'] = [1]
    assessment['scope_quote'] = -1
    assessment['compensation_quote'] = '연봉 9억원'
    agent = Agent(config[0], config[1], store, api_key='not-a-real-key')
    agent.client = NS(responses=Responses(assessment))
    result = agent.analyze(job)
    assert result.duties == [passages[1]['text']]
    assert result.compensation_quote == ''
