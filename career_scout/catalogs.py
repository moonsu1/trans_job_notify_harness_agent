"""Site-specific public catalogs. A catalog is complete only with count/paging proof."""
from __future__ import annotations

from .health import CatalogProof

import re
import json
import asyncio
import base64
import hashlib
from xml.etree import ElementTree
from urllib.parse import quote, urlencode, urljoin

from bs4 import BeautifulSoup
import yaml

from .collectors import Collection, CollectionError, deadline_from_text, soup_text, verified_jd_body
from .core import ROOT, Job, plain, prefilter, role_relevant
from .portals import browser_response, portal_deadline


async def collect(tools, company):
    readers = {'amazon': amazon, 'kakao': kakao, 'successfactors': successfactors,
               'nh': nonghyup, 'ibk': ibk, 'ibks': ibks, 'kyobo': kyobo,
               'ninehire': ninehire, 'maintenance': maintenance, 'google': google,
               'microsoft': microsoft, 'kdb': kdb, 'kiwoom': kiwoom,
               'shinhansec': shinhansec, 'careerlink': careerlink, 'meritzfire': meritzfire}
    return await readers[company['adapter']](tools, company)


async def amazon(tools, company):
    jobs, seen, total, complete = [], set(), None, False
    for offset in range(0, 1000, 100):
        data = (await tools.fetch('https://www.amazon.jobs/en/search.json', params={
            'normalized_country_code[]': 'KOR', 'base_query': 'AWS',
            'offset': offset, 'result_limit': 100})).json()
        rows, count = data.get('jobs'), data.get('hits')
        if data.get('error') or not isinstance(rows, list) or not isinstance(count, int):
            raise CollectionError('Amazon 공개 검색 목록 형식 오류')
        if total is not None and count != total:
            break
        total = count
        for row in rows:
            if row.get('country_code') != 'KOR':
                raise CollectionError('Amazon 한국 필터 응답 불일치')
            ident = str(row['id'])
            if ident in seen:
                continue
            seen.add(ident)
            body = soup_text('담당업무 '+row.get('description', '')+' 필수 자격 '+row.get('basic_qualifications', '')
                             +' 우대사항 '+row.get('preferred_qualifications', ''))
            jobs.append(Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                            employer_verified=True, title=row['title'], external_id=ident,
                            url=urljoin('https://www.amazon.jobs', row['job_path']), source_url=company['url'],
                            organization=row.get('business_category', ''), location=row.get('location', ''),
                            body=body, verified=verified_jd_body(body), status='open',
                            employment_type=row.get('job_schedule_type', '')))
        if len(seen) >= total:
            complete = len(seen) == total
            break
        if not rows:
            break
    return Collection(company['id'], jobs, 'ok' if complete else 'partial',
                      f'공식 AWS 키워드·한국 필터 {len(seen)}/{total}건; 원문 JD {sum(j.verified for j in jobs)}건',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=0, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'))


async def kakao(tools, company):
    jobs, seen, expected, complete = [], set(), 0, True
    # Explicitly query both product planning and engineering, rather than the site's default TECH tab.
    for part in ('TECHNOLOGY', 'BUSINESS_SERVICES'):
        total, ids = None, set()
        for page in range(1, 31):
            data = (await tools.fetch('https://careers.kakao.com/public/api/job-list', params={
                'company': 'KAKAO', 'part': part, 'page': page, 'employeeType': '', 'keyword': ''})).json()
            rows, count, pages = data.get('jobList'), data.get('totalJobCount'), data.get('totalPage')
            if not isinstance(rows, list) or not isinstance(count, int) or not isinstance(pages, int):
                raise CollectionError('카카오 공개 목록 형식 오류')
            if total is not None and total != count:
                complete = False
                break
            total = count
            for row in rows:
                if row.get('companyName') != company['name'] or row.get('privateFlag'):
                    raise CollectionError('카카오 공개 목록 법인·공개 여부 불일치')
                ident = row['realId']
                ids.add(ident)
                if ident in seen:
                    continue
                seen.add(ident)
                body = soup_text('조직 소개 '+row.get('introduction', '')+' 담당업무 '+row.get('workContentDesc', '')
                                 +' 필수 자격 '+row.get('qualification', '')
                                 +' 우대사항 '+row.get('preferredQualificationDesc', ''))
                jobs.append(Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                                employer_verified=True, external_id=ident, title=row['jobOfferTitle'],
                                url='https://careers.kakao.com/jobs/'+ident, source_url=company['url'],
                                location=row.get('locationName', ''), employment_type=row.get('employeeTypeName', ''),
                                deadline=row.get('endDate') or '', body=body, verified=verified_jd_body(body),
                                status='closed' if row.get('closeFlag') else 'open'))
            if page >= pages:
                complete = complete and len(ids) == total
                break
            if not rows:
                complete = False
                break
        expected += total or 0
    return Collection(company['id'], jobs, 'ok' if complete else 'partial',
                      f'공식 카카오 개발·서비스기획 목록 총 {expected}건, 통합 {len(jobs)}건; JD 포함; 법인 확인',
                      proof=CatalogProof(expected=expected, observed=len(seen), company_count=len(jobs),
                                         pages_observed=0, complete=complete,
                                         empty_confirmed=(expected == 0 and complete), basis='official_catalog'))


