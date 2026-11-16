from copy import deepcopy

import pytest

from career_scout.agent import validate_assessment
from career_scout.core import Assessment, assessment_view, canonical_url, prefilter, role_relevant


@pytest.mark.parametrize('tier,location,excluded', [
    (1, '충북 청주', False), (1, '경기 이천', False), (1, '천안', False),
    (1, 'Seoul, South Korea', False), (1, 'Tokyo Japan', True),
    (2, '서울', False), (2, '경기도 화성', False), (2, '부산', True),
    (2, '충북 청주', True), (3, '인천', True), (3, 'Seoul', False),
    (3, 'San Francisco, USA', True), (3, '', False),
    (3, 'Taipei, Taiwan', True), (3, 'Singapore', True), (3, 'KR', False),
])
def test_region(config, job, tier, location, excluded):
    job.tier, job.location = tier, location
    assert bool(prefilter(job, config[0])) == excluded


def test_salary_unknown_and_si(config, job):
    assert not prefilter(job, config[0])
    job.compensation = '연봉 1억원'
    assert not prefilter(job, config[0])
    job.company = '현대오토에버'
    assert '제외 기업' in prefilter(job, config[0])


@pytest.mark.parametrize('title', ['AI 인턴', 'AI Engineer Internship', 'LLM 계약직', '신입사원 AI',
                                  'Data Analyst (Contract: 1 year)', '산업기능요원 AI Engineer'])
def test_employment_excluded(config, job, title):
    job.title = title
    assert prefilter(job, config[0])


def test_hard_failure_and_unknowns(assessment):
    assert assessment_view(assessment, True)['grade'] == '높음'
    assessment['important_unknowns'] = ['근무지 미확인']
    assert assessment_view(assessment, True)['grade'] == '판단 보류'
    assessment['hard_failures'] = ['필수 자격 미충족']
    assert assessment_view(assessment, True)['grade'] == '낮음'
    assert not assessment_view(assessment, True)['recommended']


def test_no_fake_probability(assessment):
    result = assessment_view(assessment, True)
    assert result['score'] == 90
    assert '산정 불가' in result['probability']
    assert '%' not in result['probability']


def test_quotes_verified(config, job, assessment):
    assert validate_assessment(Assessment(**assessment), job, config[1])
    assessment['evidence'][0]['jd_quote'] = '없는 내용의 환각 인용입니다'
    with pytest.raises(ValueError, match='직접 인용'):
        validate_assessment(Assessment(**assessment), job, config[1])


def test_fabricated_field_rejected(config, job, assessment):
    assessment['compensation_quote'] = '연봉 1억 5천만원'
    with pytest.raises(ValueError, match='추출 항목'):
        validate_assessment(Assessment(**assessment), job, config[1])


def test_duplicate_score_rejected(config, job, assessment):
    assessment['evidence'].append(deepcopy(assessment['evidence'][0]))
    with pytest.raises(ValueError, match='중복'):
        validate_assessment(Assessment(**assessment), job, config[1])


def test_source_unverified(assessment):
    assert assessment_view(assessment, False)['grade'] == '판단 보류'


def test_canonical_preserves_job_ids():
    a = canonical_url('https://example.com/job/?id=1&utm_source=x#foo')
    assert a == 'https://example.com/job?id=1'
    assert a != canonical_url('https://example.com/job?id=2')


def test_ai_boilerplate_not_job_match(config, job):
    job.title = 'Storage Operations Manager'
    job.body = 'We are an AI-powered company. Storage and tax operations.'
    assert not role_relevant(job, config[0])
    job.body = 'Develop LangGraph and LLM retrieval workflows.'
    assert role_relevant(job, config[0])


def test_profile_and_skt(config):
    settings, profile, companies = config
    assert any(c['id'] == 'skt' and c['tier'] == 3 for c in companies)
    assert profile['work_experience_months'] >= 72
    assert settings['monthly_budget_usd'] == 10
    assert '생년월일' not in str(profile)


def test_budget_schedule_and_source_switches_do_not_invalidate_semantic_cache(config):
    from career_scout.core import cache_fingerprint
    settings, profile, _ = config
    baseline = cache_fingerprint(settings, profile)
    changed = dict(settings, daily_budget_usd=99, schedule_days_interval=5, allow_reposted_sources=False)
    assert cache_fingerprint(changed, profile) == baseline
    changed['model'] = 'different-model'
    assert cache_fingerprint(changed, profile) != baseline
