"""Official finance channels whose old recruiter subdomains are no longer current."""
from .health import CatalogProof
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .browser_read import public_page
from .collectors import Collection, CollectionError, soup_text, verified_jd_body
from .core import Job, plain, prefilter
from .portals import portal_deadline


async def kt(tools, company):
    # The public mobile catalog is the same KT group service, not a repost.
    async with public_page(tools, company['url']) as page:
        await page.wait_for_function("/총\\s*\\d+건의 채용/.test(document.body.innerText)", timeout=12000)
        html = await page.content()
    soup = BeautifulSoup(html, 'html.parser')
    counter = re.search(r'총\s*(\d+)\s*건의\s*채용', soup.get_text(' ', strip=True))
    data = (await tools.fetch('https://m.recruit.kt.com/api/recruit', params={
        'currentPage': 1, 'pageSize': 100, 'isInProgress': 'true', 'isContainsContents': 'true'})).json()
    rows = data.get('data')
    if data.get('isSuccess') is not True or not isinstance(rows, list) or not counter:
        raise CollectionError('KT그룹 공식 목록·총건수 미확인')
    ids = {r['recruitNoticeSn'] for r in rows}
    if len(ids) != len(rows) or len(rows) != int(counter[1]) or len(rows) >= 100:
        raise CollectionError('KT그룹 전체 목록 완전성 미확인')
    jobs, unresolved = [], 0
    for row in rows:
        if row.get('company') != company['name']:
            continue
        body = soup_text(row.get('contents') or '')
        job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                  employer_verified=True, title=row['recruitNoticeName'], external_id=str(row['recruitNoticeSn']),
                  url=row['recruitNoticeUrl'], source_url=company['url'],
                  experience=row.get('recruitClassName') or '', body=body, verified=verified_jd_body(body),
                  status='closed' if row.get('isTimeOver') else 'unknown',
                  deadline=(row.get('receiveEndDatetime') or '').replace(' ', 'T')+'+09:00'
                  if row.get('receiveEndDatetime') else '')
        if not prefilter(job, tools.settings) and not job.verified:
            try:
                detail_company = dict(company, domains=company['domains']+['kt.recruiter.co.kr'])
                detail = await tools.detail(job.url, detail_company, job.title)
                job.body, job.verified = detail.body, detail.verified
            except Exception:
                pass
            unresolved += not job.verified
        jobs.append(job)
    return Collection(company['id'], jobs, 'ok' if not unresolved else 'partial',
                      f'KT그룹 공식 전체 목록 {len(rows)}/{counter[1]}건 대조 · 실제 BC카드 {len(jobs)}건; '
                      f'현재 후보 JD 미확인 {unresolved}건; 원문 마감시각 확인',
                      proof=CatalogProof(expected=len(rows), observed=len(ids), company_count=len(jobs),
                                         pages_observed=1, complete=True,
                                         empty_confirmed=(len(rows) == 0 and True), basis='official_catalog'))


def incruit_content(html, company, url):
    soup = BeautifulSoup(html, 'html.parser')
    # Incruit publishes complete image transcripts in alt, which inner_text omits.
    transcripts = [i.get('alt', '') for i in soup.select('img[alt]') if len(i.get('alt', '')) >= 100]
    body = plain(soup_text(html)+' '+' '.join(transcripts))
    if company['name'] not in body:
        raise CollectionError('공식 Incruit 상세의 실제 기업명 미확인')
    title_match = re.search(r'20\d{2}년\s*'+re.escape(company['name'])+r'.{0,45}?채용', body)
    title = title_match[0] if title_match else company['name']+' 현재 채용공고'
    # Only extract from an explicit application-period clause, never interview dates.
    period = re.search(r'접수\s*기간\s*[:：]?\s*.{0,15}?(20\d{2})\s*(?:년|\.)\s*'
                       r'(\d{1,2})\s*(?:월|\.)\s*(\d{1,2}).{0,20}?[~～\-]\s*'
                       r'(?:20\d{2}\s*(?:년|\.)\s*)?(\d{1,2})\s*(?:월|\.)\s*(\d{1,2})', body)
    deadline = f'{period[1]}-{int(period[4]):02}-{int(period[5]):02}' if period else ''
    return Job(company_id=company['id'], company=company['name'], tier=company['tier'],
               employer_verified=True, title=title, url=url, source_url=company['url'],
               body=body, verified=verified_jd_body(body), deadline=deadline,
               source_note='공식 채용 포털의 현재 공고 및 공개 이미지 대체텍스트 원문 확인')


async def incruit(tools, company):
    async with public_page(tools, company['url']) as page:
        await page.wait_for_timeout(700)
        html, url = await page.content(), page.url
        if urlsplit(url).hostname != urlsplit(company['url']).hostname:
            raise CollectionError('Incruit 기업 전용 채용 포털 밖으로 이동')
        if not re.search(r'/hire/viewhire\.asp\?projectid=\d+', url, re.I):
            raise CollectionError('Incruit 현재 단일 공고로의 공식 목록 이동 미확인')
        job = incruit_content(html, company, url)
    return Collection(company['id'], [job], 'ok' if job.verified else 'partial',
                      '공식 채용공고 메뉴가 연결한 현재 단일 프로젝트 1건; 본문·이미지 대체텍스트 대조'
                      +('; 접수기간 미확인' if not job.deadline else ''),
                      proof=CatalogProof(expected=1, observed=1, company_count=1,
                                         pages_observed=1, complete=True,
                                         empty_confirmed=(1 == 0 and True), basis='official_catalog'))