async def successfactors(tools, company):
    jobs, seen, total, complete, errors = [], set(), None, False, 0
    for offset in range(0, 1000, 25):
        html = (await tools.fetch(company['url'], params={'startrow': offset, 'q': '', 'locationsearch': ''})).text
        soup = BeautifulSoup(html, 'html.parser')
        label = soup.select_one('.paginationLabel')
        numbers = re.findall(r'\d+', label.get_text()) if label else []
        if len(numbers) < 3:
            raise CollectionError('공식 검색 목록 총건수 미확인')
        count = int(numbers[-1])
        if total is not None and total != count:
            break
        total = count
        for anchor in soup.select('#searchresults a[href*="/job/"]'):
            url = urljoin(company['url'], anchor['href'])
            if url in seen:
                continue
            seen.add(url)
            job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                      employer_verified=True, title=plain(anchor.get_text()), url=url, source_url=company['url'])
            # Even a general list title may hide an AI team. Read the official detail when in doubt.
            try:
                job = await tools.detail(url, company, job.title)
            except Exception:
                errors += 1
            jobs.append(job)
        if len(seen) >= total:
            complete = len(seen) == total
            break
    return Collection(company['id'], jobs, 'ok' if complete and not errors else 'partial',
                      f'공식 검색 목록 {len(seen)}/{total}건; 상세 실패 {errors}건',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=0, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'))


def nh_page(html, company):
    soup = BeautifulSoup(html, 'html.parser')
    code = company['nh_code']
    badge = soup.select_one('#badgeSearch_'+code+' .num')
    active = soup.select_one('.tabmenu2 li.active a')
    if not badge or not active or active.get('id') != 'badgeSearch_'+code:
        raise CollectionError('농협 실제 법인 필터 적용 미확인')
    total = int(badge.get_text().replace(',', ''))
    jobs = []
    for tr in soup.select('tbody tr'):
        a, cells = tr.select_one('[onclick*="fn_jbnfDtl"]'), tr.select('td')
        if not a or len(cells) < 6:
            continue
        legal = plain(cells[2].get_text())
        if legal != company['name']:
            raise CollectionError('농협 목록 실제 법인 불일치')
        ident = re.search(r"fn_jbnfDtl\('(\d+)'\)", a['onclick'])
        if not ident:
            raise CollectionError('농협 공고 식별자 미확인')
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', cells[-1].get_text())
        kind = plain(cells[0].get_text())
        description = cells[0].select_one('[title]')
        # Preserve the official definition of e.g. 파트타임 to avoid calling it permanent.
        kind += ' '+(description.get('title', '') if description else '')
        jobs.append(Job(company_id=company['id'], company=legal, tier=company['tier'],
                        employer_verified=True, external_id=ident[1], title=plain(a.get_text()),
                        url='https://with.nonghyup.com/jbnf/jbnfDtl.do?jbnfSqno='+ident[1],
                        source_url=company['url'], location=plain(cells[3].get_text()),
                        employment_type=plain(kind), deadline=dates[-1] if dates else ''))
    return jobs, total


async def nonghyup(tools, company):
    jobs, seen, total, complete, unresolved = [], set(), None, False, 0
    for page in range(1, 101):
        html = (await tools.fetch(company['url'], params={'srcAuthDsc': company['nh_code'], 'pageIndex': page})).text
        batch, count = nh_page(html, company)
        if total is not None and count != total:
            break
        total = count
        for job in batch:
            if job.external_id in seen:
                raise CollectionError('농협 페이지 중복: 전체 목록 확인 실패')
            seen.add(job.external_id)
            if not prefilter(job, tools.settings) and role_relevant(job, tools.settings):
                try:
                    detailed = await tools.detail(job.url, company, job.title)
                    detailed.employment_type = job.employment_type
                    detailed.location = job.location
                    detailed.deadline = job.deadline
                    job = detailed
                    unresolved += not job.verified
                except Exception:
                    unresolved += 1
            jobs.append(job)
        if len(seen) >= total:
            complete = len(seen) == total
            break
        if not batch:
            break
    return Collection(company['id'], jobs, 'ok' if complete and not unresolved else 'partial',
                      f'공식 법인별 목록 {len(seen)}/{total}건 · {page}페이지; 관련 JD 미확인 {unresolved}건',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=page, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'))


async def ibk(tools, company):
    html = (await tools.fetch(company['url'])).text
    soup = BeautifulSoup(html, 'html.parser')
    links = {urljoin(company['url'], a['href']).replace('http://', 'https://'): plain(a.get_text())
             for a in soup.select('a[href*="/hire/viewhire.asp?projectid="]')}
    if not links:
        raise CollectionError('IBK 공식 채용 목록 미확인')
    jobs, unresolved = [], 0
    for url, title in links.items():
        job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                  employer_verified=True, title=title, url=url, source_url=company['url'])
        if not prefilter(job, tools.settings):
            try:
                job = await tools.detail(url, company, title)
                unresolved += not job.verified
            except Exception:
                unresolved += 1
        jobs.append(job)
    # The landing navigation is a curated list, with no total/pagination contract.
    return Collection(company['id'], jobs, 'partial',
                      f'공식 채용 메뉴 공고 {len(jobs)}건; 상세 미확인 {unresolved}건; 별도 수시 채용 채널도 확인 필요')


async def ibks(tools, company):
    jobs, seen, total, complete, unresolved = [], set(), None, False, 0
    for page in range(1, 101):
        data = (await tools.fetch('https://recruit.ibks.com/rec/api/ibks/announce/list',
                                 params={'searchWord': '', 'includeFinAnno': 'N', 'page': page})).json()
        rows, pages = data.get('rows'), data.get('total')
        if not isinstance(rows, list) or not isinstance(pages, int):
            raise CollectionError('IBK투자증권 공개 목록 형식 오류')
        if total is not None and total != pages:
            break
        total = pages  # This API returns total PAGES, not total job count.
        for row in rows:
            ident = str(row['annoId'])
            if ident in seen:
                raise CollectionError('IBK투자증권 페이지 중복')
            seen.add(ident)
            job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                      employer_verified=True, external_id=ident, title=row['title'],
                      url='https://recruit.ibks.com/cv/announces/ibks/'+ident, source_url=company['url'],
                      deadline=portal_deadline(row.get('term', '').split('~')[-1].strip()[:10]),
                      status='open' if row.get('status') == 'DOC' else 'unknown')
            if not row.get('annoPassYn') or row.get('annoPassYn') == 'N':
                try:
                    detail = (await tools.fetch('https://recruit.ibks.com/rec/api/ibks/announce',
                                                params={'annoId': ident})).json()
                    if detail.get('title') != job.title or detail.get('annoPassword'):
                        raise CollectionError('IBK투자증권 상세 식별자·공개 여부 불일치')
                    job.body = soup_text(detail.get('content', ''))
                    job.verified = verified_jd_body(job.body)
                except Exception:
                    unresolved += 1
            else:
                unresolved += 1
            jobs.append(job)
        if page >= pages:
            complete = True
            break
    return Collection(company['id'], jobs, 'ok' if complete and not unresolved else 'partial',
                      f'공식 진행 공고 {len(jobs)}건 · {page}/{total}페이지; 관련 JD 미확인 {unresolved}건',
                      proof=CatalogProof(expected=len(seen), observed=len(seen), company_count=len(jobs),
                                         pages_observed=page, complete=complete,
                                         empty_confirmed=(len(seen) == 0 and complete), basis='official_catalog'))


