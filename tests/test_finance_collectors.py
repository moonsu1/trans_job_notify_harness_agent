import asyncio
from types import SimpleNamespace as NS

import httpx
import pytest

from career_scout.collectors import Collection, CollectionError
from career_scout.finance_collectors import collect_mobis, collect_special, supplement_kofia


def response(html):
    return httpx.Response(200, text=html)


def test_mobis_empty_shell_is_not_proof_of_no_jobs():
    async def fetch(url):
        return response('<div id="app"></div>')

    company = dict(id='mobis', url='https://careers.mobis.com/jobs')
    result = asyncio.run(collect_mobis(NS(fetch=fetch), company))
    assert result.status == 'partial'


def test_mobis_explicit_official_zero_count():
    async def fetch(url):
        return response('0개의 채용 공고가 현재 진행 중입니다')

    result = asyncio.run(collect_mobis(NS(fetch=fetch), dict(id='mobis', url='https://careers.mobis.com/jobs')))
    assert result.status == 'ok' and not result.jobs


def test_kakaobank_refuses_unconfirmed_permanent_filter():
    async def fetch(*args, **kwargs):
        assert kwargs['json']['recruitEmployeeTypes'] == ['정규직']
        return httpx.Response(200, json={'list': [], 'paging': {'totalElements': 0}})

    with pytest.raises(CollectionError):
        asyncio.run(collect_special(NS(fetch=fetch), dict(id='kakaobank')))


def test_kofia_verifies_legal_employer_and_exact_deadline(monkeypatch):
    async def catalog(*args):
        return ([dict(company='하나증권', title='AI Agent 개발', url='https://www.kofia.or.kr/detail')], 'fixture')

    async def fetch(url):
        return response('<table><tr><th>회원사명</th><td>하나증권</td></tr>'
                        '<tr><th>접수기간</th><td>20261101 ~ 20261115</td></tr>'
                        '<tr><td class="first lastline">'+('AI Agent 정규직 개발 경력 ' * 30)+'</td></tr></table>')

    monkeypatch.setattr('career_scout.finance_collectors.cached_catalog', catalog)
    company = dict(id='hana_securities', name='하나증권', tier=3, url='https://official.example')
    result = asyncio.run(supplement_kofia(NS(fetch=fetch), company, Collection('hana_securities', status='failed')))
    assert result.status == 'partial'
    assert result.jobs[0].deadline == '2026-11-15'
    assert result.jobs[0].verified and result.jobs[0].employer_verified

    company['name'] = '다른증권'
    result = asyncio.run(supplement_kofia(NS(fetch=fetch), company, Collection('hana_securities', status='failed')))
    assert not result.jobs
