import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from protego import Protego

from career_scout import catalogs, portals, reposts
from career_scout.collectors import Collection, CollectionError, PublicTools

FIXTURES = Path(__file__).parent/'fixtures'


def company(config, ident):
    return next(c for c in config[2] if c['id'] == ident)


def test_specific_robots_allow_and_wildcard_are_respected():
    policy = Protego.parse('User-agent: *\nDisallow: /\nAllow: /careers\nAllow: /api/pcsx\n')
    assert policy.can_fetch('https://example.com/careers/job/123', 'CareerScout')
    assert policy.can_fetch('https://example.com/api/pcsx/search', 'CareerScout')
    assert not policy.can_fetch('https://example.com/private', 'CareerScout')
    policy = Protego.parse('User-agent: *\nAllow: /\nDisallow: /app*\n')
    assert not policy.can_fetch('https://example.com/app/jobnotice/view', 'CareerScout')


@pytest.mark.parametrize('code,allowed', [(401, True), (403, True), (404, True), (410, True),
                                         (429, False), (503, False), (999, False)])
def test_robots_unavailable_is_different_from_actual_page_access(code, allowed, config):
    async def main():
        tool = PublicTools(config[0])

        async def raw(method, url, **kwargs):
            raise CollectionError('HTTP '+str(code))

        tool._raw = raw
        try:
            if allowed:
                await tool.permitted('https://example.com/jobs')
                # Actual resource errors still fail. No authorization or HTTP guard is bypassed.
                with pytest.raises(CollectionError, match='HTTP '+str(code)):
                    await tool.fetch('https://example.com/jobs')
            else:
                with pytest.raises(CollectionError):
                    await tool.permitted('https://example.com/jobs')
        finally:
            await tool.close()

    asyncio.run(main())


def test_html_meta_encoding_for_korean_legacy_pages(config):
    async def main():
        tool = PublicTools(config[0])

        async def raw(*args, **kwargs):
            return httpx.Response(200, content='<meta charset="euc-kr">정규직 채용'.encode('cp949'))

        async def permit(url):
            pass

        tool._raw, tool.permitted = raw, permit
        try:
            response = await tool.fetch('https://example.com')
            assert '정규직 채용' in response.text
        finally:
            await tool.close()

    asyncio.run(main())


def test_google_relative_detail_links_use_document_base(config):
    c = company(config, 'google')
    html = ('<base href="https://www.google.com/about/careers/applications/">'
            '1 jobs matched<a href="jobs/results/123-ai-engineer" '
            'aria-label="Learn more about AI Engineer"></a>')
    jobs, total = catalogs.google_page(html, c)
    assert total == len(jobs) == 1
    assert jobs[0].url == 'https://www.google.com/about/careers/applications/jobs/results/123-ai-engineer'
    assert jobs[0].title == 'AI Engineer'
    with pytest.raises(CollectionError):
        catalogs.google_page('<div id="app"></div>', c)


def test_microsoft_employment_array_becomes_valid_string(config):
    async def fetch(url, **kwargs):
        if url.endswith('/search'):
            return httpx.Response(200, json={'status': 200, 'data': {'positions': [
                {'id': 123, 'name': 'AI Agent Engineer', 'positionUrl': '/careers/job/123',
                 'locations': ['Korea, Seoul, Seoul'], 'standardizedLocations': ['Seoul, Seoul, KR']}],
                'count': 1}})
        return httpx.Response(200, json={'status': 200, 'data': {
            'id': 123, 'name': 'AI Agent Engineer', 'efcustomTextEmploymentType': ['Full-Time'],
            'jobDescription': 'Responsibilities AI Agent 개발. '*50}})

    result = asyncio.run(catalogs.microsoft(NS(fetch=fetch), company(config, 'microsoft')))
    assert result.status == 'ok'
    assert result.jobs[0].employment_type == 'Full-Time'
    assert isinstance(json.loads(result.jobs[0].model_dump_json())['employment_type'], str)


def test_amazon_wrong_country_does_not_become_korean_job(config):
    async def fetch(url, **kwargs):
        assert kwargs['params']['normalized_country_code[]'] == 'KOR'
        return httpx.Response(200, json={'error': None, 'hits': 1, 'jobs': [{'country_code': 'USA'}]})

    with pytest.raises(CollectionError, match='한국 필터'):
        asyncio.run(catalogs.amazon(NS(fetch=fetch), company(config, 'aws')))


def test_nh_snapshot_uses_exact_bank_not_group_or_local_coop(config):
    c = company(config, 'nh_bank')
    html = (FIXTURES/'nh-bank-list.html').read_text(encoding='utf-8')
    jobs, total = catalogs.nh_page(html, c)
    assert total == 134 and len(jobs) == 10
    assert all(j.company == 'NH농협은행' for j in jobs)
    assert jobs[0].deadline == '2026-11-10'
    with pytest.raises(CollectionError, match='실제 법인 불일치'):
        catalogs.nh_page(html.replace('<td>NH농협은행</td>', '<td>지역농협</td>'), c)
    with pytest.raises(CollectionError, match='필터 적용'):
        catalogs.nh_page(html.replace('badgeSearch_1', 'badgeSearch_C'), c)


def test_nh_repeated_page_is_not_complete(config):
    html = (FIXTURES/'nh-bank-list.html').read_text(encoding='utf-8')

    async def fetch(url, **kwargs):
        return httpx.Response(200, text=html)

    with pytest.raises(CollectionError, match='페이지 중복'):
        asyncio.run(catalogs.nonghyup(NS(fetch=fetch, settings=config[0]), company(config, 'nh_bank')))


