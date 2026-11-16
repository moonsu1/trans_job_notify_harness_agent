"""Public employer catalogs as a fallback when the official channel is incomplete."""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from .collectors import CollectionError, allowed_url, soup_text, verified_jd_body
from .core import Job, canonical_url, plain, prefilter, role_relevant

HOST = 'https://recruit.wanted.jobs'


def normalize(value):
    return re.sub(r'주식회사|\(주\)|[^a-z0-9가-힣]', '', value.casefold())


def aliases(company):
    return {normalize(s) for s in [company['name'], company.get('portal_company_name', '')]
            +company.get('wanted_aliases', []) if s}


def next_props(html):
    soup = BeautifulSoup(html, 'html.parser')
    data = soup.select_one('#__NEXT_DATA__')
    if not data:
        raise CollectionError('원티드 공개 회사/공고 데이터 미확인')
    return json.loads(data.string)['props']['pageProps']


def company_hires(props, ident):
    queries = props.get('dehydrateState', {}).get('queries', [])
    for query in queries:
        if query.get('queryKey') == ['companyHire', str(ident)]:
            state = query['state']
            data = state.get('data')
            if state.get('status') != 'success' or not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
                raise CollectionError('원티드 회사 목록 응답 미확인')
            return data['jobs']
    raise CollectionError('원티드 실제 회사 채용 목록 미확인')


async def supplement(tools, company, primary):
    """Never infer a legal employer from a client name in an outsourcing ad."""
    expected = aliases(company)
    added, unresolved, examined = 0, 0, 0
    try:
        data = (await tools.fetch(HOST+'/api/v4/search', params={'query': company['name'], 'country': 'kr'})).json()
        companies = data.get('data', {}).get('companies')
        if not isinstance(companies, list):
            raise CollectionError('원티드 회사 검색 응답 미확인')
        matched = [c for c in companies if normalize(c.get('name', '')) in expected]
        for employer in matched:
            ident = employer['id']
            # Verify the stable company ID against a separate public employer record.
            checked = (await tools.fetch(HOST+'/api/v4/companies/'+str(ident))).json().get('company', {})
            if checked.get('id') != ident or normalize(checked.get('name', '')) not in expected:
                raise CollectionError('원티드 실제 채용 법인 불일치')
            props = next_props((await tools.fetch(HOST+'/company/'+str(ident))).text)
            rows = company_hires(props, ident)
            examined += len(rows)
            for row in rows:
                if row.get('hidden') or row.get('status') != 'active':
                    continue
                url = HOST+'/wd/'+str(row['id'])
                if any(j.url == url for j in primary.jobs):
                    continue
                kind = {'regular': '정규직', 'contract': '계약직', 'intern': '인턴'}.get(row.get('employment_type'), '')
                job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                          title=row['name'], external_id='wanted-'+str(row['id']), url=url,
                          source_url=HOST+'/company/'+str(ident), source_kind='mirror', employer_verified=True,
                          employment_type=kind, deadline=row.get('due_time') or '',
                          location=row.get('address', {}).get('full_location') or row.get('address', {}).get('location', ''))
                if prefilter(job, tools.settings) or not role_relevant(job, tools.settings):
                    primary.jobs.append(job)
                    added += 1
                    continue
                try:
                    details = next_props((await tools.fetch(url)).text)['initialData']
                    legal = details.get('company', {})
                    if legal.get('company_id') != ident or normalize(legal.get('company_name', '')) not in expected:
                        raise CollectionError('원티드 상세 실제 법인 불일치')
                    if details.get('id') != row['id']:
                        raise CollectionError('원티드 공고 ID 불일치')
                    if details.get('hidden') or details.get('is_private') or details.get('status') != 'active':
                        job.status = 'closed'
                        continue
                    job.body = plain(' '.join(label+' '+soup_text(details.get(k) or '') for label, k in
                                              [('소개', 'intro'), ('담당업무', 'main_tasks'), ('필수 자격', 'requirements'),
                                               ('우대사항', 'preferred_points'), ('복리후생', 'benefits')]))
                    job.verified = verified_jd_body(job.body)
                    target_url = details.get('out_link') or ''
                    official = next((j for j in primary.jobs if target_url and allowed_url(target_url, company)
                                     and canonical_url(j.url) == canonical_url(target_url)), None)
                    if official and job.verified and not official.verified:
                        # Reuse the official posting identity, backed by the
                        # verified publisher and its exact official application link.
                        official.body = job.body
                        official.verified = True
                        official.source_kind = 'mirror'
                        official.source_url = job.url
                        official.source_note = '공식 목록의 동일 지원 URL과 실제 법인을 대조한 재게시 JD'
                        added += 1
                        continue
                    unresolved += not job.verified
                except Exception:
                    unresolved += 1
                primary.jobs.append(job)
                added += 1
        if added and primary.status == 'failed':
            primary.status = 'partial'
        primary.detail += (f' / 원티드 실제 법인 확인 {len(matched)}사 · 공개 목록 {examined}건 · '
                           f'보완 {added}건 · 관련 JD 미확인 {unresolved}건; 공식 채널 완전 수집은 아님')
    except Exception as exc:
        primary.detail += ' / 원티드 보완 실패: '+(str(exc) if isinstance(exc, CollectionError) else type(exc).__name__)
    return primary
