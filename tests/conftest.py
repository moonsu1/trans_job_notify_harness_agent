import pytest

from career_scout.core import Job, load_config
from career_scout.storage import Store


@pytest.fixture(autouse=True)
def isolate_runtime_state(tmp_path, monkeypatch):
    monkeypatch.setenv('CAREER_SCOUT_DATA_DIR', str(tmp_path/'runtime'))
    monkeypatch.setenv('CAREER_SCOUT_REPORT_DIR', str(tmp_path/'reports'))
    monkeypatch.delenv('CAREER_SCOUT_OPERATION', raising=False)


@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path/'test.sqlite3')
    yield value
    value.close()


@pytest.fixture
def job():
    return Job(company_id='skt', company='SK텔레콤', tier=3,
               url='https://careers.sktelecom.com/jobs/123', title='LLM Agent Engineer',
               location='서울', employment_type='정규직',
               body='LangGraph 기반 Agent 개발과 운영, Golden Set 평가 자동화 경험을 요구합니다. '
                    'Python 백엔드 개발 경험 및 5년 이상의 실무 경력 필수. 서울 정규직. '
                    'SK텔레콤의 AI 플랫폼을 개발합니다.', verified=True, status='open')


@pytest.fixture
def assessment():
    return {
        'role_scope': {'decision': 'target', 'family': 'ai_engineer', 'responsibility': 'build_ai_service',
                       'duty_quotes': ['LangGraph 기반 Agent 개발과 운영'], 'reason': 'AI Agent 직접 개발'},
        'employer': 'SK텔레콤', 'organization': '', 'location': '서울',
        'employment_type': '정규직', 'experience_requirement': '5년 이상의 실무 경력',
        'experience_domain': 'total',
        'compensation_quote': '', 'duties': ['LangGraph 기반 Agent 개발과 운영'],
        'required': ['Python 백엔드 개발 경험'], 'preferred': [],
        'evidence': [
            {'category': 'role', 'points': 46, 'jd_quote': 'LangGraph 기반 Agent 개발과 운영',
             'profile_id': 'agent', 'reason': '직접 구현 경험'},
            {'category': 'technical', 'points': 23, 'jd_quote': 'Golden Set 평가 자동화 경험',
             'profile_id': 'evaluation', 'reason': '노드 평가 자동화'},
            {'category': 'production', 'points': 13, 'jd_quote': '5년 이상의 실무 경력',
             'profile_id': 'work_experience_months', 'reason': '6년 이상 실무'},
            {'category': 'leadership', 'points': 8, 'jd_quote': 'Python 백엔드 개발 경험',
             'profile_id': 'operations', 'reason': '백엔드 직접 개발'},
        ],
        'strengths': ['Agent 개발과 평가 자동화'], 'gaps': [], 'important_unknowns': [],
        'hard_failures': [], 'hard_failure_quotes': [], 'scope_failure': '', 'scope_quote': '',
        'emphasis_profile_ids': ['agent', 'evaluation'], 'rationale': 'JD와 직접 개발 경험 연결',
    }
