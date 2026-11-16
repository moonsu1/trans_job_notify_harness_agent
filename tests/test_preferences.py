from collections import Counter

import pytest

from career_scout.core import prefilter
from career_scout.mailer import send_mail
from career_scout.planning import search_plan
from career_scout.policy import finance_employment
from career_scout.reporting import report_rows, selection


def test_live_configuration_has_all_four_financial_sectors_and_mobis(config):
    settings, _, companies = config
    counts = Counter(c.get('finance_category') for c in companies if c.get('finance_category'))
    assert counts == {'securities': 12, 'card': 8, 'bank': 10, 'insurance': 12}
    assert next(c for c in companies if c['id'] == 'mobis')['tier'] == 3
    assert not {'toss', 'coupang', 'daangn'} & {c['id'] for c in companies}
    assert settings['schedule_days_interval'] == 3
    assert settings['monthly_budget_usd'] == 10 and settings['daily_budget_usd'] == .9


@pytest.mark.parametrize('company', ['LG전자', 'LG에너지솔루션', '한화생명', 'Hanwha Life',
                                    '포스코DX', 'POSCO', 'CJ대한통운', '씨제이제일제당',
                                    'HD현대중공업', 'HD Hyundai', '토스뱅크', '토스증권',
                                    '비바리퍼블리카', '쿠팡페이', 'Coupang', '당근페이'])
def test_rejected_employers_override_scores_and_finance_categories(config, job, company):
    job.company = company
    assert '제외' in prefilter(job, config[0])


def test_candidate_employment_history_is_not_an_employer_filter(config, job):
    config[1]['experience'].append({'id': 'previous_employer', 'description': 'LG CNS 경력 예시'})
    job.body += ' 후보자 경력은 LG CNS이며 한화 프로젝트 고객사 경험도 있습니다.'
    assert not prefilter(job, config[0])
    assert any('LG CNS' in e['description'] for e in config[1]['experience'])


@pytest.mark.parametrize('kind,clause,expected', [
    ('정규직', '', 'confirmed'), ('Permanent', '', 'confirmed'),
    ('정규직(수습 3개월)', '', 'confirmed'), ('', '고용형태: 정규직', 'confirmed'),
    ('전문계약직', '', 'excluded'), ('계약직', '', 'excluded'),
    ('파트타이머', '', 'excluded'), ('단시간 근로자', '', 'excluded'),
    ('비정규직', '', 'excluded'), ('준정규직', '', 'excluded'),
    ('Non-permanent', '', 'excluded'), ('Non-regular employee', '', 'excluded'),
    ('정규직 또는 계약직', '', 'excluded'), ('정규직 전환형 인턴', '', 'excluded'),
    ('', '채용조건 - 전문계약직으로 채용합니다.', 'excluded'),
    ('', '채용형태: 최초 3개월 계약 후 정규직 전환 여부 결정', 'excluded'),
    ('Full-time', '', 'unknown'), ('', '', 'unknown'), ('', '정규직 직원용 RAG 서비스를 개발', 'unknown'),
])
def test_financial_contract_must_be_confirmed(config, job, kind, clause, expected):
    job.company_id = 'kb'
    job.employment_type = kind
    job.body = clause
    assert finance_employment(job, config[0])[0] == expected


def test_unknown_financial_employment_cannot_be_sent_as_high_fit_review(config, job, assessment, store):
    job.company_id, job.company, job.employment_type = 'kb', 'KB국민은행', ''
    assessment['employment_type'] = ''
    store.upsert(job)
    store.save_analysis(job.key, assessment, config[0]['fingerprint'])
    rows = report_rows(store, config[0])
    assert rows[0]['employment_gate'] == 'unknown'
    assert not selection(store, rows, include_all=True)


@pytest.mark.parametrize('company_id', ['toss', 'coupang', 'daangn'])
def test_persisted_old_scores_do_not_resurrect_removed_employers(config, job, assessment, store, company_id):
    job.company_id = company_id
    store.upsert(job)
    store.save_analysis(job.key, assessment, config[0]['fingerprint'])
    rows = report_rows(store, config[0])
    assert rows[0]['exclusion'] and rows[0]['view']['score'] is None
    assert not selection(store, rows, include_all=True)


def test_old_queued_mail_is_rechecked_before_any_credential_or_smtp_access(config, job, store, monkeypatch):
    import career_scout.mailer as mailer
    job.company_id = 'toss'
    store.upsert(job)
    key = store.create_mail('old', 'old body', 'old', [{'id': job.key, 'hash': 'old', 'kind': 'new'}])

    def must_not_connect(*args):
        raise AssertionError('No authentication or SMTP connection should occur')

    monkeypatch.setattr(mailer, 'credential', must_not_connect)
    assert send_mail(store, config[0], key) == 'superseded'
    assert send_mail(store, config[0], key) == 'superseded'


def test_regular_search_plan_covers_every_configured_employer_within_existing_limit(config):
    plan = search_plan(config[2], max_calls=config[0]['max_search_calls'])
    assert len(plan) <= 12
    assert {c['id'] for group, _ in plan for c in group} == {c['id'] for c in config[2]}
    finance_groups = [group for group, track in plan if track == 'combined']
    assert {group[0]['finance_category'] for group in finance_groups} == {'bank', 'card', 'insurance', 'securities'}
    for category in ('bank', 'card', 'insurance', 'securities'):
        group = next(g for g in finance_groups if g[0]['finance_category'] == category)
        assert {c['id'] for c in group} == {c['id'] for c in config[2] if c.get('finance_category') == category}


def test_factory_preference_does_not_expand_non_metro_locations(config, job):
    job.company_id, job.company, job.location = 'mobis', '현대모비스', '울산'
    assert prefilter(job, config[0]) == '서울·경기 외 근무지'


def test_financial_regular_confirmed_is_not_blocked_by_customer_contract_work(config, job):
    job.company_id = 'kb'
    job.body = '고용형태: 정규직. 금융 고객의 계약서 문서 검색 및 AI Agent를 개발합니다.'
    assert finance_employment(job, config[0])[0] == 'confirmed'
