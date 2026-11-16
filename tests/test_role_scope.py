from copy import deepcopy

import pytest

from career_scout.agent import validate_assessment
from career_scout.core import Assessment, assessment_view, prefilter, role_relevant
from career_scout.policy import experience_check, role_exclusion


@pytest.mark.parametrize('title', [
    '[CPLB] 오퍼레이션 프로세스 자동화(AX) 담당자',
    '[쿠팡] 카탈로그 품질 검수 및 운영 프로세스 개선 (2년 이상)',
    '[쿠팡] 로켓배송 상품 판매 콘텐츠 에디터 (AI 콘텐츠 제작 경험 우대, 상품 포트폴리오 필수)',
])
def test_user_rejected_examples_cannot_be_recommended(config, job, assessment, title):
    job.title = title
    assert role_exclusion(title)
    assert prefilter(job, config[0])
    assert not role_relevant(job, config[0])
    a = validate_assessment(Assessment(**assessment), job, config[1])
    view = assessment_view(a, True)
    assert view['score'] is None and not view['recommended']


@pytest.mark.parametrize('title', ['Software Engineer - 테크코어 (AI Platform)',
                                  '[NAVER] AI 에이전트 프로덕트/서비스 기획 (경력)',
                                  'AI Agent 개발 PM/PL', 'Backend Engineer - GenAI Creative',
                                  'Machine Learning Engineer - AI Agent', 'AI Platform Engineer - Agent Runtime'])
def test_target_titles_not_excluded(title):
    assert not role_exclusion(title)


@pytest.mark.parametrize('title', [
    'MLOps Engineer', 'Software Engineer, Machine Learning - ML 인프라',
    'Data Analyst', 'Data Analytics Engineer', '데이터 분석 엔지니어',
    'AI 플랫폼 엔지니어 (Inference Pipeline)', 'Staff, Machine Learning Engineer (Ads)',
])
def test_user_excluded_mlops_and_analytics(config, job, assessment, title):
    job.title = title
    # A stray Agent/RAG sentence cannot override the main role's exclusion.
    assert prefilter(job, config[0])
    a = validate_assessment(Assessment(**assessment), job, config[1])
    assert assessment_view(a, True)['score'] is None


def test_generic_ai_or_llm_serving_alone_not_enough(config, job, assessment):
    quote = 'LLM 모델 추론 서버와 GPU 클러스터의 성능 최적화'
    job.body += quote
    assessment['role_scope']['duty_quotes'] = [quote]
    a = validate_assessment(Assessment(**assessment), job, config[1])
    assert a.role_scope.decision == 'unclear'
    assert not assessment_view(a, True)['recommended']


def test_collaborating_with_data_engineers_does_not_change_agent_role(config, job, assessment):
    quote = 'AI Agent를 개발하고 Data Engineers와 협력해 운영합니다.'
    job.body += quote
    assessment['role_scope']['duty_quotes'] = [quote]
    a = validate_assessment(Assessment(**assessment), job, config[1])
    assert a.role_scope.decision == 'target'


def test_actual_serving_role_heading_overrides_stale_link_title(config, job, assessment):
    quote = 'AI 플랫폼 엔지니어 (Serving) 모델 서빙과 GPU 클러스터 운영을 담당합니다.'
    job.body += quote
    assessment['role_scope']['duty_quotes'] = [quote]
    a = validate_assessment(Assessment(**assessment), job, config[1])
    assert a.role_scope.decision == 'out_of_scope'


def test_generic_skills_cannot_override_non_target(assessment):
    assessment['role_scope'].update(decision='out_of_scope', family='other', responsibility='use_ai_tools')
    assert assessment_view(assessment, True)['score'] is None
    assert not assessment_view(assessment, True)['recommended']


def test_75_months_not_seven_years():
    assert experience_check('7년 이상 콘텐츠 기획 및 제작 경험', 'content', 75)[0] == 'fail'
    assert experience_check('7+ years of experience', 'total', 75)[0] == 'fail'
    assert experience_check('5년 이상 데이터 품질 관리', 'data_operations', 75)[0] == 'unknown'
    assert experience_check('5년 이상의 실무 경력', 'total', 75)[0] == 'ok'
    assert experience_check('', 'unspecified', 75)[0] == 'ok'


def test_model_cannot_override_arithmetic(config, job, assessment):
    job.body += ' 7년 이상 개발 실무 경력 필수입니다.'
    assessment['experience_requirement'] = '7년 이상 개발 실무 경력'
    assessment['experience_domain'] = 'total'
    result = validate_assessment(Assessment(**assessment), job, config[1])
    assert result.hard_failures
    assert not assessment_view(result, True)['recommended']


def test_no_tenure_points_and_no_penalty_for_unstated_years(config, job, assessment):
    a = validate_assessment(Assessment(**deepcopy(assessment)), job, config[1])
    baseline = assessment_view(a, True)['score']
    assessment['experience_requirement'] = ''
    assessment['experience_domain'] = 'unspecified'
    b = validate_assessment(Assessment(**assessment), job, config[1])
    assert assessment_view(b, True)['score'] == baseline


def test_ai_tool_user_cannot_pass_as_ai_builder(config, job, assessment):
    assessment['role_scope']['responsibility'] = 'use_ai_tools'
    result = validate_assessment(Assessment(**assessment), job, config[1])
    assert result.role_scope.decision == 'unclear'
    assert assessment_view(result, True)['score'] is None