def xsheet(text):
    try:
        root = ElementTree.fromstring(text.strip())
    except ElementTree.ParseError as exc:
        raise CollectionError('교보 공개 목록 XML 형식 오류') from exc
    if root.tag != 'SHEET' or (root.findtext('MESSAGE') or '').strip():
        raise CollectionError('교보 공개 목록 오류 응답')
    data = root.find('DATA')
    if data is None or data.find('HTR') is None:
        raise CollectionError('교보 공개 목록 열 정의 미확인')
    keys = [e.text or '' for e in data.find('HTR').findall('TD')]
    return [dict(zip(keys, [e.text or '' for e in row.findall('TD')], strict=True))
            for row in data.findall('TR')]


async def kyobo(tools, company):
    params = {'S_DSCLASS': 'biz.rem.apply.recruit.Apply_list', 'S_PAGE_NO': '1',
              'S_PAGE_CNT': '1', 'S_TAB': 'tab_all', 'S_FORWARD': 'xsheetResultXML',
              'S_RE_CLASS': '', 'S_RE_TYPE': 'all'}
    count_response = await tools.fetch('https://career.kyobo.co.kr/home', 'POST',
                                       data=dict(params, S_DSMETHOD='getPageCnt'), headers={'ajax': 'true'})
    counts = xsheet(count_response.text)
    if not counts or not str(counts[0].get('PAGE_CNT', '')).isdigit():
        raise CollectionError('교보 공식 목록 페이지 수 미확인')
    pages = int(counts[0]['PAGE_CNT'])
    jobs, seen, unresolved = [], set(), 0
    for page in range(1, max(1, min(50, pages))+1):
        response = await tools.fetch('https://career.kyobo.co.kr/home', 'POST',
                                     data=dict(params, S_DSMETHOD='search01', S_PAGE_NO=str(page)),
                                     headers={'ajax': 'true'})
        rows = xsheet(response.text)
        if pages == 0 and rows:
            raise CollectionError('교보 빈 목록·페이지 수 불일치')
        if pages and not rows:
            raise CollectionError('교보 빈 페이지: 전체 목록 확인 실패')
        for row in rows:
            ident = (row['C_CD'], row['RE_NO'], row['NOTI_SEQ_NO'])
            if ident in seen:
                raise CollectionError('교보 목록 페이지 중복')
            seen.add(ident)
            view = {'S_DSCLASS': 'biz.rem.apply.recruit.Apply_view', 'S_DSMETHOD': 'search01',
                    'S_FORWARD': '/rem/apply/recruit/apply_view.jsp', 'S_C_CD': ident[0],
                    'S_RE_NO': ident[1], 'S_NOTI_SEQ_NO': ident[2]}
            job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                      employer_verified=True, external_id='-'.join(ident), title=row['TITLE'],
                      url='https://career.kyobo.co.kr/home?'+urlencode(view), source_url=company['url'],
                      deadline=portal_deadline(row.get('RCP_END_YMD', '')),
                      status='open' if row.get('STATUS') == '접수중' else 'closed')
            if not prefilter(job, tools.settings) and role_relevant(job, tools.settings):
                detail = await tools.fetch('https://career.kyobo.co.kr/home', 'POST', data=view)
                job.body = soup_text(detail.text)
                job.verified = verified_jd_body(job.body)
                unresolved += not job.verified and not prefilter(job, tools.settings)
            jobs.append(job)
    return Collection(company['id'], jobs, 'ok' if pages <= 50 and not unresolved else 'partial',
                      f'공식 전체 목록 {len(jobs)}건 · {pages}페이지; 관련 JD 미확인 {unresolved}건; 빈 목록은 XML·페이지 수 대조',
                      proof=CatalogProof(expected=len(seen), observed=len(seen), company_count=len(jobs),
                                         pages_observed=pages, complete=pages <= 50,
                                         empty_confirmed=(len(seen) == 0 and pages <= 50), basis='official_catalog'))


