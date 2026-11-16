from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo('Asia/Seoul')
POLICY_VERSION = '2026-11-09-agent-focus-v5'


def now() -> datetime:
    return datetime.now(KST)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cache_fingerprint(settings, profile, policy_version=None):
    # Spending limits, schedule and discovery switches do not change the meaning
    # of an already-validated role assessment. Avoid charging for that work again.
    semantic = {key: settings.get(key) for key in
                ('model', 'excluded_employers', 'target_total_compensation_krw')}
    return digest([semantic, profile, policy_version or POLICY_VERSION])


def load_config(root: Path = ROOT) -> tuple[dict, dict, list[dict]]:
    def read(name):
        return yaml.safe_load((root / 'config' / name).read_text(encoding='utf-8'))
    settings, profile, sources = read('settings.yaml'), read('profile.yaml'), read('sources.yaml')
    financial_path = root/'config'/'financial_companies.yaml'
    if financial_path.exists():
        sources['companies'].extend(yaml.safe_load(financial_path.read_text(encoding='utf-8'))['companies'])
    sources['companies'] = [c for c in sources['companies']
                           if c['id'] not in settings.get('excluded_company_ids', [])
                           and not any(re.search(p, c['name']) for p in settings.get('excluded_company_patterns', []))]
    ids = [c['id'] for c in sources['companies']]
    if len(set(ids)) != len(ids):
        raise ValueError('기업 설정의 ID가 중복되었습니다')
    settings['_company_metadata'] = {c['id']: {'name': c['name'], 'tier': c['tier'],
                                              'greenhouse_board': c.get('board', '') if c.get('adapter') == 'greenhouse' else '',
                                              'finance_category': c.get('finance_category', '')}
                                     for c in sources['companies']}
    start = date.fromisoformat(profile['employment_start'])
    today = now().date()
    profile['work_experience_months'] = max(0, (today.year-start.year)*12+today.month-start.month
                                            - (today.day < start.day))
    settings['fingerprint'] = cache_fingerprint(settings, profile)
    return settings, profile, sources['companies']


def data_dir() -> Path:
    base = os.environ.get('CAREER_SCOUT_DATA_DIR')
    p = Path(base) if base else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CareerScout'
    p.mkdir(parents=True, exist_ok=True)
    return p


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith(('utm_', 'fbclid', 'gclid'))
             and k.lower() not in ('lang', 'language', 'locale')]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/',
                       urlencode(sorted(query)), ''))


def plain(value: str) -> str:
    return re.sub(r'\s+', ' ', value or '').strip()


class Job(BaseModel):
    company_id: str
    company: str
    tier: int
    url: str
    url_is_catalog: bool = False
    source_url: str = ''
    source_kind: str = 'official'
    employer_verified: bool = False
    external_id: str = ''
    parent_external_id: str = ''
    title: str
    organization: str = ''
    location: str = ''
    employment_type: str = ''
    experience: str = ''
    deadline: str = ''
    compensation: str = ''
    body: str = ''
    source_note: str = ''
    verified: bool = False
    status: str = 'unknown'
    checked_at: str = Field(default_factory=lambda: now().isoformat())

    @property
    def key(self) -> str:
        return digest([self.company_id, self.external_id or canonical_url(self.url)])[:24]

    @property
    def content_hash(self) -> str:
        return digest([self.title, self.organization, self.location, self.employment_type,
                       self.experience, self.deadline, self.compensation, self.body,
                       self.verified, self.status, self.source_kind, self.employer_verified])


class Evidence(BaseModel):
    category: str = Field(description='role, technical, production, leadership 중 하나; 연차는 별도 필수조건')
    points: int
    jd_quote: str = Field(description='JD 원문에서 그대로 인용한 짧은 문구')
    profile_id: str = Field(description='profile experience id 또는 education 또는 work_experience_months')
    reason: str


class RoleScope(BaseModel):
    decision: Literal['target', 'out_of_scope', 'unclear']
    family: Literal['ai_engineer', 'ai_planning', 'ai_pm_pl', 'other', 'unclear']
    responsibility: Literal['build_ai_service', 'plan_ai_product', 'lead_ai_delivery', 'use_ai_tools', 'non_ai', 'unknown']
    duty_quotes: list[str]
    reason: str


