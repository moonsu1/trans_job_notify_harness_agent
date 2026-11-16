import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from career_scout import portals
from career_scout.collectors import CollectionError
from career_scout.hyundai import parse_job, portal


def sk_row(corp='SK telecom', ident='R262206'):
    return {'noticeID': ident, 'corpName': corp, 'title': 'AI Agent Engineer',
            'workingArea': 'Seoul', 'workingType': 'Permanent', 'recruitType': 'Experienced',
            'end': 'November 10, 2026(Tue)'}


def test_sk_full_json_is_not_limited_to_first_visible_cards():
    values = [sk_row(ident=f'R{n:06d}') for n in range(1, 118)]
    rows, total, complete = portals.sk_catalog({'success': True, 'totalCount': 117, 'list': values})
    assert complete and total == len(rows) == 117
    assert not portals.sk_catalog({'success': True, 'totalCount': 118, 'list': values})[2]
    assert not portals.sk_catalog({'success': True, 'totalCount': 2, 'list': [values[0], values[0]]})[2]
    with pytest.raises(CollectionError):
        portals.sk_catalog({'success': False, 'totalCount': 0, 'list': []})


def test_exact_legal_employer_excludes_other_sk_companies():
    company = {'id': 'skhynix'}
    assert portals.sk_matches('SK hynix', company)
    assert not portals.sk_matches('SK hynix system ic', company)
    assert not portals.sk_matches('SK inc.(AX)', company)
    assert not portals.sk_matches('SK telecom', company)


@pytest.mark.parametrize('raw,expected', [('November 10, 2026(Tue)', '2026-11-10'),
                                        ('202611161700', '2026-11-16T17:00:00+09:00'),
                                        ('20261115', '2026-11-15'), ('20261113', '2026-11-13'),
                                        ('202611', ''), ('20261335', ''),
                                        ('2026.11.16', '2026-11-16'), ('bad', '')])
def test_portal_dates_are_real_dates_not_raw_strings(raw, expected):
    assert portals.portal_deadline(raw) == expected


def samsung_html(total=2, pages=1, ids=('23409', '23405')):
    return (f'<input class="divCnt" data-value="{total}" data-max="{pages}">'
            +''.join(f'<li><a data-value="{key}"><p class="company">삼성전자 DS부문</p>'
                     '<h3 class="title">AI Agent 개발</h3><span class="period">2026.11.04 ~ 2026.11.16</span>'
                     '</a></li>' for key in ids))


def test_samsung_missing_count_is_not_zero_jobs():
    with pytest.raises(CollectionError):
        portals.samsung_page('<ul id="list"></ul>')
    rows, total, pages = portals.samsung_page(samsung_html())
    assert len(rows) == total == 2 and pages == 1
    assert rows[0]['company'] == '삼성전자 DS부문'
    assert rows[0]['id'] == '23409'


def test_portal_cache_reuses_catalog_within_run():
    calls = []
    tools = NS()

    async def loader():
        calls.append(True)
        await asyncio.sleep(0)
        return ['public job']

    async def run():
        return await asyncio.gather(*(portals.cached_catalog(tools, 'sk', loader) for _ in range(3)))

    assert asyncio.run(run()) == [['public job']]*3
    assert len(calls) == 1


def test_empty_group_portal_plus_blocked_dedicated_site_stays_partial(config, monkeypatch):
    company = next(c for c in config[2] if c['id'] == 'skhynix')

    async def cached(*args):
        return [sk_row()], 1, True

    async def blocked(url):
        raise CollectionError('robots.txt 접근 제한')

    monkeypatch.setattr(portals, 'cached_catalog', cached)
    result = asyncio.run(portals.collect_sk(NS(settings=config[0], permitted=blocked, fetch=blocked), company))
    assert result.status == 'partial' and not result.jobs
    assert '실제 대상 법인 0건' in result.detail and 'Talent Hub 원문 확인 실패' in result.detail


