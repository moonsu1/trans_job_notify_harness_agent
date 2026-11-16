"""Public SK/Samsung catalogs, with total-count checks and verified legal employers."""
from __future__ import annotations

from .health import CatalogProof

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .collectors import Collection, CollectionError, USER_AGENT, soup_text, verified_jd_body
from .core import Job, KST, plain, prefilter


def portal_deadline(value):
    value = re.sub(r'\([^)]*\)', '', str(value or '')).strip()
    formats = [(r'\d{12}', '%Y%m%d%H%M'), (r'\d{8}', '%Y%m%d'),
               (r'\d{4}\.\d{2}\.\d{2}', '%Y.%m.%d'), (r'\d{4}-\d{2}-\d{2}', '%Y-%m-%d'),
               (r'[A-Za-z]+ \d{1,2}, \d{4}', '%B %d, %Y')]
    for pattern, fmt in formats:
        if not re.fullmatch(pattern, value):
            continue
        try:
            date = datetime.strptime(value, fmt)
            return date.replace(tzinfo=KST).isoformat() if fmt == '%Y%m%d%H%M' else date.date().isoformat()
        except ValueError:
            continue
    return ''


@asynccontextmanager
async def browser_response(tools, url, response_path):
    if not tools.browser:
        raise CollectionError('공개 목록의 정상 브라우저 로딩 필요: 브라우저 수집 비활성')
    await tools.permitted(url)
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = None
        try:
            page = await browser.new_page(user_agent=USER_AGENT)
            await page.route('**/*', tools.route_public)
            async with page.expect_response(lambda r: r.url.split('?', 1)[0].endswith(response_path),
                                            timeout=30000) as capture:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            response = await capture.value
            if hasattr(tools, 'vault'):
                tools.vault.capture(response.url, await response.text(), status=response.status, kind='http')
            if response.status != 200:
                raise CollectionError(f'공개 목록 HTTP {response.status}')
            yield page, response
        finally:
            if page:
                await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


async def cached_catalog(tools, name, loader):
    # A run shares one public catalog across companies, avoiding repeated browser loads.
    if not hasattr(tools, '_portal_catalogs'):
        tools._portal_catalogs, tools._portal_locks = {}, {}
    lock = tools._portal_locks.setdefault(name, asyncio.Lock())
    async with lock:
        if name not in tools._portal_catalogs:
            tools._portal_catalogs[name] = await loader()
        return tools._portal_catalogs[name]


async def supplemental_notes(tools, company):
    notes = []
    for url in company.get('supplemental_urls', []):
        try:
            response = await tools.fetch(url)
            if company['id'] == 'samsung_research' and url == 'https://research.samsung.com/careers':
                soup = BeautifulSoup(response.text, 'html.parser')
                headquarters = next((li for li in soup.select('.careers__listBox')
                                     if 'SR Korea(HQ)' in li.get_text()), None)
                channel = headquarters.select_one('.careers__webSite a[href]') if headquarters else None
                if channel and urlsplit(channel['href']).hostname == 'www.samsungcareers.com':
                    # Korea links to the very same official catalog already checked.
                    # Overseas Workday links are not additional Korean vacancies.
                    channel_observation(tools, company, url, 'ok', 'SR Korea(HQ) 공식 지원 링크가 삼성 공통 목록과 같음', linked=True)
                    continue
            notes.append(f'별도 채널 원문 수집 미완료: {url}')
            channel_observation(tools, company, url, 'partial', notes[-1])
        except Exception as exc:
            notes.append(f'별도 채널 원문 미확인: {url} ({exc if isinstance(exc, CollectionError) else type(exc).__name__})')
            channel_observation(tools, company, url, 'partial', notes[-1])
    return notes


def channel_observation(tools, company, url, status, detail, *, linked=False):
    from .core import canonical_url
    from .health import CatalogProof
    records = [r for r in tools.evidence_for(company) if canonical_url(r['url']) == canonical_url(url)] if hasattr(tools, 'evidence_for') else []
    entry = dict(channel='supplemental:'+url, channel_role='same_catalog_link' if linked else 'supplemental_catalog',
                 status=status, detail=detail, schema_hash=records[-1]['schema_hash'] if records else '',
                 evidence_ids=[r['id'] for r in records])
    if linked:
        entry['proof'] = CatalogProof(expected=0, observed=0, complete=True, empty_confirmed=True, basis='official_same_catalog_link').model_dump()
    if not hasattr(tools, '_channel_observations'):
        tools._channel_observations = {}
    tools._channel_observations.setdefault(company['id'], {})[url] = entry
    return entry


