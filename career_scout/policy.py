"""Role scope is a prerequisite, not a compensable skill-score component."""
from __future__ import annotations

import re


def finance_employment(job, settings, assessment=None):
    category = settings.get('_company_metadata', {}).get(job.company_id, {}).get('finance_category')
    if not settings.get('finance_permanent_only') or not category:
        return 'not_applicable', ''
    a = assessment or {}
    declared = ' '.join([job.title, job.employment_type, a.get('employment_type', '')])
    # Employment clauses only: mentions of customer contracts elsewhere in the JD
    # must not change the hiring arrangement.
    clauses = [m.group(0) for m in re.finditer(
        r'(?:고용\s*(?:형태|구분)|채용\s*(?:형태|구분|조건)|근무\s*형태|계약\s*기간|근무\s*조건|신분)[\s:：]*.{0,100}', job.body)]
    evidence = declared+' '+' '.join(clauses)
    negative = (r'전문\s*계약직|기간제|무기\s*계약직|별정직|위촉직|프리랜서|파트타이머|파트타임|단시간|'
                r'비정규직|준정규직|non[-\s]*(?:permanent|regular)|'
                r'계약직|인턴|파견|(?:정규직\s*전환|전환\s*형)|\bcontract\b|temporary|internship')
    if re.search(negative, evidence, re.I):
        return 'excluded', '금융사는 정규직만: 전문계약직·계약직·전환형·인턴 제외'
    if re.search(r'정규직|\bpermanent\b|\bregular\s*(?:employee|employment)\b', evidence, re.I):
        return 'confirmed', ''
    return 'unknown', '금융사 정규직 고용형태 확인 전 추천 보류'


ROLE_LABELS = {'ai_engineer': 'AI Engineer / Agent·LLM 개발',
               'ai_planning': 'AI 제품·서비스 기획', 'ai_pm_pl': 'AI 프로젝트 PM·PL',
               'other': '희망 직무 범위 밖', 'unclear': '직무 확인 필요'}
AI_PRODUCT = re.compile(r'(?<![a-z])LLM(?![a-z])|(?<![a-z])RAG(?![a-z])|\bagent\b|agentic|text.?to.?sql|text2sql|'
                        r'온톨로지|ontology|생성형|AI\s*(플랫폼|에이전트|서비스|제품|모델|검색|프로젝트)|'
                        r'AI\s*(platform|application|service|product|solution)|large language', re.I)
AGENT_PRODUCT = re.compile(r'(?<![a-z])agent(?![a-z])|에이전트|agentic|langgraph|'
                           r'agent[\s_-]*(runtime|workflow|orchestrat|platform)|LLM\s*(router|API)|prompt\s*studio|'
                           r'(?<![a-z])RAG(?![a-z])|retrieval.augmented|text.?to.?sql|text2sql|'
                           r'온톨로지|ontology|tool\s*(calling|use)|function\s*calling', re.I)


def agent_focus_failure(title, duty_quotes):
    """An AI label or generic LLM serving alone does not establish Agent product work."""
    rejected = role_exclusion(title)
    if rejected:
        return rejected
    # A collected link's hint can be stale. A clear role title in the quoted JD
    # (e.g. "AI 플랫폼 엔지니어 (Serving)") takes precedence over that hint.
    heading = duty_quotes[0] if duty_quotes else ''
    quoted_role = role_exclusion(heading[:120]) if re.match(
        r'^(?:AI\s*플랫폼\s*엔지니어|AI\s*Platform\s*Engineer|MLOps\s*Engineer|'
        r'LLMOps\s*Engineer|Data\s*(?:Analyst|Engineer))', heading, re.I) else ''
    if quoted_role:
        return quoted_role
    duties = ' '.join(duty_quotes)
    focus = AGENT_PRODUCT.search(duties) or (AGENT_PRODUCT.search(title) and re.search(
        r'서비스|제품|기획|답변|workflow|application|product', duties, re.I))
    if not focus:
        return 'Agent·RAG·Text2SQL·온톨로지 제품의 직접 개발·기획을 주업무 근거에서 확인하지 못함'
    return ''