async def ninehire(tools, company):
    response = await tools.fetch(company['url'])
    soup = BeautifulSoup(response.text, 'html.parser')
    node = soup.select_one('#__NEXT_DATA__')
    if not node:
        raise CollectionError('Ninehire 공개 회사 데이터 미확인')
    props = json.loads(node.string)['props']['pageProps']['homepageProps']
    info = props['info']
    if info.get('companyName') != company['name']:
        raise CollectionError('Ninehire 실제 법인 불일치')
    jobs, seen, count, complete = [], set(), None, False
    for page in range(1, 51):
        data = (await tools.fetch('https://api.ninehire.com/identity-access/homepage/recruitments', params={
            'companyId': info['companyId'], 'page': page, 'countPerPage': 100, 'order': 'created_at_desc'})).json()
        rows, total = data.get('results'), data.get('count')
        if not isinstance(rows, list) or not isinstance(total, int):
            raise CollectionError('Ninehire 공개 목록 형식 오류')
        if count is not None and count != total:
            break
        count = total
        for row in rows:
            ident = str(row['recruitmentId'])
            if ident in seen:
                raise CollectionError('Ninehire 목록 중복')
            seen.add(ident)
            # The list schema is useful for discovery, but never fabricate detail text or URL.
            url = row.get('url') or company['url']
            jobs.append(Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                            employer_verified=True, external_id=ident, title=row.get('externalTitle') or '제목 미확인',
                            url=url, source_url=company['url']))
        if len(seen) >= total:
            complete = len(seen) == total
            break
        if not rows:
            break
    return Collection(company['id'], jobs, 'ok' if complete and not jobs else 'partial',
                      f'공식 회사 식별자·공개 목록 {len(seen)}/{count}건; 신규 상세 JD 미확인 {len(jobs)}건',
                      proof=CatalogProof(expected=count, observed=len(seen), company_count=len(jobs),
                                         pages_observed=0, complete=complete,
                                         empty_confirmed=(count == 0 and complete), basis='official_catalog'))


