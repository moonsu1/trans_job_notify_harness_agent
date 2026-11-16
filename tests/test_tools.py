from types import SimpleNamespace as NS

import pytest

from career_scout.collectors import verified_jd_body
from career_scout.collectors import CollectionError, PublicTools, allowed_url, jsonld_jobs, public_url, search_sources, soup_text, normalize_deadline, explicit_closed


def test_conversational_jd_sections_and_third_party_bank_portal(config, job):
    from career_scout.collectors import allowed_url
    from career_scout.core import prefilter
    assert verified_jd_body('합류하면 함께 할 업무에요 ' + 'AI Agent 개발. ' * 50 + '이런 분과 함께하고 싶어요')
    assert not verified_jd_body('AI Agent 개발. ' * 50)
    assert not allowed_url('https://kbgoodjob.kbstar.com/job/123', {'id': 'kb', 'domains': ['kbstar.com']})
    job.company_id = 'kb'
    job.url = 'https://kbgoodjob.kbstar.com/job/123'
    assert prefilter(job, config[0])

def test_jsonld_graph():
    html = '<script type="application/ld+json">{"@graph":[{"@type":"JobPosting","title":"AI"}]}</script>'
    assert jsonld_jobs(html)[0]['title'] == 'AI'


def test_escaped_html_and_deadline():
    assert soup_text('&lt;p&gt;LLM Agent&lt;/p&gt;') == 'LLM Agent'
    assert soup_text('&amp;lt;p&amp;gt;LLM&amp;lt;/p&amp;gt;') == 'LLM'
    assert normalize_deadline('2026.11.12 10:00:00') == '2026-11-12 10:00:00'


def test_closed_requires_explicit_notice():
    assert explicit_closed('해당 공고는 마감되었습니다')
    assert not explicit_closed('본 공고는 인재 선발 완료 시 조기 마감될 수 있습니다')


def test_url_security(config):
    c = next(c for c in config[2] if c['id'] == 'skt')
    assert allowed_url('https://careers.sktelecom.com/job/1', c)
    assert not allowed_url('https://careers.sktelecom.com.evil.example/job/1', c)
    assert not allowed_url('javascript:alert(1)', c)
    with pytest.raises(CollectionError):
        public_url('http://127.0.0.1/private')
    with pytest.raises(CollectionError):
        public_url('http://169.254.169.254/latest/meta-data')


def test_search_only_tool_sources(config):
    c = next(c for c in config[2] if c['id'] == 'skt')
    response = NS(output=[NS(type='message', content='https://evil.example'),
                          NS(type='web_search_call', action=NS(sources=[
                              NS(url='https://careers.sktelecom.com/jobs/1'),
                              NS(url='https://evil.example')]))])
    assert search_sources(response, [c]) == [(c, 'https://careers.sktelecom.com/jobs/1')]


def test_naver_javascript_links(config):
    c = next(c for c in config[2] if c['id'] == 'naver')
    links = PublicTools.list_links('<li onclick="show(\'123\')">AI Engineer</li>', c)
    assert len(links) == 1
    assert 'annoId=123' in links[0][0]


def test_public_reposting_deadline_is_not_ignored():
    from career_scout.collectors import deadline_from_text
    assert deadline_from_text('접수기간 20260528~20260614 사이트바로가기') == '2026-06-14'
    assert deadline_from_text('지원서 접수 마감 : 2026.11.12(목) 10:00') == '2026-11-12'
    assert deadline_from_text('모집기간 2026-11-01 ~ 2026-11-30') == '2026-11-30'
    assert deadline_from_text('수시채용, 개인정보 보유기간 180일') == ''


def test_sk_shared_portal_uses_actual_employer_and_title():
    from career_scout.collectors import sk_detail_metadata, employer_matches
    html = '''<h1>SK Careers</h1><h2 class="box-title">AI Solutions Engineer 구성원 영입</h2>
    <div class="box-detail-item"><div class="label">회사</div><div class="value">SK biopharmaceuticals</div></div>
    <div class="box-detail-item"><div class="label">지원 기간</div><div class="value">October 28, 2026(Wed)~November 10, 2026(Tue)</div></div>
    <div class="box-detail-item"><div class="label">지역</div><div class="value">Gyeonggi/Incheon - 판교</div></div>'''
    meta = sk_detail_metadata(html)
    assert meta['company'] == 'SK biopharmaceuticals'
    assert meta['title'] == 'AI Solutions Engineer 구성원 영입'
    assert meta['deadline'] == '2026-11-10'
    assert not employer_matches(meta['company'], {'id': 'skhynix', 'name': 'SK하이닉스'})