def test_samsung_filters_ds_dx_and_securities_separately(config, monkeypatch):
    companies = [c for c in config[2] if c.get('adapter') == 'samsungcareers']
    rows = [{'id': '123', 'company': '삼성물산 건설부문', 'title': 'AI Engineer', 'period': ''}]

    async def cached(*args):
        return rows, 1, 1, []

    async def permitted(url):
        return None

    monkeypatch.setattr(portals, 'cached_catalog', cached)
    for company in companies:
        result = asyncio.run(portals.collect_samsung(NS(settings=config[0], permitted=permitted), company))
        assert not result.jobs
        assert result.status == ('partial' if company.get('supplemental_urls') else 'ok')


def test_kia_reuses_flow_without_hyundai_company_or_urls(config):
    company = next(c for c in config[2] if c['id'] == 'kia')
    assert portal(company)['hgrCd'] == 2
    value = {'recuYy': '2026', 'recuType': 'N2', 'recuCls': 17, 'recuNoticeNm': 'AI Agent 개발',
             'workPlaceCodeNm': '서울', 'channelCodeNm': '경력'}
    job = parse_job(value, company)
    assert job.url.startswith('https://career.kia.com/apply/applyView.kc?')
    assert job.company == '기아'
    assert '현대' not in job.url


def test_actual_sk_and_samsung_count_snapshots():
    root = Path(__file__).parent/'fixtures'
    sk = json.loads((root/'sk-public-list.json').read_text(encoding='utf-8'))
    rows, total, complete = portals.sk_catalog(sk)
    assert complete and total == 116
    assert sum(portals.sk_matches(v['corpName'], {'id': 'skt'}) for v in rows) == 5
    rows, total, pages = portals.samsung_page((root/'samsung-public-list.html').read_text(encoding='utf-8'))
    assert len(rows) == total == 6 and pages == 1


@pytest.fixture
def samsung_fire_roles():
    return json.loads((Path(__file__).parent/'fixtures/samsung-fire-23492-roles.json').read_text(encoding='utf-8'))


def test_samsung_multirole_response_keeps_each_jd_and_contract_separate(config, samsung_fire_roles):
    company = next(c for c in config[2] if c['id'] == 'samsung_fire')
    jobs = portals.parse_samsung_roles(samsung_fire_roles,'https://www.samsungcareers.com/hr/?no=23492',company)
    assert len(jobs) == 26 and len({j.key for j in jobs}) == 26
    assert len({j.url for j in jobs}) == 1
    assert all(j.verified and j.employer_verified and j.url_is_catalog for j in jobs)
    assert all(j.parent_external_id == '23492' and j.employment_type == '정규직' for j in jobs)
    assert all('채용 법인: 삼성화재' in j.body for j in jobs)
    assert all(j.location == '서울 서초구' and j.deadline == '2026-11-23T17:00:00+09:00' for j in jobs)
    by_title = {j.title:j for j in jobs}
    assert 'Knowledge Graph/Ontology' in by_title['AI Data Architect'].body
    assert 'LLMOps' not in by_title['AI Data Architect'].body
    assert 'Airflow' in by_title['AI Data Engineer'].body
    assert 'Dify' in by_title['AI Platform Engineer'].body
    assert 'Airflow' not in by_title['AI Platform Engineer'].body
    assert 'M365 Copilot' in by_title['사내 AI 서비스 운영'].body
    assert '글로벌 M&A' in by_title


def test_multirole_storage_preserves_parent_history_without_merging_roles(store, config, samsung_fire_roles):
    from career_scout.core import Job
    from career_scout.reporting import report_rows
    company = next(c for c in config[2] if c['id'] == 'samsung_fire')
    url = 'https://www.samsungcareers.com/hr/?no=23492'
    parent = Job(company_id=company['id'],company=company['name'],tier=3,external_id='23492',
                 url=url,title='통합 경력 공고',body='분리 전 본문',verified=False)
    key = store.upsert(parent)
    store.db.execute('INSERT INTO applications VALUES(?,?,?,?,?)',(key,'2026-11-15','pending','pending','saved'))
    store.db.commit()
    jobs = portals.parse_samsung_roles(samsung_fire_roles,url,company)
    for j in jobs:
        store.upsert(j)
    for j in reversed(jobs):
        store.upsert(j)
    rows = store.rows()
    assert len(rows) == 27
    assert sum(r['superseded_group'] for r in rows) == 1
    assert store.db.execute('SELECT screening FROM applications WHERE job_id=?',(key,)).fetchone()[0] == 'pending'
    parent_row = next(r for r in report_rows(store,config[0]) if r['id'] == key)
    assert parent_row['exclusion'] == '세부 직무별 공고로 분리된 통합 공고'