async def maintenance(tools, company):
    html = (await tools.fetch(company['url'])).text
    if 'reMenuAction.do' in html and len(soup_text(html)) < 100:
        html = (await tools.fetch(urljoin(company['url'], 'reMenuAction.do'))).text
    # Follow this site's own public maintenance link, without executing arbitrary scripts.
    if 'access_block.html' in html:
        html = (await tools.fetch(urljoin(company['url'], 'access_block.html'))).text
    text = soup_text(html)
    if '시스템 개선 작업' in text:
        return Collection(company['id'], status='failed',
                          detail='공식 사이트 점검 중: '+text[:450]+'; 점검 종료 후 자동 재확인')
    return await tools.generic(company)


def google_page(html, company):
    soup = BeautifulSoup(html, 'html.parser')
    count = re.search(r'([\d,]+) jobs? matched', soup.get_text(' ', strip=True))
    if not count:
        raise CollectionError('Google 공식 검색 총건수 미확인')
    base = soup.select_one('base[href]')
    base = urljoin(company['url'], base['href']) if base else 'https://www.google.com/about/careers/applications/'
    jobs = []
    for a in soup.select('a[href]'):
        href = a['href']
        if not re.search(r'jobs/results/\d+', href):
            continue
        title = a.get('aria-label', '').removeprefix('Learn more about ')
        if not title:
            continue
        url = urljoin(base, href)
        jobs.append(Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                        employer_verified=True, title=title, url=url, source_url=company['url']))
    return jobs, int(count[1].replace(',', ''))