class Assessment(BaseModel):
    role_scope: RoleScope
    employer: str
    organization: str
    location: str
    employment_type: str
    experience_requirement: str
    experience_domain: Literal['total', 'llm_agent', 'ai_ml', 'ai_planning', 'content', 'data_operations', 'security', 'other', 'unspecified']
    compensation_quote: str
    duties: list[str]
    required: list[str]
    preferred: list[str]
    evidence: list[Evidence]
    strengths: list[str]
    gaps: list[str]
    important_unknowns: list[str]
    hard_failures: list[str]
    hard_failure_quotes: list[str]
    scope_failure: str = Field(description='기업/조직/지역/고용형태가 설정 범위 밖인 명확한 근거; 없으면 빈 문자열')
    scope_quote: str
    emphasis_profile_ids: list[str]
    rationale: str


def assessment_view(a: Assessment | dict | None, verified: bool) -> dict:
    if not a:
        return {'score': None, 'grade': '판단 보류', 'recommended': False,
                'probability': '산정 불가 — 실제 지원 결과 데이터 부족'}
    d = a.model_dump() if isinstance(a, Assessment) else a
    role = d.get('role_scope', {})
    if role.get('decision') != 'target':
        return {'score': None, 'grade': '직무 범위 밖' if role.get('decision') == 'out_of_scope' else '직무 확인 필요',
                'recommended': False, 'probability': '산정 불가 — 실제 지원 결과 데이터 부족'}
    score = sum(e['points'] for e in d['evidence'])
    fail = bool(d['hard_failures'] or d['scope_failure'])
    if fail:
        grade = '낮음'
    elif not verified or d['important_unknowns']:
        grade = '판단 보류'
    elif score >= 80:
        grade = '높음'
    elif score >= 60:
        grade = '중간'
    else:
        grade = '낮음'
    return {'score': score, 'grade': grade, 'recommended': not fail and grade in ('높음', '중간'),
            'probability': '산정 불가 — 실제 지원 결과 데이터 부족'}