def test_samsung_role_ids_are_stable_when_role_order_changes(config, samsung_fire_roles):
    company = next(c for c in config[2] if c['id'] == 'samsung_fire')
    url = 'https://www.samsungcareers.com/hr/?no=23492'
    original = {j.title:j.key for j in portals.parse_samsung_roles(samsung_fire_roles,url,company)}
    samsung_fire_roles['data']['items'].reverse()
    assert original == {j.title:j.key for j in portals.parse_samsung_roles(samsung_fire_roles,url,company)}


@pytest.mark.parametrize('fault',['employer','parent_id','role_parent','missing_role_id','duplicate_role_id'])
def test_samsung_role_employer_and_ids_require_real_matching_evidence(config, samsung_fire_roles, fault):
    company = next(c for c in config[2] if c['id'] == 'samsung_fire')
    data = deepcopy(samsung_fire_roles)
    if fault == 'employer':
        data['data']['result']['cmpNameKr'] = '다른 삼성 계열사'
    elif fault == 'parent_id':
        data['data']['result']['seq'] = 999
    elif fault == 'role_parent':
        data['data']['items'][0]['recruitSeq'] = '999'
    elif fault == 'missing_role_id':
        del data['data']['items'][0]['seq']
    else:
        data['data']['items'][1]['seq'] = data['data']['items'][0]['seq']
    with pytest.raises(CollectionError):
        portals.parse_samsung_roles(data,'https://www.samsungcareers.com/hr/?no=23492',company)


def test_samsung_catalog_ingests_roles_but_retains_announcement_count(config, samsung_fire_roles, monkeypatch):
    from career_scout.health import observe_result
    company = next(c for c in config[2] if c['id'] == 'samsung_fire')
    async def catalog(*args):
        return [dict(id='23492',title='통합 경력 공고',company='삼성화재',period='')],1,1,[]
    async def roles(tools,url,employer):
        return portals.parse_samsung_roles(samsung_fire_roles,url,employer)
    monkeypatch.setattr(portals,'cached_catalog',catalog)
    monkeypatch.setattr(portals,'read_samsung_roles',roles)
    result = asyncio.run(portals.collect_samsung(NS(settings=config[0]),company))
    assert result.status == 'ok' and len(result.jobs) == 26
    assert result.proof.observed == 1 and result.proof.company_count == 1
    snapshot = observe_result(result,company,config[0])
    assert snapshot.status == 'ok' and snapshot.jd_required == snapshot.jd_verified == 4


def test_actual_dx_multirole_posting_does_not_become_samsung_research(config):
    from career_scout.core import prefilter
    data = json.loads((Path(__file__).parent/'fixtures/samsung-dx-23440-roles.json').read_text(encoding='utf-8'))
    company = next(c for c in config[2] if c['id'] == 'samsung_research')
    jobs = portals.parse_samsung_roles(data,'https://www.samsungcareers.com/hr/?no=23440',company)
    assert len(jobs) == 14 and all(j.verified for j in jobs)
    assert {j.organization for j in jobs} == {'VD','DA','생산기술연구소','G-CS','G-EHS','한국총괄'}
    assert all('삼성리서치가 아닌' in prefilter(j,config[0]) for j in jobs)


def test_known_sr_organization_is_not_excluded_by_dx_scope_check(config, job):
    from career_scout.core import prefilter
    job.company_id,job.company,job.organization = 'samsung_research','삼성전자 DX부문','SR'
    assert not prefilter(job,config[0])