def test_xml_zero_has_schema_but_a_blank_error_page_does_not():
    assert catalogs.xsheet((FIXTURES/'kyobo-zero.html').read_text(encoding='utf-8')) == []
    with pytest.raises(CollectionError):
        catalogs.xsheet('<html><body></body></html>')
    with pytest.raises(CollectionError):
        catalogs.xsheet('<SHEET><DATA><HTR><TD>TITLE</TD></HTR></DATA><MESSAGE>Error</MESSAGE></SHEET>')


def test_wanted_company_catalog_distinguishes_empty_from_missing():
    props = {'dehydrateState': {'queries': [{'queryKey': ['companyHire', '12'],
                                           'state': {'status': 'success', 'data': {'jobs': []}}}]}}
    assert reposts.company_hires(props, 12) == []
    with pytest.raises(CollectionError):
        reposts.company_hires(props, 13)
    with pytest.raises(CollectionError):
        reposts.company_hires({}, 12)


def test_wanted_client_mention_does_not_match_outsourcing_employer(config):
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json={'data': {'companies': [
            {'id': 1, 'name': '신한은행 협력업체 인력파견'}]}})

    result = asyncio.run(reposts.supplement(NS(fetch=fetch, settings=config[0]),
                                          company(config, 'shinhan'), Collection('shinhan', status='failed')))
    assert not result.jobs and result.status == 'failed'
    assert len(calls) == 1


def test_wanted_regulatory_contract_rule_applies_before_detail(config):
    c = company(config, 'shinhan')
    props = {'dehydrateState': {'queries': [{'queryKey': ['companyHire', '1'], 'state': {
        'status': 'success', 'data': {'jobs': [{'id': 999, 'name': 'AI Agent Engineer', 'hidden': False,
                                             'status': 'active', 'employment_type': 'contract',
                                             'address': {'location': '서울'}}]}}}]}}

    async def fetch(url, **kwargs):
        if url.endswith('/search'):
            return httpx.Response(200, json={'data': {'companies': [{'id': 1, 'name': c['name']}]}})
        if '/api/v4/companies/' in url:
            return httpx.Response(200, json={'company': {'id': 1, 'name': c['name']}})
        assert '/wd/' not in url
        return httpx.Response(200, text='<script id="__NEXT_DATA__">'+json.dumps({'props': {'pageProps': props}})+'</script>')

    result = asyncio.run(reposts.supplement(NS(fetch=fetch, settings=config[0]), c, Collection(c['id'], status='failed')))
    assert len(result.jobs) == 1 and result.jobs[0].employment_type == '계약직'
    assert not result.jobs[0].verified


def test_hynix_hub_explicit_current_empty_plus_full_group_catalog(config, monkeypatch):
    async def cached(*args):
        return [], 0, True

    async def fetch(url):
        return httpx.Response(200, text=(FIXTURES/'hynix-current.html').read_text(encoding='utf-8'))

    monkeypatch.setattr(portals, 'cached_catalog', cached)
    result = asyncio.run(portals.collect_sk(NS(fetch=fetch, settings=config[0]), company(config, 'skhynix')))
    assert result.status == 'ok' and not result.jobs
    assert '현재 모집 탭' in result.detail


def test_new_hynix_hub_layout_cannot_be_treated_as_zero(config, monkeypatch):
    async def cached(*args):
        return [], 0, True

    async def fetch(url):
        return httpx.Response(200, text='<main>new unknown layout</main>')

    monkeypatch.setattr(portals, 'cached_catalog', cached)
    result = asyncio.run(portals.collect_sk(NS(fetch=fetch, settings=config[0]), company(config, 'skhynix')))
    assert result.status == 'partial'


def test_sr_korea_links_back_to_existing_catalog_not_overseas_workday(config):
    async def fetch(url):
        return httpx.Response(200, text='<li class="careers__listBox">SR Korea(HQ)'
                              '<span class="careers__webSite"><a href="https://www.samsungcareers.com/">Click</a>'
                              '</span></li><a href="https://sec.wd3.myworkdayjobs.com/">Overseas</a>')

    assert asyncio.run(portals.supplemental_notes(NS(fetch=fetch), company(config, 'samsung_research'))) == []


def test_reviewed_image_requires_same_bytes_and_legal_employer(tmp_path, monkeypatch):
    import hashlib
    import yaml
    root = tmp_path/'config'
    root.mkdir()
    content = b'reviewed-public-job-image'
    roles = [{'id': 'agent', 'title': 'AI Agent Engineer'}]
    (root/'reviewed_images.yaml').write_text(yaml.safe_dump({'reviews': [
        {'company_id': 'meritz_fire', 'sha256': hashlib.sha256(content).hexdigest(), 'roles': roles}
    ]}), encoding='utf-8')
    monkeypatch.setattr(catalogs, 'ROOT', tmp_path)
    assert catalogs.reviewed_roles(content, {'id': 'meritz_fire'}) == roles
    assert not catalogs.reviewed_roles(content+b'changed', {'id': 'meritz_fire'})
    assert not catalogs.reviewed_roles(content, {'id': 'another_employer'})


@pytest.mark.parametrize('title', ['인재POOL 지원서 등록', '입사확정자 지원서 작성'])
def test_non_vacancy_registration_does_not_become_recommendation(config, job, title):
    from career_scout.core import prefilter
    job.title = title
    assert '실제 모집 직무' in prefilter(job, config[0])
