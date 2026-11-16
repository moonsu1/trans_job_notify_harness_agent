import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import httpx
import pytest

from career_scout import jobflex
from career_scout.browser_read import jobflex_request, public_read_post
from career_scout.collectors import CollectionError, allowed_url
from career_scout.core import Job, prefilter
from career_scout.legacy_finance import incruit_content


@pytest.mark.parametrize('method,allowed', [('search01', True), ('getTotCnt', True),
                                         ('insert01', False), ('delete01', False), ('save', False)])
def test_public_browser_cannot_submit_an_application(method, allowed):
    request = NS(url='https://recruit.hmsec.com/Application.do',
                 post_data='S_DSCLASS=biz.rem.apply.recruit.Apply_list&S_DSMETHOD='+method)
    assert public_read_post(request) is allowed
    request.post_data = 'S_DSCLASS=biz.rem.apply.recruit.Apply_write&S_DSMETHOD='+method
    assert not public_read_post(request)


def test_public_browser_menu_is_read_only():
    request = NS(url='https://recruit.hmsec.com/reMenuAction.do',
                 post_data='X_POP_URL=/rem/apply/recruit/apply_view.jsp')
    assert public_read_post(request)
    request.post_data = 'X_POP_URL=/rem/apply/recruit/apply_write.jsp'
    assert not public_read_post(request)


def test_jobflex_browser_request_cannot_call_auth_or_write_routes():
    for path in ['/login', '/position/v2/jobflex/12/apply', '/resume/save', '/position/v2/jobflex/../login']:
        with pytest.raises(CollectionError):
            asyncio.run(jobflex_request(NS(), path))


@pytest.mark.parametrize('data', [
    {'pagination': {'page': 1, 'totalPages': 1, 'totalCount': -1}, 'list': []},
    {'pagination': {'page': 1, 'totalPages': 1, 'totalCount': 2}, 'list': [{'positionSn': 4}, {'positionSn': 4}]},
    {'pagination': {'page': 1, 'totalPages': 1, 'totalCount': 1}, 'list': [{'title': 'AI Engineer'}]},
    {'pagination': {}, 'list': []},
])
def test_jobflex_failed_or_duplicate_catalog_is_not_zero_jobs(data):
    with pytest.raises(CollectionError):
        jobflex.parse_page(data)


def test_detail_title_must_match_the_catalog(job):
    with pytest.raises(CollectionError):
        jobflex.apply_detail(job, {'title': '다른 직무', 'jobDescription': '<p>JD</p>'})


def test_incruit_image_transcript_contains_the_actual_contract(config):
    c = next(c for c in config[2] if c['id'] == 'shinhan_card')
    transcript = ('2026년 신한카드 AI Engineer 채용 접수기간 : 2026년 11월 9일 ~ 11월 16일. '
                  '채용신분 정규직. 담당업무 AI Agent 설계 및 개발. 필수 자격 LLM 개발 경험. ')*8
    job = incruit_content('<img alt="'+transcript+'">', c, c['url'])
    assert job.verified and job.deadline == '2026-11-16'
    assert '채용신분 정규직' in job.body
    with pytest.raises(CollectionError):
        incruit_content('<img alt="다른 회사 공고">', c, c['url'])


def test_prearranged_registration_form_is_not_a_job(job, config):
    job.title = '경력직 채용 입사지원서 작성'
    job.body = '인사부 및 현업부서와 사전 협의된 경우 해당 공고에 입사지원서 작성 부탁드립니다.'
    assert '사전 협의자 전용' in prefilter(job, config[0])


def test_same_post_only_catalog_keeps_different_posting_ids(store, job):
    first = job.model_copy(update={'url_is_catalog': True, 'external_id': 'official-1'})
    second = first.model_copy(update={'external_id': 'official-2', 'title': '다른 실제 공고'})
    assert store.upsert(first) != store.upsert(second)
    assert len(store.rows()) == 2
    assert store.upsert(first) == first.key
    assert len(store.rows()) == 2


def test_changed_reviewed_image_is_never_treated_as_verified(job, config):
    async def run():
        async def raw(*args, **kwargs):
            return httpx.Response(200, content=b'newly changed image', headers={'content-type': 'image/png'})
        c = next(c for c in config[2] if c['id'] == 'hana_securities')
        candidate = Job(company_id=c['id'], company=c['name'], tier=3, title='AI 아키텍트',
                        url=c['url'], body='', verified=False)
        await jobflex.read_reviewed_image(NS(_raw=raw), candidate,
                                         {'jobDescription': '<img src="/upload/new-image.png">'}, c)
        assert not candidate.verified and not candidate.employment_type
    asyncio.run(run())


@pytest.mark.parametrize('group_status,expected', [('POST_SUBMISSION', 'ok'), ('IN_SUBMISSION', 'partial')])
def test_shared_group_portal_does_not_relabel_other_companies(monkeypatch, config, group_status, expected):
    c = next(c for c in config[2] if c['id'] == 'db_insurance')

    @asynccontextmanager
    async def page(*args, **kwargs):
        yield NS(url=c['url'])

    async def request(page, path, payload=None):
        if path.startswith('/design/'):
            return {'title': 'DB그룹'}
        return {'pagination': {'page': 1, 'totalCount': 3, 'totalPages': 1}, 'list': [
            {'positionSn': 1, 'title': '[DB글로벌칩] 엔지니어', 'submissionStatus': 'POST_SUBMISSION'},
            {'positionSn': 2, 'title': '[DB손해보험] 사무지원직', 'submissionStatus': 'POST_SUBMISSION'},
            {'positionSn': 3, 'title': '[DB그룹] 공동 채용', 'submissionStatus': group_status}]}

    monkeypatch.setattr(jobflex, 'public_page', page)
    monkeypatch.setattr(jobflex, 'jobflex_request', request)
    result = asyncio.run(jobflex.collect(NS(settings=config[0]), c))
    assert result.status == expected
    assert len(result.jobs) == 1
    assert result.jobs[0].title == '[DB손해보험] 사무지원직'


def test_shared_greenhouse_domain_is_bound_to_the_actual_board(config):
    c = next(c for c in config[2] if c['id'] == 'krafton')
    assert allowed_url('https://job-boards.greenhouse.io/krafton/jobs/123', c)
    assert not allowed_url('https://job-boards.greenhouse.io/officeofthestateauditor/jobs/123', c)
    assert not allowed_url('https://boards-api.greenhouse.io/v1/boards/other/jobs', c)
    wrong = Job(company_id='krafton', company='크래프톤', tier=3, title='Senior AI Engineer',
                url='https://job-boards.greenhouse.io/officeofthestateauditor/jobs/123', verified=True)
    assert '다른 기업 공고' in prefilter(wrong, config[0])
