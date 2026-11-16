from __future__ import annotations

import asyncio
import html as html_lib
import io
import ipaddress
import json
import re
import socket
from contextlib import suppress
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from protego import Protego

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .core import Job, canonical_url, plain, prefilter, role_relevant
from .health import CatalogProof

USER_AGENT = 'CareerScout/0.1 (personal public job monitor; no applications)'
DETAIL_PATTERN = (r'(?i)(/jobs?/[^/?#]+|/positions?/[^/?#]+|/recruit/(detail|view)|'
                  r'(detail|view|announceView|applyView|jobView|recruitView)\.(do|hc|kc)|'
                  r'jobnoticeSn=|/career/job-posting|jobs-view\?seq=|'
                  r'(job|recruit|notice|announce|posting)(Id|No|Seq|_id|_no|_seq)=)')
MIRROR_DOMAINS = ['work24.go.kr', 'kofia.or.kr', 'bzpp.co.kr', 'wanted.co.kr', 'recruit.wanted.jobs',
                  'jobkorea.co.kr', 'saramin.co.kr']
COMPANY_ALIASES = {'samsung_ds': ['삼성전자', 'Samsung Electronics'],
                   'samsung_research': ['삼성리서치', '삼성전자', 'Samsung Research'],
                   'skhynix': ['SK하이닉스', 'SK hynix'], 'skt': ['SK텔레콤', 'SK Telecom'],
                   'hyundai': ['현대자동차', 'Hyundai Motor'], 'kia': ['기아', 'Kia'],
                   'naver': ['네이버', 'NAVER'], 'kakao': ['카카오', 'Kakao'],
                   'coupang': ['쿠팡', 'Coupang'], 'daangn': ['당근', 'Daangn', 'Karrot'],
                   'toss': ['토스', '비바리퍼블리카', 'Toss'], 'krafton': ['크래프톤', 'Krafton'],
                   'google': ['Google', '구글'], 'microsoft': ['Microsoft', '마이크로소프트'],
                   'aws': ['Amazon', 'AWS', '아마존'], 'nvidia': ['NVIDIA', '엔비디아'],
                   'samsung_securities': ['삼성증권', 'Samsung Securities'],
                   'miraeasset': ['미래에셋증권', 'Mirae Asset Securities'],
                   'korea_investment': ['한국투자증권', 'Korea Investment'],
                   'kb': ['KB국민은행', '국민은행', 'Kookmin'], 'shinhan': ['신한은행', 'Shinhan']}


def employer_matches(text, company):
    def normalize(value):
        return re.sub(r'[\W_]', '', value.casefold())
    corpus = normalize(text)
    return any(normalize(alias) in corpus for alias in COMPANY_ALIASES.get(company['id'], [company['name']]))


class CollectionError(Exception):
    def __init__(self, message, *, http_status=None, retry_after=0):
        super().__init__(message)
        self.http_status, self.retry_after = http_status, retry_after


@dataclass
class Collection:
    company_id: str
    jobs: list[Job] = field(default_factory=list)
    status: str = 'partial'
    detail: str = ''
    proof: CatalogProof | None = None
    channels: list[dict] = field(default_factory=list)
    version: str = 'builtin'


def allowed_url(url, company):
    p = urlsplit(url)
    host = (p.hostname or '').lower()
    if (p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443)
            and canonical_url(url) in company.get('_verified_urls', ())):
        return True
    # KB Goodjob hosts third-party employers; the bank's footer is not employer evidence.
    if company['id'] == 'kb' and host == 'kbgoodjob.kbstar.com':
        return False
    if host in ('job-boards.greenhouse.io', 'boards.greenhouse.io'):
        if not company.get('board') or p.path.strip('/').split('/')[0] != company['board']:
            return False
    if host == 'boards-api.greenhouse.io':
        if not company.get('board') or not p.path.startswith('/v1/boards/'+company['board']+'/'):
            return False
    return (p.scheme in ('https', 'http') and not p.username and not p.password
            and p.port in (None, 80, 443)
            and any(host == d or host.endswith('.'+d) for d in company['domains']))


