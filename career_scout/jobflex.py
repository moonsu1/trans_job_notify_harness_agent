"""Public Jobflex catalogs and original editor frames; never sign in or apply."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .browser_read import jobflex_request, public_page
from .collectors import Collection, CollectionError, allowed_url, soup_text, verified_jd_body
from .core import Job, plain, prefilter


def normalized(text):
    return re.sub(r'주식회사|\(주\)|[^a-z0-9가-힣]', '', text.casefold())


def parse_page(data):
    paging, rows = data.get('pagination', {}), data.get('list')
    if (not isinstance(rows, list) or not isinstance(paging.get('totalCount'), int)
            or not isinstance(paging.get('totalPages'), int) or not isinstance(paging.get('page'), int)
            or paging['totalCount'] < 0 or paging['totalPages'] < 0 or paging['page'] < 1):
        raise CollectionError('Jobflex 공개 목록·총건수 응답 미확인')
    ids = [str(row.get('positionSn', '')) for row in rows]
    if any(not i.isdigit() for i in ids) or len(ids) != len(set(ids)):
        raise CollectionError('Jobflex 공고 ID 누락·중복')
    return rows, paging


def apply_detail(job, detail):
    if detail.get('title') != job.title or not isinstance(detail.get('jobDescription'), str):
        raise CollectionError('Jobflex 목록·상세 제목 또는 본문 불일치')
    job.body = soup_text(detail['jobDescription'])
    job.verified = verified_jd_body(job.body)
    if detail.get('submissionStatus') == 'POST_SUBMISSION':
        job.status = 'closed'
    if detail.get('endDateTime'):
        job.deadline = detail['endDateTime']+'+09:00'
    job.source_note = '익명 공개 브라우저의 실제 목록·상세 API 대조'
    return job


async def read_reviewed_image(tools, job, detail, company):
    from .catalogs import reviewed_roles
    from urllib.parse import urljoin
    for image in BeautifulSoup(detail['jobDescription'], 'html.parser').select('img[src]')[:5]:
        job.source_note = '이미지 JD: 원문 대조 대기'
        url = urljoin(job.url, image['src'])
        if not allowed_url(url, company):
            continue
        # Visible assets of the already open public browser page. Normal HTTP
        # status, DNS, redirect and size guards still apply; no cookies or login.
        response = await tools._raw('GET', url)
        if not response.headers.get('content-type', '').startswith('image/'):
            continue
        roles = reviewed_roles(response.content, company)
        if len(roles) == 1:
            role = roles[0]
            job.body = plain(role['body'])
            job.employment_type, job.location = role['employment_type'], role['location']
            job.verified = verified_jd_body(job.body)
            job.source_note = '공식 이미지 원문을 직접 대조한 기록과 SHA-256 일치; '+url
            return


async def collect(tools, company):
    jobs, seen, total, complete, unresolved = [], set(), None, False, 0
    async with public_page(tools, company['url']) as page:
        design = await jobflex_request(page, '/design/v2?languageType=KOR')
        brand = design.get('title', '')
        names = [company['name']]+company.get('portal_aliases', [])
        if not any(normalized(name) in normalized(brand) for name in names):
            raise CollectionError('Jobflex 공개 포털 실제 회사 불일치: '+str(brand)[:60])
        for number in range(1, 101):
            data = await jobflex_request(page, '/position/v1/jobflex', {
                'pageableRq': {'page': number, 'size': 100, 'sort': ['CREATED_DATE_TIME']},
                'filter': {'keyword': '', 'tagSnList': [], 'jobGroupSnList': [], 'careerTypeList': [],
                           'regionSnList': [], 'submissionStatusList': [], 'openStatusList': [],
                           'resumeLanguageTypeList': []}})
            rows, paging = parse_page(data)
            if paging['page'] != number or (total is not None and total != paging['totalCount']):
                break
            total = paging['totalCount']
            for row in rows:
                ident = str(row['positionSn'])
                if ident in seen:
                    raise CollectionError('Jobflex 페이지 간 공고 중복')
                seen.add(ident)
                if company.get('portal_employer_prefix') and not row['title'].startswith(company['portal_employer_prefix']):
                    if 'DB그룹' in row['title'] and row.get('submissionStatus') != 'POST_SUBMISSION':
                        unresolved += 1  # A live group ad needs legal-employer/role separation.
                    continue
                end = row.get('endDateTime') or ''
                root = 'https://'+urlsplit(page.url).netloc
                job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                          employer_verified=True, external_id=ident, title=row['title'],
                          url=root+'/career/jobs/'+ident, source_url=company['url'],
                          status='closed' if row.get('submissionStatus') == 'POST_SUBMISSION' else 'unknown',
                          deadline=end+'+09:00' if end else '',
                          experience='신입' if row.get('careerType') == 'NEW' else '')
                # Closed positions have known reasons; uninformative active titles
                # also need a detail read so an AI role hidden in a group ad is seen.
                if not prefilter(job, tools.settings):
                    try:
                        detail = await jobflex_request(page, '/position/v2/jobflex/'+ident)
                        apply_detail(job, detail)
                        if not job.verified:
                            await read_reviewed_image(tools, job, detail, company)
                        unresolved += not job.verified and not prefilter(job, tools.settings)
                    except Exception:
                        unresolved += 1
                jobs.append(job)
            if len(seen) >= total:
                complete = len(seen) == total
                break
            if not rows:
                break
    from .health import CatalogProof
    return Collection(company['id'], jobs, 'ok' if complete and not unresolved else 'partial',
                      f'공식 공개 브라우저 목록 {len(seen)}/{total}건 · 실제 법인 {len(jobs)}건; '
                      f'관련·미분류 JD 미확인 {unresolved}건; 실제 회사·페이지·상세 제목 대조',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=number, complete=complete,
                                         empty_confirmed=total == 0 and complete, basis='official_api'))


async def read_legacy(tools, url, company, title=''):
    async with public_page(tools, url) as page:
        await page.wait_for_timeout(1200)
        soup = BeautifulSoup(await page.content(), 'html.parser')
        headers = {plain(th.get_text()): plain(th.find_next_sibling('td').get_text())
                   for th in soup.select('th') if th.find_next_sibling('td')}
        actual_title = headers.get('공고명') or title
        if not actual_title or (title and actual_title != title):
            raise CollectionError('레거시 공개 공고 제목 불일치')
        frame = page.frame_locator('iframe#viewSmartEditor')
        try:
            body = await frame.locator('body').inner_text(timeout=8000)
        except Exception:
            raise CollectionError('공개 JD 에디터 프레임 본문 미확인') from None
        # Wait for the parent's editor to populate, rather than accepting the shell.
        if not verified_jd_body(body):
            await page.wait_for_timeout(1000)
            body = await frame.locator('body').inner_text(timeout=3000)
        return Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                   employer_verified=True, title=actual_title, url=url, source_url=company['url'],
                   body=plain(body), verified=verified_jd_body(body),
                   source_note='익명 공개 상세 화면의 SmartEditor 프레임 원문 확인')