def prefilter(job: Job, settings: dict) -> str:
    """Only exclude from explicit structured fields or clearly unsuitable titles."""
    if job.company_id in settings.get('excluded_company_ids', []):
        return '사용자가 제외한 기업'
    if any(re.search(pattern, job.company) for pattern in settings.get('excluded_company_patterns', [])):
        return '사용자가 제외한 기업·계열'
    if settings.get('_company_metadata') and job.company_id not in settings['_company_metadata']:
        return '현재 탐색 대상에서 제외된 기업'
    if job.company_id == 'samsung_research' and job.organization.strip().casefold() in (
            'vd', 'da', 'mx', 'g-cs', 'g-ehs', '생산기술연구소', '한국총괄'):
        return '삼성리서치가 아닌 명시된 삼성전자 DX 조직: '+job.organization
    if job.company_id == 'kb' and urlsplit(job.url).hostname == 'kbgoodjob.kbstar.com':
        return 'KB굿잡은 제3자 채용 플랫폼이며 KB국민은행 자체 채용이 아님'
    if urlsplit(job.url).hostname in ('job-boards.greenhouse.io', 'boards.greenhouse.io'):
        board = settings.get('_company_metadata', {}).get(job.company_id, {}).get('greenhouse_board')
        verified_route = canonical_url(job.url) in settings.get('_verified_source_urls', {}).get(job.company_id, ())
        if not verified_route and (not board or urlsplit(job.url).path.strip('/').split('/')[0] != board):
            return '공유 채용 플랫폼의 실제 회사 게시판 불일치: 다른 기업 공고'
    if job.status == 'closed':
        return '마감 확인'
    from .policy import role_exclusion
    excluded_role = role_exclusion(job.title)
    if excluded_role:
        return excluded_role
    if job.employer_verified:
        from .collectors import employer_matches
        if not employer_matches(job.company, {'id': job.company_id, 'name': job.company}):
            return '실제 채용 법인이 탐색 대상 기업과 다름'
    if job.deadline:
        try:
            deadline = datetime.fromisoformat(job.deadline.replace('Z', '+00:00'))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=KST)
            if len(job.deadline) == 10:
                if deadline.date() < now().date():
                    return '접수 마감일 경과'
            elif deadline < now():
                return '접수 마감시각 경과'
        except ValueError:
            pass
    employer = plain(job.company).casefold()
    if any(plain(e).casefold() in employer for e in settings['excluded_employers']):
        return 'SI·컨설팅 등 제외 기업'
    title = job.title.casefold()
    if re.search(r'인재\s*(?:풀|pool|db)|talent\s*pool|입사\s*확정자', title, re.I):
        return '인재풀·입사확정자 등록은 현재 지원할 실제 모집 직무가 아님'
    if re.search(r'인사부.{0,40}사전\s*협의된\s*경우.{0,35}입사지원서\s*작성', job.body):
        return '사전 협의자 전용 입사지원서 등록 페이지: 실제 공개 모집 직무가 아님'
    if re.search(r'신입\s*(전용|사원)|intern(ship)?|인턴|계약직|파견직|산업기능요원|전문연구요원|\bcontract\b|temporary|part.time', title):
        return '정규직 경력 채용 범위 밖'
    if job.experience.strip().casefold() in ('신입', 'entry-level', 'entry level'):
        return '경력 채용 범위 밖'
    if job.employment_type and re.search(r'intern|contract|temporary|part.time|계약|인턴|파견',
                                         job.employment_type, re.I):
        return '정규직 경력 채용 범위 밖'
    from .policy import finance_employment
    employment, reason = finance_employment(job, settings)
    if employment == 'excluded':
        return reason
    scope = location_scope(job.location, job.tier)
    if scope == 'outside':
        return '국외 근무지' if job.tier == 1 else '서울·경기 외 근무지'
    return ''


def location_scope(location: str, tier: int) -> str:
    loc = location.casefold().strip()
    if loc:
        korea = bool(re.search(r'korea|서울|경기|seoul|pangyo|판교|분당|수원|용인|화성|이천|평택|기흥|성남|청주|천안|온양|한국|대한민국|전국|부산|대전|광주|대구|울산|인천|충청|충북|충남|경북|경남|전남|전북|강원|제주', loc))
        metro = bool(re.search(r'서울|경기|seoul|gyeonggi|pangyo|판교|분당|수원|용인|화성|이천|평택|기흥|성남|남양|의왕|광명|과천|양재|마북', loc))
        # Multiple locations are allowed if at least one eligible Korean location is explicit.
        country_only = loc in ('kr', 'kor', 'korea', 'south korea', 'republic of korea', '대한민국', '한국', '전국')
        if country_only:
            return 'eligible' if tier == 1 else 'unknown'
        unknown = bool(re.search(r'미정|협의|미기재|추후|multiple|various|remote|global|tbd', loc))
        if tier == 1:
            return 'eligible' if korea else 'unknown' if unknown else 'outside'
        if not metro:
            if korea or not unknown:
                return 'outside'
        else:
            return 'eligible'
    return 'unknown'


def role_relevant(job: Job, settings: dict) -> bool:
    from .policy import role_exclusion
    if role_exclusion(job.title):
        return False
    title = plain(job.title).casefold()
    for keyword in settings['role_keywords']:
        if re.search(r'[a-z]', keyword, re.I):
            if re.search(r'(?<![a-z])'+re.escape(keyword.casefold())+r'(?![a-z])', title):
                return True
        elif keyword in title:
            return True
    if re.search(r'\b(ai|ml|nlp)\b|검색\s*(개발|엔진)|지식\s*그래프', title, re.I):
        return True
    # Company boilerplate mentioning AI/ML must not turn every operations role into a match.
    return bool(re.search(r'\b(llm|rag|langgraph|langchain|text2sql|graphrag|ontology)\b|'
                          r'생성형\s*AI|온톨로지|retrieval.augmented|large language model', job.body, re.I))