def role_exclusion(title: str) -> str:
    rules = [
        (r'\bmlops\b|\bllmops\b|\bml\s*(인프라|infrastructure)\b|머신러닝\s*인프라|'
         r'machine\s*learning\s*(platform|infrastructure)|'
         r'(?:AI\s*플랫폼\s*엔지니어|AI\s*Platform\s*Engineer)\s*\((?:Serving|Inference|Platform|Recommendation|Retrieval)',
         'MLOps·모델 서빙·학습 인프라 중심 직무: Agent 제품 개발·기획과 구분해 제외'),
        (r'data\s*(analyst|analytics|scientist|engineer)|analytics\s*engineer|'
         r'데이터\s*(분석|애널리스트|사이언티스트|엔지니어)|데이터\s*품질|데이터\s*카탈로그',
         '데이터 분석·데이터 엔지니어링 중심 직무: 현재 희망하는 Agent 직무 범위 밖'),
        (r'^(?!.*(?:agent|에이전트|\bRAG\b|Text2SQL|Ontology|온톨로지)).*'
         r'(?:machine\s*learning\s*engineer|ML\s*엔지니어)',
         '일반 ML·추천·광고 모델 중심 직무: 현재는 Agent·LLM 제품 개발·기획에 집중'),
        (r'콘텐츠\s*에디터|content\s*(editor|producer)|카피라이터|copywriter|포토그래퍼|그래픽\s*디자이너',
         '콘텐츠·디자인 제작 직무: AI 도구 활용은 AI 서비스 개발·기획 경력과 다름'),
        (r'카탈로그\s*품질\s*검수|카탈로그\s*운영\s*및\s*프로세스|catalog\s*(quality|operation|inspection)',
         '카탈로그 검수·운영 직무: 데이터 품질 운영은 Agent·LLM 개발·AI 기획 직무가 아님'),
        (r'오퍼레이션\s*프로세스\s*자동화|밀크런\s*운송|물류\s*운영\s*담당',
         '운영·일반 업무 자동화 직무: SQL·Python·RPA·MCP 공통 기술만으로 추천하지 않음'),
    ]
    for pattern, reason in rules:
        if re.search(pattern, title, re.I):
            return reason
    return ''


def experience_check(requirement: str, domain: str, actual_months: int):
    """Employment months are an upper bound, not proof of every domain's tenure."""
    text = requirement or ''
    lower, upper = None, None
    patterns = [r'(\d+)\s*년\s*(?:이상|이상의)', r'(\d+)\s*\+\s*years?',
                r'(?:at least|minimum(?: of)?)\s*(\d+)\s*years?']
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            lower = int(match[1])*12
            break
    interval = re.search(r'(\d+)\s*(?:~|〜|–|to|-)\s*(\d+)\s*(?:년|years?)', text, re.I)
    if interval:
        lower, upper = int(interval[1])*12, int(interval[2])*12
    maximum = re.search(r'(\d+)\s*년\s*(미만|이하)', text)
    if maximum:
        upper = int(maximum[1])*12 - (1 if maximum[2] == '미만' else 0)
    if lower is not None and lower > actual_months:
        return 'fail', f'필수 최소 {lower}개월 > 전체 실무 {actual_months}개월; 석사 기간 별도'
    if upper is not None and domain == 'total' and actual_months > upper:
        return 'fail', f'필수 경력 상한 {upper}개월 < 전체 실무 {actual_months}개월'
    if (lower is not None or upper is not None) and domain != 'total':
        return 'unknown', f'{domain} 분야의 요구 경력은 전체 실무 {actual_months}개월만으로 충족을 단정할 수 없음'
    return 'ok', ''