def public_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise CollectionError('허용되지 않은 URL')
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80))
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise CollectionError('공개 인터넷 주소만 허용')
    except socket.gaierror:
        raise CollectionError('호스트 주소 확인 실패') from None


def soup_text(html):
    # Greenhouse returns HTML-escaped rich text, sometimes escaped twice.
    for _ in range(2):
        if '&lt;' in html or '&amp;lt;' in html:
            html = html_lib.unescape(html)
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg']):
        tag.decompose()
    main = soup.select_one('main, article, [role=main]') or soup.body or soup
    return plain(main.get_text(' ', strip=True))


def verified_jd_body(body):
    signals = re.search(r'자격|담당업무|주요업무|주요 업무|우대|qualifications|responsibilities|requirements|'
                        r'함께\s*할\s*업무|함께\s*하고\s*싶어요', body, re.I)
    blocked = re.search(r'access denied|captcha|접근이 제한|보안문자', body, re.I)
    return len(body) >= 350 and bool(signals) and not blocked


def jsonld_jobs(html):
    soup = BeautifulSoup(html, 'html.parser')
    found = []

    def walk(value):
        if isinstance(value, dict):
            types = value.get('@type', [])
            if types == 'JobPosting' or isinstance(types, list) and 'JobPosting' in types:
                found.append(value)
            for v in value.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(tag.string or tag.get_text()))
        except (ValueError, TypeError):
            continue
    return found


def structured_job(value, company, fallback):
    locations = value.get('jobLocation', [])
    if isinstance(locations, dict):
        locations = [locations]
    location = []
    for loc in locations:
        address = loc.get('address', {}) if isinstance(loc, dict) else {}
        if isinstance(address, str):
            location.append(address)
        else:
            country = address.get('addressCountry', '')
            if isinstance(country, dict):
                country = country.get('name', '')
            location.append(plain(' '.join(str(address.get(k, '')) for k in
                                         ('streetAddress', 'addressLocality', 'addressRegion'))+' '+str(country)))
    employer = value.get('hiringOrganization', {})
    employer = employer.get('name', '') if isinstance(employer, dict) else str(employer)
    identifier = value.get('identifier', '')
    identifier = identifier.get('value', '') if isinstance(identifier, dict) else identifier
    salary = value.get('baseSalary')
    employment = value.get('employmentType', '')
    if isinstance(employment, list):
        employment = ', '.join(employment)
    return Job(company_id=company['id'], company=employer or company['name'], tier=company['tier'],
               url=urljoin(fallback, value.get('url') or fallback), source_url=company['url'],
               external_id=str(identifier or ''), title=value.get('title') or '제목 미확인',
               location=' / '.join(location), employment_type=employment,
               deadline=normalize_deadline(str(value.get('validThrough') or '')),
               compensation=json.dumps(salary, ensure_ascii=False) if salary and re.search(r'\d', json.dumps(salary)) else '',
               body=soup_text(value.get('description') or ''),
               verified=bool(value.get('description')), status='unknown')


def normalize_deadline(value):
    return re.sub(r'^(\d{4})\.(\d{2})\.(\d{2})', r'\1-\2-\3', value)


def deadline_from_text(text):
    date_pattern = r'20\d{2}(?:[./-]\d{1,2}[./-]\d{1,2}|\d{4})'
    for match in re.finditer(r'접수\s*기간|모집\s*기간|지원\s*기간|접수\s*마감일?|지원서\s*접수\s*마감|마감일', text):
        window = text[match.end():match.end()+130]
        dates = list(re.finditer(date_pattern, window))
        if not dates or dates[0].start() > 35:
            continue
        selected = dates[0] if '마감' in match[0] else dates[min(1, len(dates)-1)]
        parts = re.sub(r'\D', '', selected[0])
        if len(parts) != 8:
            nums = re.findall(r'\d+', selected[0])
            if len(nums) != 3:
                continue
            parts = f'{int(nums[0]):04}{int(nums[1]):02}{int(nums[2]):02}'
        try:
            return datetime.strptime(parts, '%Y%m%d').date().isoformat()
        except ValueError:
            continue
    return ''


