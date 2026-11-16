"""Read Hyundai's public browser list/detail requests through its normal entry flow."""
from __future__ import annotations

from .health import CatalogProof

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from .collectors import Collection, CollectionError, USER_AGENT, soup_text
from .core import Job, KST, plain, prefilter

LIST_API = 'rec/AP-HM-FO-02700'
VISIBILITY_API = 'rec/AP-HM-FO-02900'
DETAIL_API = 'rec/AP-HM-FO-02800'
IDENTITY = ('recuYy', 'recuType', 'recuCls')


def portal(company):
    kia = company['id'] == 'kia'
    return {'base': 'https://career.kia.com' if kia else 'https://talent.hyundai.com',
            'suffix': 'kc' if kia else 'hc', 'prefix': 'KM' if kia else 'HM',
            'hgrCd': 2 if kia else 1, 'name': company['name']}


def endpoint(company, number):
    return f'rec/AP-{portal(company)["prefix"]}-FO-{number}'


def identity(value):
    result = {k: str(value.get(k) or '') for k in IDENTITY}
    if not all(result.values()):
        raise CollectionError('현대차 공고 식별자 누락')
    return result


def data_payload(payload):
    if not isinstance(payload, dict) or payload.get('status') != 200 or not isinstance(payload.get('data'), dict):
        raise CollectionError('현대차 공개 조회 응답 형식/상태 오류')
    return payload['data']


def parse_job(value, company):
    ids = identity(value)
    site = portal(company)
    url = site['base']+'/apply/applyView.'+site['suffix']+'?'+urlencode(ids)
    channel = plain(value.get('channelCodeNm') or '')
    title = plain(value.get('recuNoticeNm') or '')
    if not title:
        raise CollectionError('현대차 공고 제목 누락')
    fields = [('조직소개', 'aboutTeamNtc'), ('직무상세', 'privJdDtl'),
              ('지원자격', 'privMustReq'), ('우대사항', 'prefReq'), ('공고 본문', 'recuNoticeWebCont')]
    parts = [(label, soup_text(value.get(key) or '')) for label, key in fields]
    body = plain(' '.join(label+' '+text for label, text in parts if text))
    deadline = ''
    # Match the deadline shown to applicants; backend application grace periods
    # can differ by a few minutes and must not silently extend the visible deadline.
    display_end = bool(value.get('appDispEdDt'))
    raw_date = str(value.get('appDispEdDt' if display_end else 'applyEndDt') or '')
    raw_time = str(value.get('appDispEdTm' if display_end else 'applyEndTm') or '')
    if raw_date and not raw_date.startswith(('2999', '9999')):
        try:
            if raw_time:
                deadline = datetime.strptime(raw_date+raw_time, '%Y%m%d%H%M').replace(tzinfo=KST).isoformat()
            else:
                deadline = datetime.strptime(raw_date, '%Y%m%d').date().isoformat()
        except ValueError:
            pass
    location = ' / '.join(dict.fromkeys(plain(value.get(k) or '') for k in (
        'workPlaceCodeNm', 'workPlaceCodeNm2', 'workPlaceCodeNm3') if plain(value.get(k) or '')))
    # "경력" is a hiring channel, not proof of a permanent employment contract.
    employment = channel if any(t in channel for t in ('정규직', '계약', '인턴', '파견')) else ''
    verified = len(body) >= 200 and any(label in ('직무상세', '공고 본문') and len(text) >= 50
                                       for label, text in parts)
    metadata = plain(' '.join(x for x in (company['name'], title, location, channel) if x))
    return Job(company_id=company['id'], company=company['name'], tier=company['tier'],
               employer_verified=True, url=url, external_id='-'.join(ids.values()), source_url=company['url'],
               title=title, location=location, experience=channel, employment_type=employment,
               deadline=deadline, body=metadata+' '+body if body else '', verified=verified,
               status='open' if value.get('applyStartYn') == 'Y' else 'unknown')


@asynccontextmanager
async def session(tools, company):
    if not tools.browser:
        raise CollectionError(company['name']+' 정상 브라우저 진입 필요: 브라우저 수집 비활성')
    await tools.permitted(company['url'])
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(user_agent=USER_AGENT)
            await page.route('**/*', tools.route_public)
            # The site's entry script completes the queue and submits its own form.
            # Never persist or manufacture nfGubnC/queue/session credentials.
            async with page.expect_response(lambda r: urlsplit(r.url).path == '/api/'+endpoint(company, '02700'),
                                            timeout=35000) as first:
                await page.goto(company['url'], wait_until='domcontentloaded', timeout=30000)
            response = await first.value
            if response.status != 200:
                raise CollectionError(f'현대차 공개 목록 HTTP {response.status}')
            yield page, data_payload(await response.json())
        finally:
            if 'page' in locals():
                await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