def sk_catalog(payload):
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise CollectionError('SK 공개 목록 조회 실패')
    values = payload.get('list')
    total = payload.get('totalCount')
    if not isinstance(values, list) or not isinstance(total, int) or total < 0:
        raise CollectionError('SK 공개 목록 응답 형식 오류')
    if any(not isinstance(v, dict) or not re.fullmatch(r'R\d+', str(v.get('noticeID', '')))
           or not v.get('corpName') or not v.get('title') for v in values):
        raise CollectionError('SK 목록 공고 식별자/법인/제목 누락')
    ids = [v['noticeID'] for v in values]
    complete = len(ids) == len(set(ids)) == total
    return values, total, complete


def sk_matches(employer, company):
    def normalize(s):
        return re.sub(r'[^a-z0-9가-힣]', '', s.casefold())
    aliases = ('SK hynix', 'SK하이닉스') if company['id'] == 'skhynix' else ('SK telecom', 'SK텔레콤')
    return normalize(employer) in {normalize(a) for a in aliases}


async def collect_sk(tools, company):
    async def load():
        async with browser_response(tools, 'https://www.skcareers.com/Recruit', '/Recruit/GetRecruitList') as (_, r):
            return sk_catalog(await r.json())

    values, total, complete = await cached_catalog(tools, 'sk', load)
    target = [v for v in values if sk_matches(v['corpName'], company)]
    jobs, errors, skipped = [], 0, 0
    for value in target:
        candidate = Job(company_id=company['id'], company=value['corpName'], tier=company['tier'],
                        employer_verified=True, external_id=value['noticeID'],
                        url='https://www.skcareers.com/Recruit/Detail/'+value['noticeID'],
                        source_url=company['url'], title=plain(value['title']), location=value.get('workingArea', ''),
                        employment_type=value.get('workingType', ''), experience=value.get('recruitType', ''),
                        deadline=portal_deadline(value.get('end', '')))
        if prefilter(candidate, tools.settings):
            skipped += 1
            jobs.append(candidate)
            continue
        try:
            job = await tools.detail(candidate.url, company, candidate.title)
            if not sk_matches(job.company, company):
                raise CollectionError('SK 목록/상세 실제 법인 불일치')
            job.external_id = candidate.external_id
            if not job.verified:
                errors += 1
            jobs.append(job)
        except Exception:
            errors += 1
            jobs.append(candidate)
    hub_summary = ''
    if company.get('hub_url'):
        try:
            html = (await tools.fetch(company['hub_url'])).text
            soup = BeautifulSoup(html, 'html.parser')
            active = soup.select_one('.recruit-lnb-item.ing .lnb-list-header[data-type]')
            group = soup.select_one('.sub-common-layout-group[data-type="'+active['data-type']+'"]') if active else None
            empty = group.select_one('.empty-content .text') if group else None
            if empty and '곧 공개될 새로운 공고' in empty.get_text():
                hub_summary = ' / Talent Hub 현재 모집 탭의 공고 준비 중 안내 확인'
                notes = []
                entry = channel_observation(tools, company, company['hub_url'], 'ok', '공식 현재 모집 탭의 공고 준비 중 문구 확인')
                entry['proof'] = dict(expected=0, observed=0, company_count=0, complete=True, empty_confirmed=True,
                                      basis='official_empty_current_tab')
            else:
                # New public cards require their own detail URLs, never treat an unknown layout as zero.
                notes = ['Talent Hub 모집 탭 변경: 새 공고 원문 확인 대기 '+company['hub_url']]
                channel_observation(tools, company, company['hub_url'], 'partial', notes[-1])
        except Exception as exc:
            notes = ['Talent Hub 원문 확인 실패: '+str(exc)]
            channel_observation(tools, company, company['hub_url'], 'partial', notes[-1])
    else:
        notes = await supplemental_notes(tools, company)
    status = 'ok' if complete and not errors and not notes else 'partial'
    detail = (f'SK 공동 포털 공개 목록 {len(values)}/{total}건 확인 / 실제 대상 법인 {len(target)}건 / '
              f'상세 JD {len(jobs)-errors-skipped}건 확인 / 사전 제외 {skipped}건 / 상세 실패 {errors}건'+hub_summary)
    if not complete:
        detail += ' / 전체 건수 불일치 또는 중복: 포털 전체 확인 미완료'
    if notes:
        detail += ' / '+'; '.join(notes)
    return Collection(company['id'], jobs, status, detail,
                      proof=CatalogProof(expected=total, observed=len(values), company_count=len(jobs),
                                         pages_observed=1, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'),
                      channels=list(getattr(tools, '_channel_observations', {}).get(company['id'], {}).values()))


def samsung_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    counter = soup.select_one('.divCnt')
    if not counter:
        raise CollectionError('삼성 목록 총건수 요소 없음: 빈 공고로 처리하지 않음')
    try:
        total, pages = int(counter['data-value']), int(counter['data-max'])
        if total < 0 or pages < 0:
            raise ValueError
    except (KeyError, ValueError):
        raise CollectionError('삼성 목록 총건수/페이지 형식 오류') from None
    rows = []
    for item in soup.select('li'):
        title, employer = item.select_one('.title'), item.select_one('.company')
        anchor = item.select_one('a[data-value]')
        if not title or not employer or not anchor:
            continue
        ident = anchor['data-value'].replace(',', '')
        if not ident.isdigit():
            raise CollectionError('삼성 공고 식별자 오류')
        rows.append({'id': ident, 'title': plain(title.get_text()), 'company': plain(employer.get_text()),
                     'period': plain(item.select_one('.period').get_text()) if item.select_one('.period') else ''})
    return rows, total, pages


async def samsung_catalog(tools):
    async with browser_response(tools, 'https://www.samsungcareers.com/hr/?ty=B', '/hr/list.data') as (page, response):
        rows, total, pages = samsung_page(await response.text())
        issues = []
        for number in range(2, min(pages, 100)+1):
            try:
                html = await page.evaluate('''async (number) => await $.ajax({
                    url:'/hr/list.data', method:'POST', dataType:'html',
                    data:{currentPageNo:number, intNo:0, strVal:'', strTxt:'', strKey:'',
                          strCompany:'', strType:'B', strOrderBy:'', strEntity:''}
                })''', number)
                more, next_total, next_pages = samsung_page(html)
                rows.extend(more)
                if total != next_total or pages != next_pages:
                    issues.append('조회 중 총건수/페이지 변경')
            except Exception:
                issues.append(f'{number}페이지 조회 실패')
                break
        if len(rows) != total or len({r['id'] for r in rows}) != total or pages > 100:
            issues.append('전체 공고 건수 불일치/중복/페이지 상한')
        return rows, total, pages, issues


async def collect_samsung(tools, company):
    rows, total, pages, issues = await cached_catalog(tools, 'samsung', lambda: samsung_catalog(tools))
    target = [r for r in rows if r['company'] == company['portal_company_name']]
    jobs, errors = [], 0
    for row in target:
        # Public links open the site's original announcement modal.
        url = 'https://www.samsungcareers.com/hr/?no='+row['id']
        candidate = Job(company_id=company['id'], company=row['company'], tier=company['tier'],
                        employer_verified=True, external_id=row['id'], url=url, source_url=company['url'],
                        title=row['title'], experience='경력', deadline=portal_deadline(row['period'].split('~')[-1]))
        try:
            roles = await read_samsung_roles(tools, url, company)
            jobs.extend(roles)
            errors += sum(not job.verified for job in roles)
        except Exception:
            errors += 1
            jobs.append(candidate)
    notes = await supplemental_notes(tools, company)
    status = 'ok' if not issues and not errors and not notes else 'partial'
    detail = (f'삼성그룹 공통 경력 목록 {len(rows)}/{total}건 · {pages}페이지 확인 / '
              f'{company["portal_company_name"]} 공고 {len(target)}건 · 세부직무 {len(jobs)}건 / 상세 확인 대기 {errors}건')
    if company['id'] == 'samsung_research' and not notes:
        detail += ' / SR Korea(HQ) 공식 안내의 지원 채널이 삼성채용과 동일함 확인'
    if issues or notes:
        detail += ' / '+'; '.join(issues+notes)
    from .health import CatalogProof
    channels = list(getattr(tools, '_channel_observations', {}).get(company['id'], {}).values())
    return Collection(company['id'], jobs, status, detail,
                      proof=CatalogProof(expected=total, observed=len(rows), company_count=len(target),
                                         pages_expected=pages, pages_observed=pages if not issues else 0,
                                         complete=not issues, empty_confirmed=total == 0 and not issues,
                                         basis='official_catalog'), channels=channels)


async def samsung_detail_data(tools, url):
    async with browser_response(tools, url, '/recruit/detail.data') as (_, response):
        return await response.json()


def parse_samsung_roles(data, url, company):
    if data.get('success') is not True or not isinstance(data.get('data'), dict):
        raise CollectionError('삼성 공개 상세 조회 실패')
    value, items = data['data'].get('result'), data['data'].get('items')
    if not isinstance(value, dict) or not isinstance(items, list) or not items:
        raise CollectionError('삼성 공개 상세 직무 정보 없음')
    expected = parse_qs(urlsplit(url).query).get('no', [''])[0]
    if str(value.get('seq')) != expected or value.get('cmpNameKr') != company['portal_company_name']:
        raise CollectionError('삼성 상세 공고 ID 또는 실제 법인 불일치')
    jobs, ids = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise CollectionError('삼성 세부직무 응답 형식 오류')
        if item.get('rmYn') == 'Y':
            continue
        ident = str(item.get('seq') or '')
        title = plain(str(item.get('titleKr') or item.get('titleEn') or ''))
        if not ident.isdigit() or ident in ids or not title or str(item.get('recruitSeq')) != expected:
            raise CollectionError('삼성 세부직무 ID·제목·상위 공고 대조 실패 또는 중복')
        ids.add(ident)
        parts = [f'채용 법인: {value["cmpNameKr"]}', f'직무: {title}']
        for label, key in [('주요 업무','taskKr'),('지원 자격','qlfctKr'),('우대 사항','favorKr'),
                           ('직무 안내','explnKr'),('참고 사항','memoKr'),('근무지','workPlaceKr')]:
            text = soup_text(item.get(key) or '')
            if text:
                parts.append(label+': '+text)
        common = soup_text(value.get('qlfctKr') or '')
        if common:
            parts.append('공통 지원 자격: '+common)
        body = '\n'.join(parts)
        organization = re.match(r'^\[([^\]]{1,50})\]', title)
        memo = soup_text(item.get('memoKr') or '')
        employment = re.search(r'(?:근무|고용|채용)\s*형태\s*[:：]\s*([^\n;]{1,40})', memo)
        jobs.append(Job(company_id=company['id'], company=value['cmpNameKr'], tier=company['tier'],
                        employer_verified=True, external_id=expected+':'+ident, parent_external_id=expected,
                        url=url, url_is_catalog=True, source_url=company['url'], title=title, body=body,
                        location=soup_text(item.get('workPlaceKr') or '').lstrip('- '),
                        organization=organization[1] if organization else '',
                        employment_type=employment[1].strip() if employment else '', experience='경력',
                        deadline=portal_deadline(value.get('enddate', '')),
                        verified=bool(item.get('taskKr') and item.get('qlfctKr')) and verified_jd_body(body),
                        source_note=f'삼성 공식 공고 {expected} / 세부직무 {ident}의 업무·자격·근무조건을 개별 대조',
                        status='open' if value.get('isOpened') == 1 else 'unknown'))
    if not jobs:
        raise CollectionError('삼성 현재 세부직무 목록 미확인')
    return jobs


async def read_samsung_roles(tools, url, company):
    return parse_samsung_roles(await samsung_detail_data(tools, url), url, company)


async def read_samsung_detail(tools, url, company):
    data = await samsung_detail_data(tools, url)
    jobs = parse_samsung_roles(data, url, company)
    if len(jobs) == 1:
        return jobs[0]
    value = data['data']['result']
    expected = str(value['seq'])
    # Search discovery preserves the shared link; the catalog ingests verified roles separately.
    return Job(company_id=company['id'], company=value['cmpNameKr'], tier=company['tier'],
               employer_verified=True, external_id=expected, url=url, source_url=company['url'],
               title=value.get('title') or '삼성 경력 공고', body='\n\n'.join(j.body for j in jobs),
               location=' / '.join(dict.fromkeys(j.location for j in jobs)),
               experience='경력', deadline=portal_deadline(value.get('enddate', '')),
               verified=False, source_note=f'세부직무 {len(jobs)}개가 있는 공통 공고; 직무별 공고로 평가',
               status='open' if value.get('isOpened') == 1 else 'unknown')