def sk_detail_metadata(html):
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.select_one('.box-title')
    values = {}
    for item in soup.select('.box-detail-item'):
        label, value = item.select_one('.label'), item.select_one('.value')
        if label and value:
            values[plain(label.get_text())] = plain(value.get_text())
    deadline = ''
    period = values.get('지원 기간', '')
    if '~' in period:
        end = re.sub(r'\([^)]*\)', '', period.split('~')[-1]).strip()
        for fmt in ('%B %d, %Y', '%Y.%m.%d', '%Y-%m-%d', '%Y/%m/%d'):
            try:
                deadline = datetime.strptime(end, fmt).date().isoformat()
                break
            except ValueError:
                continue
    return {'title': plain(title.get_text()) if title else '', 'company': values.get('회사', ''),
            'location': values.get('지역', ''), 'employment_type': values.get('유형', ''),
            'experience': values.get('구분', ''), 'deadline': deadline}


def explicit_closed(text):
    return bool(re.search(r'마감된\s*공고입니다|해당\s*공고는\s*마감되었습니다|본\s*공고는\s*마감되었습니다|'
                          r'this (?:job|position) is no longer (?:available|accepting applications)|'
                          r'this (?:job|position) has been (?:closed|filled)', text, re.I))


class PublicTools:
    def __init__(self, settings, browser=True):
        self.settings = settings
        self.browser = browser
        self.http = httpx.AsyncClient(timeout=15, follow_redirects=False,
                                      headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
        self.robots = {}
        self.domain_locks = {}
        self._route_tasks = set()
        from .evidence import EvidenceVault
        self.vault = EvidenceVault()
        self.health_store = None

    async def close(self):
        pending = [task for task in self._route_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.http.aclose()

    def evidence_for(self, company):
        return self.vault.for_company(company['id']) or [r for r in self.vault.records.values()
                                                       if allowed_url(r['url'], company)]

    async def _raw(self, method, url, **kwargs):
        for _ in range(5):
            await asyncio.to_thread(public_url, url)
            response = await self.http.request(method, url, **kwargs)
            if any(t in response.headers.get('content-type', '') for t in ('html', 'json', 'xml', 'text/plain')):
                encoding = 'cp949' if re.search(br'charset\s*=\s*["\x27]?\s*(euc-kr|ks_c_5601-1987|cp949)', response.content[:4096], re.I) else response.encoding or 'utf-8'
                self.vault.capture(str(response.url), response.content.decode(encoding, errors='replace'), status=response.status_code,
                                   request={'method': method, 'json': kwargs.get('json')})
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('location', ''))
                # Never follow a redirect to a non-public host; checked on the next loop.
                if response.status_code == 303:
                    method, kwargs = 'GET', {}
                continue
            if response.status_code >= 400:
                retry = response.headers.get('retry-after', '0')
                retry_seconds = int(retry) if retry.isdigit() else 0
                if not retry.isdigit():
                    from email.utils import parsedate_to_datetime
                    from datetime import timezone
                    with suppress(ValueError, TypeError):
                        retry_seconds = max(0, int((parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()))
                raise CollectionError(f'HTTP {response.status_code}', http_status=response.status_code,
                                      retry_after=retry_seconds)
            if len(response.content) > 12_000_000:
                raise CollectionError('문서 용량 제한 초과')
            return response
        raise CollectionError('리다이렉트 한도 초과')

    async def permitted(self, url):
        p = urlsplit(url)
        root = f'{p.scheme}://{p.netloc}'
        if root not in self.robots:
            try:
                response = await self._raw('GET', root+'/robots.txt')
                robot = Protego.parse(response.text)
                self.robots[root] = robot
            except CollectionError as e:
                # RFC 9309 2.3.1.3: a robots resource returning 4xx is unavailable.
                # This does not override HTTP errors on the actual job resource.
                code = re.fullmatch(r'HTTP (\d{3})', str(e))
                if code and 400 <= int(code[1]) < 500 and int(code[1]) != 429:
                    self.robots[root] = None
                else:
                    raise CollectionError('robots 확인 실패: '+str(e)) from None
        robot = self.robots[root]
        if robot and not robot.can_fetch(url, USER_AGENT):
            raise CollectionError('robots.txt 접근 제한')

    async def fetch(self, url, method='GET', **kwargs):
        lock = self.domain_locks.setdefault(urlsplit(url).netloc, asyncio.Lock())
        async with lock:
            await self.permitted(url)
            for attempt in range(3):
                try:
                    result = await self._raw(method, url, **kwargs)
                    # Several Korean Incruit pages declare EUC-KR only in HTML,
                    # with no charset in the HTTP header. HTTPX otherwise assumes UTF-8.
                    if 'charset=' not in result.headers.get('content-type', '').lower():
                        encoding = re.search(br'charset\s*=\s*["\x27]?\s*(euc-kr|ks_c_5601-1987|cp949)',
                                             result.content[:4096], re.I)
                        if encoding:
                            result.encoding = 'cp949'
                    await asyncio.sleep(0.25)
                    return result
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt == 2:
                        raise CollectionError('네트워크 타임아웃/연결 실패') from None
                    await asyncio.sleep(attempt+1)
                except CollectionError as exc:
                    if exc.http_status not in (429, 500, 502, 503, 504) or attempt == 2 or exc.retry_after > 10:
                        raise
                    await asyncio.sleep(max(exc.retry_after, attempt+1))

    async def render(self, url):
        if not self.browser:
            raise CollectionError('브라우저 수집 비활성')
        await self.permitted(url)
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(user_agent=USER_AGENT)

                await page.route('**/*', self.route_public)
                response = await page.goto(url, wait_until='domcontentloaded', timeout=20000)
                if response and response.status >= 400:
                    raise CollectionError(f'브라우저 HTTP {response.status}')
                await page.wait_for_timeout(2000)
                return await page.content()
            finally:
                if 'page' in locals():
                    await page.unroute_all(behavior='ignoreErrors')
                await browser.close()

    async def route_public(self, route):
        task = asyncio.current_task()
        self._route_tasks.add(task)
        # Keep the owning Playwright event task alive until teardown. The library
        # can dispose its page emitter before an aborted route's Future resolves.
        # This only retains a reference; it does not alter Playwright internals.
        route_impl = getattr(route, '_impl_obj', None)
        for owner in asyncio.all_tasks():
            coro = owner.get_coro()
            frame = getattr(coro, 'cr_frame', None)
            if (route_impl is not None and frame and frame.f_locals.get('route') is route_impl
                    and owner not in self._route_tasks):
                self._route_tasks.add(owner)
                owner.add_done_callback(self._route_tasks.discard)
        try:
            if (route.request.resource_type in ('image', 'font', 'media')
                    or urlsplit(route.request.url).hostname in ('www.googletagmanager.com', 'www.google-analytics.com')):
                await route.abort()
                return
            await asyncio.to_thread(public_url, route.request.url)
            await self.permitted(route.request.url)
            await route.continue_()
        except Exception:
            # Navigation/closing may dispose the route while a robots check is in flight.
            with suppress(Exception):
                await route.abort()
        finally:
            self._route_tasks.discard(task)

    async def detail(self, url, company, title='') -> Job:
        if not allowed_url(url, company):
            raise CollectionError('기업 허용 도메인 밖 URL')
        if urlsplit(url).hostname == 'recruit.incruit.com' and '/job/' in urlsplit(url).path:
            from .legacy_finance import read_incruit
            return await read_incruit(self, url, company, title)
        if 'recruiter.co.kr' in urlsplit(url).netloc and urlsplit(url).path == '/app/jobnotice/view':
            from .jobflex import read_legacy
            return await read_legacy(self, url, company, title)
        if company.get('adapter') in ('hyundai', 'kia') and urlsplit(url).path in ('/apply/applyView.hc', '/apply/applyView.kc'):
            from .hyundai import read_detail
            return await read_detail(self, url, company)
        if company.get('adapter') == 'samsungcareers' and urlsplit(url).path.rstrip('/') == '/hr':
            from .portals import read_samsung_detail
            return await read_samsung_detail(self, url, company)
        r = await self.fetch(url)
        if not allowed_url(str(r.url), company):
            raise CollectionError('공식 도메인 밖으로 이동')
        original = company.get('_official_domains', company['domains'])
        mirror = not allowed_url(url, dict(company, domains=original))
        if mirror and not employer_matches(soup_text(r.text), company):
            raise CollectionError('재게시 공고의 실제 채용 법인 근거 없음')
        if 'application/pdf' in r.headers.get('content-type', '') or urlsplit(url).path.lower().endswith('.pdf'):
            reader = PdfReader(io.BytesIO(r.content))
            if len(reader.pages) > 100:
                raise CollectionError('PDF 페이지 제한 초과')
            body = plain(' '.join(p.extract_text() or '' for p in reader.pages))
            return Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                       url=url, source_url=company['url'], title=title or 'PDF 채용 JD', body=body,
                       verified=bool(body), organization=company.get('organization', ''))
        html = r.text
        if 'skcareers.com' in urlsplit(url).netloc and '/Recruit/Detail/' in urlsplit(url).path:
            meta = sk_detail_metadata(html)
            if not meta['company'] or not meta['title']:
                html = await self.render(url)
                meta = sk_detail_metadata(html)
            if meta['company'] and meta['title']:
                body = plain(' '.join(v for v in meta.values() if v)+' '+soup_text(html))
                return Job(company_id=company['id'], company=meta['company'], tier=company['tier'],
                           url=url, source_url=company['url'], title=meta['title'], body=body,
                           location=meta['location'], employment_type=meta['employment_type'],
                           experience=meta['experience'], deadline=meta['deadline'],
                           employer_verified=True, verified=len(body) >= 350)
        structured = jsonld_jobs(html)
        if len(structured) == 1:
            job = structured_job(structured[0], company, str(r.url))
            if len(job.body) < 200:
                job.body = soup_text(html)
                job.verified = len(job.body) >= 350
            if not job.deadline:
                job.deadline = deadline_from_text(soup_text(html))
            if explicit_closed(soup_text(html)):
                job.status = 'closed'
            if allowed_url(job.url, company):
                job.source_kind = 'mirror' if mirror else 'official'
                return job
        if len(soup_text(html)) < 350:
            if 'recruiter.co.kr' in urlsplit(url).netloc:
                try:
                    await self.permitted('https://api-recruiter.recruiter.co.kr/design/v2')
                except CollectionError:
                    raise CollectionError('공식 상세 본문이 동적 API 접근 제한으로 미확인') from None
            html = await self.render(url)
            structured = jsonld_jobs(html)
            if len(structured) == 1:
                return structured_job(structured[0], company, url)
        body = soup_text(html)
        soup = BeautifulSoup(html, 'html.parser')
        heading = soup.select_one('h1') or soup.select_one('title')
        og_title = soup.select_one('meta[property="og:title"]')
        if not title and og_title and og_title.get('content'):
            title = og_title['content']
        title = title or (heading.get_text(' ', strip=True) if heading else '제목 미확인')
        verified = verified_jd_body(body)
        # Generic pages cannot establish that a job is open merely from HTTP 200.
        return Job(company_id=company['id'], company=company['name'], tier=company['tier'], url=url,
                   source_url=company['url'], title=plain(title), body=body, verified=verified,
                   status='closed' if explicit_closed(body) else 'unknown',
                   deadline=deadline_from_text(body),
                   source_kind='mirror' if mirror else 'official',
                   organization=company.get('organization', ''))

    async def collect(self, company) -> Collection:
        from .evidence import SOURCE
        token = SOURCE.set(company['id'])
        try:
            if self.health_store and self.settings.get('maintenance_enabled', True):
                from .maintenance import Registry
                registry = Registry(self.health_store.store, self.settings)
                result = await registry.execute(self, company)
                if result is not None:
                    return result
            return await self._collect_builtin(company)
        finally:
            SOURCE.reset(token)

    async def _collect_builtin(self, company) -> Collection:
        try:
            if company.get('primary_timeout_seconds'):
                result = await asyncio.wait_for(self.collect_primary(company), company['primary_timeout_seconds'])
            else:
                result = await self.collect_primary(company)
        except TimeoutError:
            result = Collection(company['id'], status='failed', detail='공식 채널 응답 시간 초과; 대체 공개 채널 확인')
        if company.get('finance_category'):
            from .finance_collectors import supplement_kofia
            result = await supplement_kofia(self, company, result)
        if result.status != 'ok':
            from .reposts import supplement
            result = await supplement(self, company, result)
        return result

    async def collect_primary(self, company) -> Collection:
        try:
            adapter = company.get('adapter', 'generic')
            if adapter == 'hmsec':
                from .hmsec import collect
                return await collect(self, company)
            if adapter == 'kb_group':
                from .kb_group import collect
                return await collect(self, company)
            if adapter in ('kt_group', 'incruit_current', 'woori_group', 'daishin_board'):
                from . import legacy_finance
                reader = {'kt_group': legacy_finance.kt, 'incruit_current': legacy_finance.incruit,
                          'woori_group': legacy_finance.woori, 'daishin_board': legacy_finance.daishin}[adapter]
                return await reader(self, company)
            if adapter in ('amazon', 'kakao', 'successfactors', 'nh', 'ibk', 'ibks', 'kyobo', 'ninehire', 'maintenance',
                           'google', 'microsoft', 'kdb', 'kiwoom', 'shinhansec', 'careerlink', 'meritzfire'):
                from .catalogs import collect
                return await collect(self, company)
            if adapter in ('mobis', 'korea_investment', 'kakaobank'):
                from .finance_collectors import collect_special
                return await collect_special(self, company)
            if adapter in ('hyundai', 'kia'):
                from .hyundai import collect
                return await collect(self, company)
            if adapter == 'samsungcareers':
                from .portals import collect_samsung
                return await collect_samsung(self, company)
            if adapter == 'greenhouse':
                return await self.greenhouse(company)
            if adapter == 'workday':
                return await self.workday(company)
            if adapter == 'naver':
                return await self.naver(company)
            if adapter == 'skcareers':
                return await self.skcareers(company)
            if company.get('finance_category') and 'recruiter.co.kr' in urlsplit(company['url']).netloc:
                from .finance_collectors import collect_jobflex
                return await collect_jobflex(self, company)
            return await self.generic(company)
        except Exception as e:
            from .errors import safe_text
            detail = str(e) if isinstance(e, CollectionError) else safe_text(type(e).__name__+': '+str(e))
            return Collection(company['id'], status='failed', detail=detail)

    async def naver(self, company):
        jobs, ids = [], set()
        complete = False
        for offset in range(0, 200, 10):
            r = await self.fetch('https://recruit.navercorp.com/rcrt/loadJobList.do',
                                 params={'firstIndex': offset})
            values = r.json().get('list', [])
            if not values:
                complete = True
                break
            fresh = [v for v in values if str(v['annoId']) not in ids]
            if not fresh:
                break
            for v in fresh:
                ids.add(str(v['annoId']))
                candidate = Job(company_id=company['id'], company=v.get('sysCompanyCdNm') or company['name'],
                                tier=company['tier'], url='https://recruit.navercorp.com/rcrt/view.do?annoId='+str(v['annoId']),
                                title=v['annoSubject'], experience=v.get('entTypeCdNm', ''),
                                employment_type=v.get('empTypeCdNm', ''))
                if not role_relevant(candidate, self.settings) or prefilter(candidate, self.settings):
                    continue
                try:
                    job = await self.detail(candidate.url, company, candidate.title)
                    job.external_id = str(v['annoId'])
                    job.experience = candidate.experience
                    job.employment_type = candidate.employment_type or job.employment_type
                    job.deadline = normalize_deadline(v.get('endYmdTime', '')) or job.deadline
                    jobs.append(job)
                except Exception:
                    jobs.append(candidate)
        return Collection(company['id'], jobs, 'ok' if complete else 'partial',
                          f'공개 목록 API {len(ids)}건 확인; 관련 {len(jobs)}건; '
                          +('전체 페이지 확인' if complete else '목록 상한/반복 응답으로 범위 제한'),
                      proof=CatalogProof(expected=len(ids), observed=len(ids), company_count=len(jobs),
                                         pages_observed=0, complete=complete,
                                         empty_confirmed=(len(ids) == 0 and complete), basis='official_catalog'))

    async def skcareers(self, company):
        from .portals import collect_sk
        return await collect_sk(self, company)

    async def greenhouse(self, company):
        board = company['board']
        url = f'https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true'
        response = await self.fetch(url)
        payload = response.json()
        values = payload.get('jobs')
        if (not isinstance(values, list) or payload.get('meta', {}).get('total') != len(values)
                or len({v['id'] for v in values}) != len(values)):
            raise CollectionError('Greenhouse 공식 전체 목록 건수·ID 불일치')
        jobs = []
        for v in values:
            job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                      url=v['absolute_url'], source_url=url, external_id=str(v['id']), title=v['title'],
                      location=v.get('location', {}).get('name', ''),
                      body=soup_text(v.get('content', '')), status='open', employer_verified=True)
            job.verified = verified_jd_body(job.body)
            if role_relevant(job, self.settings) and not prefilter(job, self.settings):
                jobs.append(job)
        unresolved = sum(not j.verified for j in jobs)
        return Collection(company['id'], jobs, 'partial' if unresolved else 'ok',
                          f'공식 Greenhouse 전체 {len(values)}/{payload["meta"]["total"]}건 · 관련 {len(jobs)}건; JD 미확인 {unresolved}건',
                      proof=CatalogProof(expected=len(values), observed=len(values), company_count=len(jobs),
                                         pages_observed=1, complete=True,
                                         empty_confirmed=(len(values) == 0 and True), basis='official_catalog'))

    async def workday(self, company):
        host = urlsplit(company['url']).netloc
        base = f'https://{host}/wday/cxs/{company["tenant"]}/{company["board"]}'
        jobs, total, seen, pages = [], None, set(), 0
        page_limit = 10000 if self.settings.get('_full_scan') else 100
        for offset in range(0, page_limit, 20):
            response = await self.fetch(base+'/jobs', 'POST', json={
                'appliedFacets': {}, 'limit': 20, 'offset': offset, 'searchText': 'Korea'})
            data = response.json()
            if not isinstance(data.get('total'), int) or not isinstance(data.get('jobPostings'), list):
                raise CollectionError('Workday 목록 응답 필드 변경')
            if total is not None and total != data['total']:
                raise CollectionError('Workday 페이지 총건수 불일치')
            total, pages = data['total'], pages+1
            for item in data['jobPostings']:
                if item['externalPath'] in seen:
                    raise CollectionError('Workday 공고 ID 중복')
                seen.add(item['externalPath'])
                url = f'https://{host}/en-US/{company["board"]}{item["externalPath"]}'
                job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                          url=url, source_url=company['url'], title=item['title'], external_id=item['externalPath'],
                          location=item.get('locationsText', ''))
                if role_relevant(job, self.settings) and not prefilter(job, self.settings):
                    try:
                        detail = (await self.fetch(base+item['externalPath'])).json().get('jobPostingInfo', {})
                        job.body = soup_text(detail.get('jobDescription', ''))
                        job.external_id = detail.get('jobReqId', '')
                        job.employment_type = detail.get('timeType', '')
                        job.verified = bool(job.body)
                        job.status = 'open'
                    except Exception:
                        pass
                jobs.append(job)
            if offset+20 >= total:
                break
        complete = total is not None and len(seen) == total
        return Collection(company['id'], jobs, 'ok' if complete else 'partial',
                          f'Korea 검색 {total}건, 최대 {page_limit}건 확인; 목록 {len(seen)}건',
                          proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                             pages_expected=(total+19)//20 if total else 1, pages_observed=pages,
                                             complete=complete, empty_confirmed=total == 0, basis='workday_total'))

    async def generic(self, company):
        response = await self.fetch(company['url'])
        html = response.text
        if '시스템 개선 작업 안내' in soup_text(html):
            return Collection(company['id'], status='failed',
                              detail='공식 채용 사이트 시스템 개선 작업 중: 접속 불가·채용 재공고 예정 안내 확인; 공고 없음으로 판단하지 않음')
        values = jsonld_jobs(html)
        links = self.list_links(html, company)
        if not values and not links:
            try:
                html = await self.render(company['url'])
                values = jsonld_jobs(html)
                links = self.list_links(html, company)
            except Exception as e:
                return Collection(company['id'], status='failed',
                                  detail='목록 해석 불가 / '+(str(e) if isinstance(e, CollectionError) else type(e).__name__))
        jobs = []
        errors = 0
        for value in values:
            job = structured_job(value, company, company['url'])
            if allowed_url(job.url, company) and role_relevant(job, self.settings):
                jobs.append(job)
        limit = len(links) if self.settings.get('_full_scan') else self.settings['max_candidates_per_source']
        # Relevant titles first, followed by uninformative links that may contain a matching JD.
        links.sort(key=lambda x: not role_relevant(Job(company_id=company['id'], company=company['name'],
                                                     tier=company['tier'], url=x[0], title=x[1]), self.settings))
        for url, title in links[:limit]:
            if any(canonical_url(j.url) == canonical_url(url) for j in jobs):
                continue
            try:
                job = await self.detail(url, company, title)
                if role_relevant(job, self.settings):
                    jobs.append(job)
            except Exception:
                errors += 1
        if values:
            status = 'partial' if errors or len(links) > limit else 'ok'
        elif links:
            # Generic lists cannot prove complete pagination coverage.
            status = 'partial'
        else:
            status = 'failed'
        return Collection(company['id'], jobs, status,
                          f'구조화 JD {len(values)} / 상세 링크 {len(links)} / 오류 {errors}; '
                          '일반 목록은 페이지 전체 수집 보장 없음')

    @staticmethod
    def list_links(html, company):
        soup = BeautifulSoup(html, 'html.parser')
        result = {}
        pattern = company.get('link_pattern', DETAIL_PATTERN)
        for a in soup.select('a[href]'):
            url = urljoin(company['url'], a['href'])
            title = plain(a.get_text(' ', strip=True))
            if allowed_url(url, company) and re.search(pattern, url) and canonical_url(url) != canonical_url(company['url']):
                result.setdefault(canonical_url(url), (url, title))
        # Common Korean career boards use javascript:fnView('posting-id').
        if company['id'] == 'naver':
            for a in soup.select('[onclick]'):
                match = re.search(r'(?:(?:go|fn|open)?(?:Detail|View)|show)\s*\(\s*[\'"]?(\d+)',
                                  a.get('onclick', ''), re.I)
                if match:
                    url = 'https://recruit.navercorp.com/rcrt/view.do?annoId='+match[1]
                    result[canonical_url(url)] = (url, plain(a.get_text(' ', strip=True)))
        return list(result.values())


def search_sources(response, companies):
    """Only trust tool-returned source URLs, never URLs invented in output_text."""
    result = []
    seen = set()
    for output in response.output:
        if getattr(output, 'type', '') != 'web_search_call':
            continue
        action = getattr(output, 'action', None)
        for source in getattr(action, 'sources', None) or []:
            url = getattr(source, 'url', '') if not isinstance(source, dict) else source.get('url', '')
            if not url or url in seen:
                continue
            seen.add(url)
            path = urlsplit(url).path.lower()
            if any(part in path for part in ('/job_intro/', '/division_intro/', '/new_recruits/', '/hub/')):
                continue
            if not (re.search(DETAIL_PATTERN, url) or re.search(r'(?i)/hr/.*[?&]no=\d+|apply_view\.jsp|'
                    r'/view\.do\?.*seq=|empDetailAuthView|/biz/(businessDetailView|businessView)|'
                    r'/Recruit/GI_Read/\d+|/rec_idx/\d+|/w[d]?/\d+', url)):
                continue
            for company in companies:
                if allowed_url(url, company):
                    # A shared Samsung/SK portal is not sufficient to attribute a legal employer.
                    result.append((company, url))
    return result