async def google(tools, company):
    jobs, seen, total, complete, unresolved = [], set(), None, False, 0
    for page in range(1, 51):
        html = (await tools.fetch(company['url'], params={'location': 'Seoul, South Korea', 'page': page})).text
        batch, count = google_page(html, company)
        if total is not None and count != total:
            break
        total = count
        for job in batch:
            if job.url in seen:
                raise CollectionError('Google 목록 페이지 중복')
            seen.add(job.url)
            try:
                job = await tools.detail(job.url, company, job.title)
                unresolved += not job.verified
            except Exception:
                unresolved += 1
            jobs.append(job)
        if len(seen) >= total:
            complete = len(seen) == total
            break
        if not batch:
            break
    return Collection(company['id'], jobs, 'ok' if complete and not unresolved else 'partial',
                      f'공식 Seoul 검색 {len(seen)}/{total}건; 상세 JD {sum(j.verified for j in jobs)}건; 미확인 {unresolved}건',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=page, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'))


def ms_data(value):
    if value.get('status') != 200 or not isinstance(value.get('data'), dict):
        raise CollectionError('Microsoft 공개 목록 오류 응답')
    return value['data']


async def microsoft(tools, company):
    jobs, seen, total, complete, unresolved, start = [], set(), None, False, 0, 0
    for _ in range(50):
        data = ms_data((await tools.fetch('https://apply.careers.microsoft.com/api/pcsx/search', params={
            'domain': 'microsoft.com', 'query': '', 'location': 'Seoul, South Korea', 'start': start})).json())
        rows, count = data.get('positions'), data.get('count')
        if not isinstance(rows, list) or not isinstance(count, int):
            raise CollectionError('Microsoft 검색 총건수·목록 미확인')
        if total is not None and count != total:
            break
        total = count
        for row in rows:
            ident = str(row['id'])
            if ident in seen:
                raise CollectionError('Microsoft 목록 페이지 중복')
            seen.add(ident)
            if not any('KR' in loc or 'Korea' in loc for loc in row.get('standardizedLocations', [])+row.get('locations', [])):
                raise CollectionError('Microsoft 한국 위치 필터 응답 불일치')
            url = urljoin(company['url'], row['positionUrl'])
            job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                      employer_verified=True, title=row['name'], external_id=ident,
                      url=url, source_url=company['url'], location=' / '.join(row.get('locations', [])))
            try:
                detail = ms_data((await tools.fetch('https://apply.careers.microsoft.com/api/pcsx/position_details',
                    params={'position_id': ident, 'domain': 'microsoft.com', 'hl': 'en'})).json())
                if str(detail.get('id')) != ident or detail.get('name') != job.title:
                    raise CollectionError('Microsoft 상세 ID 불일치')
                job.body = soup_text(detail.get('jobDescription', ''))
                employment = detail.get('efcustomTextEmploymentType', '')
                job.employment_type = ' / '.join(employment) if isinstance(employment, list) else str(employment or '')
                job.verified = verified_jd_body(job.body)
                job.status = 'open'
                unresolved += not job.verified
            except Exception:
                unresolved += 1
            jobs.append(job)
        start += len(rows)
        if len(seen) >= total:
            complete = len(seen) == total
            break
        if not rows:
            break
    return Collection(company['id'], jobs, 'ok' if complete and not unresolved else 'partial',
                      f'공식 Seoul 검색 {len(seen)}/{total}건; 상세 JD {sum(j.verified for j in jobs)}건; 미확인 {unresolved}건',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=0, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'))


async def kdb(tools, company):
    jobs, seen, total, complete = [], set(), None, False
    for page in range(1, 51):
        payload = {'SEARCH_CONDITION': '', 'SEARCH_KEYWORD': '', 'SEARCH_CATEGORY': '',
                   'ORDER_TYPE': 'new', 'MODE': '', 'ITR_BLB_ID': 'STD226',
                   'HEADER_STD_WEB': {'REQ_PAGE_NO': page, 'PAGE_ROW_COUNT': '50',
                                      'NEXT_PAGE_YN': 'S' if page == 1 else 'M',
                                      'NEXTPAGDTT': 'P', 'GRID_NEXTKEY_ITR_CND': ''}}
        data = (await tools.fetch('https://recruit.kdb.co.kr/BOUBUF01R01.jct', 'POST',
                                 data={'_JSON_': quote(json.dumps(payload, ensure_ascii=False))})).json()
        rows, paging = data.get('STDLIST_REC'), data.get('HEADER_STD_WEB', {})
        count = paging.get('TOT_PAG_ROW_CNT')
        if data.get('COMMON_HEAD', {}).get('ERROR') or not isinstance(rows, list) or not isinstance(count, int):
            raise CollectionError('산업은행 공개 목록 오류 응답')
        if total is not None and count != total:
            break
        total = count
        for row in rows:
            ident = str(row['ITR_NAC_ID_MNG_SNO'])
            if ident in seen:
                raise CollectionError('산업은행 페이지 중복')
            seen.add(ident)
            body = soup_text(row.get('NAC_HTML_TXT_INF', '') or '')
            title = row['NAC_CONE_TTL']
            query = {'ITR_NAC_ID_MNG_SNO': ident, 'ITR_BLB_ID': 'STD226',
                     'SEARCH_CONDITION': 'ZZ', 'ORDER_TYPE': 'new', 'FIRST_MENU_ID': 'RCRARA01N00'}
            url = 'https://recruit.kdb.co.kr/BOUBUF02N00.act?'+urlencode({'NEXT_DATA': json.dumps(query)})
            jobs.append(Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                            employer_verified=True, external_id=ident, title=title, url=url, source_url=company['url'],
                            body=body, verified=verified_jd_body(body),
                            deadline=deadline_from_text(body),
                            status='closed' if re.search(r'합격자?\s*발표|결과\s*발표|대상자\s*발표', title) else 'unknown'))
        if len(seen) >= total:
            complete = len(seen) == total
            break
        if not rows:
            break
    return Collection(company['id'], jobs, 'ok' if complete else 'partial',
                      f'공식 채용공고 목록 {len(seen)}/{total}건; 본문 {sum(j.verified for j in jobs)}건; 전형 결과 공지 제외',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=page, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'))


async def kiwoom(tools, company):
    jobs, seen, complete = [], set(), False
    for page in range(1, 51):
        data = (await tools.fetch('https://bbn.kiwoom.com/ir/SEmpAncmListAjax', 'POST',
                                 data={'pageNo': page, 'pageSize': 10, '_reqAgent': 'ajax'})).json()
        rows = data.get('empAncmList')
        if not isinstance(rows, list):
            raise CollectionError('키움 공식 채용 목록 오류 응답')
        if not rows:
            complete = True
            break
        for row in rows:
            ident = str(row['seqno'])
            if ident in seen:
                raise CollectionError('키움 공식 목록 페이지 중복')
            seen.add(ident)
            body = soup_text(row.get('cont') or '')
            jobs.append(Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                            employer_verified=True, title=row['titl'], external_id=ident,
                            url='https://bbn.kiwoom.com/ir/VEmpAncmDetailView?seqno='+ident,
                            source_url=company['url'], deadline=portal_deadline(row.get('acptEndDt') or ''),
                            body=body, verified=verified_jd_body(body),
                            status='closed' if row.get('joinStatCode') == '1' else 'unknown'))
    unresolved = sum(not j.verified and not prefilter(j, tools.settings) and role_relevant(j, tools.settings) for j in jobs)
    return Collection(company['id'], jobs, 'ok' if complete and not unresolved else 'partial',
                      f'공식 채용 게시판 {len(jobs)}건 · {page}페이지(종료 페이지 포함); 관련 JD 미확인 {unresolved}건; 이미지 공고 추정 금지',
                      proof=CatalogProof(expected=len(seen), observed=len(seen), company_count=len(jobs),
                                         pages_observed=page, complete=complete,
                                         empty_confirmed=(len(seen) == 0 and complete), basis='official_catalog'))


