import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from career_scout import hyundai
from career_scout.core import location_scope
from career_scout.collectors import CollectionError


@pytest.fixture
def company(config):
    return next(c for c in config[2] if c['id'] == 'hyundai')


def row(number):
    return {'recuYy': '2026', 'recuType': 'N2', 'recuCls': number,
            'recuNoticeNm': f'AI Agent Engineer {number}', 'workPlaceCodeNm': '양재본사',
            'channelCodeNm': '경력', 'applyEndDt': '20991130', 'applyEndTm': '1700', 'groupYn': 'N'}


def full_detail(value):
    return dict(value, recuNoticeSecretYn='N', applyStartYn='Y',
                privJdDtl='AI Agent의 Tool Calling과 RAG 서비스를 설계·개발합니다. ' * 5,
                privMustReq='Agent 개발 실무 경험 및 Python 개발 경험을 요구합니다.')


def test_actual_public_detail_fixture_and_no_invented_contract_type(company):
    payload = json.loads((Path(__file__).parent/'fixtures'/'hyundai-public-detail.json').read_text(encoding='utf-8'))
    job = hyundai.parse_job(payload, company)
    assert job.verified and job.employer_verified
    assert 'ERP' in job.body and '지원자격' in job.body
    assert job.deadline == '2026-11-12T17:00:00+09:00'
    assert job.employment_type == ''  # The public field only states "경력".
    assert location_scope(job.location, 2) == 'eligible'
    assert 'nfGubnC' not in job.url
    assert job.external_id == '2026-N2-379'


def test_visibility_check_precedes_detail_and_blocks_private(company, monkeypatch):
    calls = []

    async def fake_get(page, endpoint, params):
        calls.append(endpoint)
        return {'applySecret': {'recuNoticeSecretYn': 'Y'}}

    monkeypatch.setattr(hyundai, 'get', fake_get)
    with pytest.raises(CollectionError, match='비공개'):
        asyncio.run(hyundai.public_detail(None, row(1), company))
    assert calls == [hyundai.VISIBILITY_API]


def test_identity_mismatch_never_attaches_another_jobs_jd(company, monkeypatch):
    async def fake_get(page, endpoint, params):
        if endpoint == hyundai.VISIBILITY_API:
            return {'applySecret': {'recuNoticeSecretYn': 'N'}}
        return {'applyInfo': full_detail(row(999))}

    monkeypatch.setattr(hyundai, 'get', fake_get)
    with pytest.raises(CollectionError, match='식별자 불일치'):
        asyncio.run(hyundai.public_detail(None, row(1), company))


@pytest.mark.parametrize('failure', ['', 'duplicate_page', 'empty_page', 'network', 'count_change'])
def test_pagination_and_missing_data_are_not_reported_as_success(company, config, monkeypatch, failure):
    rows = [row(n) for n in range(1, 13)]
    rows[0]['recuNoticeNm'] = '[계약직] 운영 지원'
    rows[0]['channelCodeNm'] = '계약직'
    details = []

    @asynccontextmanager
    async def fake_session(tools, company):
        yield None, {'listCnt': 12, 'list': rows[:10]}

    async def fake_get(page, endpoint, params):
        if endpoint == hyundai.LIST_API:
            assert params['page'] == 2
            if failure == 'network':
                raise TimeoutError
            return {'listCnt': 13 if failure == 'count_change' else 12,
                    'list': rows[:10] if failure == 'duplicate_page' else [] if failure == 'empty_page' else rows[10:]}
        if endpoint == hyundai.VISIBILITY_API:
            return {'applySecret': {'recuNoticeSecretYn': 'N'}}
        number = int(params['recuCls'])
        details.append(number)
        return {'applyInfo': full_detail(row(number))}

    monkeypatch.setattr(hyundai, 'session', fake_session)
    monkeypatch.setattr(hyundai, 'get', fake_get)
    result = asyncio.run(hyundai.collect(NS(settings=config[0]), company))
    if failure:
        assert result.status == 'partial'
    else:
        assert result.status == 'ok'
        assert len(result.jobs) == 12 and len(details) == 11
        assert '12/12' in result.detail and '2페이지' in result.detail
    assert 1 not in details  # Explicit contract posting is stored but not fetched/analyzed.


def test_unknown_visibility_and_missing_body_stay_incomplete(company, config, monkeypatch):
    @asynccontextmanager
    async def fake_session(tools, company):
        yield None, {'listCnt': 2, 'list': [row(1), row(2)]}

    async def fake_get(page, endpoint, params):
        if endpoint == hyundai.VISIBILITY_API:
            return {'applySecret': {'recuNoticeSecretYn': None if params['recuCls'] == '1' else 'N'}}
        return {'applyInfo': row(2)}

    monkeypatch.setattr(hyundai, 'session', fake_session)
    monkeypatch.setattr(hyundai, 'get', fake_get)
    result = asyncio.run(hyundai.collect(NS(settings=config[0]), company))
    assert result.status == 'partial' and all(not j.verified for j in result.jobs)