async def read_incruit(tools, url, company, title):
    async with public_page(tools, url) as page:
        await page.wait_for_timeout(400)
        heading = await page.locator('body').inner_text()
        if not title or title not in plain(heading) or company['name'] not in heading:
            raise CollectionError('Incruit 상세 실제 회사·제목 불일치')
        frame = page.frame_locator('iframe[src*="/jobcont/"]')
        text = await frame.locator('body').inner_text(timeout=8000)
        alts = await frame.locator('img[alt]').evaluate_all(
            "images => images.map(i=>i.getAttribute('alt')).filter(a=>a.length>=100)")
        body = plain(title+' '+text+' '+' '.join(alts))
        period = re.search(r'(20\d{2})\s*\.\s*(\d+)\s*\.\s*(\d+)\s*\.\s*(\d\d:\d\d)\s*~\s*'
                           r'(20\d{2})\s*\.\s*(\d+)\s*\.\s*(\d+)\s*\.\s*(\d\d:\d\d)', heading)
        deadline = f'{period[5]}-{int(period[6]):02}-{int(period[7]):02}T{period[8]}:00+09:00' if period else ''
        return Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                   employer_verified=True, title=title, url=url, source_url=company['url'],
                   body=body, verified=verified_jd_body(body), deadline=deadline,
                   source_note='공식 공고의 공개 jobcont 프레임·이미지 대체텍스트 원문 확인')


async def woori(tools, company):
    jobs, seen, total = [], set(), None
    async with public_page(tools, company['url']) as page:
        for number in range(1, 31):
            if number > 1:
                response = await page.goto(company['url']+'?pageIndex='+str(number), wait_until='domcontentloaded')
                if response.status != 200:
                    raise CollectionError('우리금융 목록 페이지 HTTP '+str(response.status))
            await page.wait_for_function("/진행중인 채용 공고\\s*\\d+개/.test(document.body.innerText)", timeout=10000)
            html = await page.content()
            soup = BeautifulSoup(html, 'html.parser')
            text = soup.get_text(' ', strip=True)
            count = re.search(r'진행중인\s*채용\s*공고\s*(\d+)\s*개', text)
            if not count or (total is not None and total != int(count[1])):
                raise CollectionError('우리금융 공식 목록 총건수 미확인·변경')
            total = int(count[1])
            anchors = soup.select('a[onclick*="window.open"]')
            for anchor in anchors:
                match = re.search(r"window.open\('(https?:/{2,}[^']+)'", anchor.get('onclick', ''))
                if not match:
                    continue
                link = re.sub(r'^(https?:)/+', r'\1//', match[1])
                if link in seen:
                    raise CollectionError('우리금융 페이지 간 공고 중복')
                seen.add(link)
                # Actual legal employer is a sibling list column, not the group name.
                row = anchor.find_parent('div', class_='list')
                if not row:
                    raise CollectionError('우리금융 실제 법인 열 미확인')
                columns = row.select(':scope > .filings')
                if len(columns) < 2:
                    raise CollectionError('우리금융 법인 열 개수 미확인')
                if plain(columns[1].get_text()) != company['name']:
                    continue
                title = plain(anchor.get_text(' ', strip=True))
                job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                          employer_verified=True, title=title, url=link, source_url=company['url'])
                if not prefilter(job, tools.settings):
                    try:
                        job = await tools.detail(link, company, title)
                    except Exception:
                        pass
                jobs.append(job)
            if len(seen) >= total:
                if len(seen) != total:
                    raise CollectionError('우리금융 공식 목록 건수 불일치')
                unresolved = sum(not j.verified and not prefilter(j, tools.settings) for j in jobs)
                return Collection(company['id'], jobs, 'ok' if not unresolved else 'partial',
                                  f'우리금융 공식 전체 목록 {len(seen)}/{total}건 · 실제 우리은행 {len(jobs)}건; '
                                  f'정규직 후보 JD 미확인 {unresolved}건',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=number, complete=len(seen) == total,
                                         empty_confirmed=(total == 0 and len(seen) == total), basis='official_catalog'))
            if not anchors:
                break
    raise CollectionError('우리금융 전체 목록 확인 미완료')


async def daishin(tools, company):
    jobs, seen = [], set()
    for number in range(1, 51):
        response = await tools.fetch(company['url'], params={'boardseq': '263', 'page': number})
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('tr')
        added = 0
        for row in rows:
            anchor = row.select_one('a[href*="DW_Basic_Read.aspx"]')
            dates = re.findall(r'\d{4}-\d{2}-\d{2}', row.get_text())
            if not anchor or len(dates) != 2:
                continue
            url = urljoin(company['url'], anchor['href'])
            if url in seen:
                raise CollectionError('대신 공식 게시판 페이지 반복')
            seen.add(url)
            added += 1
            job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                      employer_verified=True, title=plain(anchor.get_text()), url=url,
                      source_url=company['url'], deadline=portal_deadline(dates[-1]),
                      status='closed' if '마감' in row.get_text() else 'unknown')
            if not prefilter(job, tools.settings):
                try:
                    job = await tools.detail(url, company, job.title)
                except Exception:
                    pass
            jobs.append(job)
        if not added:
            return Collection(company['id'], jobs, 'partial',
                              f'공식 기업 채용 게시판 {len(jobs)}건 전체 페이지 확인; 과거 공고만 게시되어 '
                              '현재 경력 채용은 금융투자협회 원문으로 추가 확인; 전체 경력 채널 보장 불가')
    raise CollectionError('대신 공식 채용 게시판 종료 페이지 미확인')