async def shinhansec(tools, company):
    html = (await tools.fetch(company['url'])).text
    soup = BeautifulSoup(html, 'html.parser')
    cards = soup.select('.recruit_list__item[onclick*="goView"]')
    rows = (await tools.fetch('https://recruit.shinhansec.com/recruit/mainList.do', 'POST',
                              data={'searchType': 'A'})).json()
    if not isinstance(rows, list):
        raise CollectionError('신한투자증권 공식 목록 응답 오류')
    ids = {re.search(r"goView\('(\d+)'\)", c['onclick'])[1] for c in cards}
    if ids != {str(r['ANNO_ID']) for r in rows} or len(ids) != len(rows):
        raise CollectionError('신한투자증권 공개 화면·목록 ID 불일치')
    jobs, unresolved = [], 0
    for row in rows:
        ident = str(row['ANNO_ID'])
        job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                  employer_verified=True, title=row['ANNO_SUBJECT'], external_id=ident,
                  url='https://recruit.shinhansec.com/recruit/view.do?annoId='+ident, source_url=company['url'],
                  employment_type=row.get('WORK_STYLE_TEXT', ''), experience=row.get('REQ_TYPE_NM', ''),
                  deadline=portal_deadline(row.get('ACCEPT_END_YMD', '')))
        if not prefilter(job, tools.settings):
            try:
                detail = await tools.fetch('https://recruit.shinhansec.com/recruit/view.do', 'POST',
                                            data={'annoId': ident})
                job.body = soup_text(detail.text)
                job.verified = verified_jd_body(job.body)
                unresolved += not job.verified and not prefilter(job, tools.settings)
            except Exception:
                unresolved += 1
        jobs.append(job)
    return Collection(company['id'], jobs, 'ok' if not unresolved else 'partial',
                      f'공식 전체 목록·화면 ID {len(jobs)}건 대조; 고용형태 확인, 상세 미확인 {unresolved}건',
                      proof=CatalogProof(expected=len(rows), observed=len(ids), company_count=len(jobs),
                                         pages_observed=1, complete=True,
                                         empty_confirmed=(len(rows) == 0 and True), basis='official_catalog'))


async def careerlink(tools, company):
    from .browser_read import public_page
    replies = []
    async with public_page(tools, company['url'], on_response=lambda response: replies.append(response)
                           if '/v1/recruit/jd/list' in response.url else None) as page:
        successful = None
        for _ in range(12):
            await page.wait_for_timeout(500)
            for response in replies:
                try:
                    data = await response.json()
                    if response.status == 200 and data.get('code') == '0000':
                        successful = data['data']
                except Exception:
                    pass
            if successful is not None:
                break
        if successful is None:
            raise CollectionError('Careerlink 실제 공개 목록 응답 미확인; 화면의 기본 0건을 신뢰하지 않음')
        # API completion precedes React's commit; count/card reads must follow it.
        await page.wait_for_timeout(600)
        html = await page.content()
        actual = successful.get('coList', [])
        if not any(c.get('coNm') == company['name'] for c in actual):
            raise CollectionError('Careerlink 실제 채용 법인 불일치')
        api_rows = successful.get('rcrtList')
        if not isinstance(api_rows, list):
            raise CollectionError('Careerlink 공개 목록 배열 미확인')
    soup = BeautifulSoup(html, 'html.parser')
    counter = re.search(r'(\d+)\s*건\s*의\s*(?:채용)?\s*공고가\s*있습니다', soup.get_text(' ', strip=True))
    if not counter:
        raise CollectionError('Careerlink 공개 목록 총건수 미확인')
    cards = {urljoin(company['url'], a['href']): a for a in soup.select('a[href^="/jobs/RC"]')}
    total = int(counter[1])
    jobs, unresolved = [], 0
    for url, card in cards.items():
        text = plain(card.get_text(' ', strip=True))
        kind = '계약직' if '계약직' in text else ('정규직' if '정규직' in text else '')
        heading = card.select_one('h1,h2,h3,h4,h5,h6')
        title = plain(heading.get_text()) if heading else text
        dates = re.findall(r'\d{4}\.\d{2}\.\d{2}', text)
        job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                  employer_verified=True, title=title, url=url, source_url=company['url'],
                  external_id=url.rsplit('/', 1)[-1], employment_type=kind,
                  deadline=portal_deadline(dates[-1]) if dates else '')
        if not prefilter(job, tools.settings):
            try:
                job = await tools.detail(url, company, title)
                job.employment_type = kind
                unresolved += not job.verified
            except Exception:
                unresolved += 1
        jobs.append(job)
    return Collection(company['id'], jobs, 'ok' if len(cards) == total == len(api_rows) and not unresolved else 'partial',
                      f'공식 공개 목록 {len(cards)}/{total}건; 카드별 고용형태 확인; 상세 미확인 {unresolved}건',
                      proof=CatalogProof(expected=total, observed=len(cards), company_count=len(jobs),
                                         pages_observed=1, complete=len(cards) == total == len(api_rows),
                                         empty_confirmed=(total == 0 and len(cards) == total == len(api_rows)), basis='official_catalog'))