async def get(page, endpoint, params):
    if endpoint not in (LIST_API, VISIBILITY_API, DETAIL_API,
                        'rec/AP-KM-FO-02700', 'rec/AP-KM-FO-02900', 'rec/AP-KM-FO-02800'):
        raise CollectionError('현대차 공개 읽기 Tool 허용 범위 밖')
    result = await page.evaluate('''async ({endpoint, params}) => {
        const response = await instance.get(endpoint, {params});
        return response.data;
    }''', {'endpoint': endpoint, 'params': params})
    return data_payload(result)


async def public_detail(page, value, company):
    params = dict(hgrCd=portal(company)['hgrCd'], lang='ko', **identity(value))
    visible = (await get(page, endpoint(company, '02900'), params)).get('applySecret')
    if not isinstance(visible, dict) or visible.get('recuNoticeSecretYn') != 'N':
        raise CollectionError('현대차 비공개/공개 여부 미확인 공고: 인증 없이 수집하지 않음')
    info = (await get(page, endpoint(company, '02800'), params)).get('applyInfo')
    if not isinstance(info, dict) or identity(info) != identity(value):
        raise CollectionError('현대차 상세 응답 식별자 불일치')
    if info.get('recuNoticeSecretYn') == 'Y':
        raise CollectionError('현대차 비공개 공고')
    return parse_job(dict(value, **{k: v for k, v in info.items() if v is not None}), company)


async def read_detail(tools, url, company):
    query = parse_qs(urlsplit(url).query)
    ids = identity({k: query.get(k, [''])[0] for k in IDENTITY})
    async with session(tools, company) as (page, _):
        return await public_detail(page, ids, company)


async def collect(tools, company):
    jobs, rows, seen = [], [], set()
    errors, skipped, detailed, pages, groups = 0, 0, 0, 1, 0
    issues = []
    async with session(tools, company) as (page, first):
        try:
            total = int(first['listCnt'])
            values = first['list']
            if total < 0 or not isinstance(values, list):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise CollectionError('현대차 공개 목록 총건수/목록 형식 오류') from None
        size = 10
        max_pages = 100 if tools.settings.get('_full_scan') else 20
        for number in range(1, min(max_pages, (total+size-1)//size)+1):
            if number > 1:
                try:
                    payload = await get(page, endpoint(company, '02700'),
                                        dict(hgrCd=portal(company)['hgrCd'], lang='ko', page=number, pageblock=size))
                    pages = number
                    if int(payload['listCnt']) != total:
                        issues.append('조회 중 목록 총건수 변경')
                    values = payload['list']
                    if not isinstance(values, list):
                        raise ValueError
                except Exception:
                    issues.append(f'{number}페이지 조회 실패')
                    break
            if not values and total:
                issues.append(f'{number}페이지가 예상보다 일찍 비어 있음')
                break
            for value in values:
                try:
                    key = tuple(identity(value).values())
                    if key in seen:
                        issues.append('목록 중복 공고 발견')
                        continue
                    seen.add(key)
                    rows.append(value)
                except (CollectionError, AttributeError):
                    issues.append('목록 공고 식별자 오류')
        if len(rows) != total:
            issues.append(f'총 {total}건 중 {len(rows)}건만 확인')
        semaphore = asyncio.Semaphore(3)

        async def one(value):
            nonlocal skipped, detailed, errors, groups
            try:
                candidate = parse_job(value, company)
            except CollectionError:
                errors += 1
                return None
            if prefilter(candidate, tools.settings):
                skipped += 1
                return candidate
            if value.get('groupYn') == 'Y':
                groups += 1
                return candidate
            async with semaphore:
                try:
                    job = await public_detail(page, value, company)
                    if job.verified:
                        detailed += 1
                    else:
                        errors += 1
                    return job
                except Exception:
                    errors += 1
                    return candidate

        jobs = [j for j in await asyncio.gather(*(one(value) for value in rows)) if j]
    status = 'partial' if issues or errors or groups else 'ok'
    detail = (f'공개 목록 {len(rows)}/{total}건 · {pages}페이지 확인 / 상세 JD {detailed}건 확인 / '
              f'지역·고용형태·직무 사전 제외 {skipped}건 / 상세 실패 {errors}건 / 그룹 공고 미확인 {groups}건')
    if issues:
        detail += ' / '+'; '.join(dict.fromkeys(issues))
    return Collection(company['id'], jobs, status, detail,
                      proof=CatalogProof(expected=total, observed=len(rows), company_count=len(jobs),
                                         pages_observed=pages, complete=not issues,
                                         empty_confirmed=(total == 0 and not issues), basis='official_catalog'))