def reviewed_roles(content, company):
    path = ROOT/'config'/'reviewed_images.yaml'
    if not path.exists():
        return []
    ident = hashlib.sha256(content).hexdigest()
    matches = [r for r in yaml.safe_load(path.read_text(encoding='utf-8'))['reviews']
               if r['company_id'] == company['id'] and r['sha256'] == ident]
    return matches[0]['roles'] if len(matches) == 1 else []


async def meritzfire(tools, company):
    html = (await tools.fetch(company['url'])).text
    soup = BeautifulSoup(html, 'html.parser')
    anchors = {re.search(r"noticeView\('(\d+)'\)", a.get('onclick', ''))[1]: a
               for a in soup.select('[onclick*="noticeView"]')
               if re.search(r"noticeView\('(\d+)'\)", a.get('onclick', ''))}
    # This public header list is the same complete list used by the site carousel.
    async with browser_response(tools, company['url'], '/json.smart') as (_, response):
        data = await response.json()
    rows = data.get('body', {}).get('resultList')
    if not isinstance(rows, list) or set(anchors) != {str(r['RECU_SNO']) for r in rows}:
        raise CollectionError('메리츠화재 공식 목록·화면 공고 ID 불일치')
    jobs, unresolved, ocr_count = [], 0, 0
    for row in rows:
        ident = str(row['RECU_SNO'])
        card_text = anchors[ident].get_text(' ', strip=True)
        dates = re.findall(r'\d{4}\.\d{2}\.\d{2}', card_text)
        url = 'https://recruit.meritzfire.com/new/job/noticeView.do?RECU_SNO='+ident
        candidate = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                        employer_verified=True, title=row['TTL'], external_id=ident,
                        url=url, source_url=company['url'],
                        deadline=portal_deadline(dates[-1]) if dates else '')
        if prefilter(candidate, tools.settings):
            jobs.append(candidate)
            continue
        try:
            response = await tools.fetch(url)
            detail = BeautifulSoup(response.text, 'html.parser')
            # Read only images inside the job description, not profile/form help.
            images = [image['src'] for image in detail.select('img[src^="data:image/"]')]
            splits, recognized = [], []
            for src in images[:5]:
                content = base64.b64decode(src.split(',', 1)[1], validate=True)
                roles = reviewed_roles(content, company)
                for role in roles:
                    splits.append(candidate.model_copy(update={
                        'external_id': ident+'-'+role['id'], 'title': candidate.title+' · '+role['title'],
                        'employment_type': role['employment_type'], 'location': role['location'],
                        'experience': role.get('experience', ''), 'body': role['body'], 'verified': True,
                        'source_note': '이미지 원문을 직접 대조한 세부 직무; 원본 이미지 해시 일치 확인'}))
                if not roles:
                    from .ocr import read_image
                    try:
                        recognized.append(await asyncio.to_thread(read_image, content))
                        ocr_count += 1
                    except Exception:
                        unresolved += 1
            if splits:
                jobs.extend(splits)
            else:
                # OCR is discovery evidence until verified. It must not receive a fit score.
                candidate.body = plain(' '.join(recognized))
                candidate.source_note = 'Windows OCR 자동 추출·원문 대조 대기' if recognized else '이미지 JD 원문 확인 대기'
                jobs.append(candidate)
                unresolved += 1
        except Exception:
            jobs.append(candidate)
            unresolved += 1
    return Collection(company['id'], jobs, 'ok' if not unresolved else 'partial',
                      f'공식 공고 {len(rows)}건 ID 대조; 원문 대조 직무 {sum(j.verified for j in jobs)}건; '
                      f'로컬 OCR {ocr_count}장; 이미지·원문 확인 대기 {unresolved}건',
                      proof=CatalogProof(expected=len(rows), observed=len(anchors), company_count=len(jobs),
                                         pages_observed=1, complete=len(rows) == len(anchors),
                                         empty_confirmed=(len(rows) == 0 and len(rows) == len(anchors)), basis='official_catalog'))
