"""Public financial hiring catalogs and association notices; never infer a permanent contract."""
from __future__ import annotations

from .health import CatalogProof

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .collectors import Collection, CollectionError, deadline_from_text
from .core import Job, plain, prefilter, role_relevant
from .portals import cached_catalog, portal_deadline


def normalized(value):
    return re.sub(r'주식회사|\(주\)|[^a-z0-9가-힣]', '', value.casefold())


async def collect_jobflex(tools, company):
    from .jobflex import collect
    return await collect(tools, company)


async def collect_special(tools, company):
    if company['id'] == 'mobis':
        return await collect_mobis(tools, company)
    if company['id'] == 'korea_investment':
        response = await tools.fetch('https://career.koreainvestment.com/annoucement',
                                     params={'company': '', 'annoType': ''})
        data = response.json()
        rows = data.get('annoList')
        total = data.get('annoCnt', {}).get('totCnt')
        if not isinstance(rows, list) or not isinstance(total, int):
            raise CollectionError('한국투자 공개 목록 형식 오류')
        target = [r for r in rows if r.get('companyName') == company['name']]
        # A public group catalog can return a trust/asset manager. Do not relabel it as the securities firm.
        return Collection(company['id'], status='partial' if target or len(rows) != total else 'ok',
                          detail=f'한국투자금융 그룹 공개 목록 {len(rows)}/{total}건 / 실제 한국투자증권 {len(target)}건'
                          +(' / 상세 조회 연결 확인 필요' if target else ''),
                          proof=CatalogProof(expected=total, observed=len(rows), company_count=len(target),
                                             pages_expected=1, pages_observed=1, complete=len(rows) == total,
                                             empty_confirmed=not target and len(rows) == total, basis='annoCnt.totCnt'))
    return await collect_kakaobank(tools, company)


async def collect_mobis(tools, company):
    response = await tools.fetch(company['url'])
    soup = BeautifulSoup(response.text, 'html.parser')
    body = plain(soup.get_text(' ', strip=True))
    links = [(urljoin(company['url'], a['href']), plain(a.get_text(' ', strip=True)))
             for a in soup.select('a[href*="jobs-view?seq="]')]
    # An explicit official zero count is different from an empty JS shell.
    zero = bool(re.search(r'0\s*개의?\s*채용\s*공고가\s*현재\s*진행', body))
    if not links and zero:
        return Collection(company['id'], status='ok', detail='공식 공개 목록에 현재 진행 공고 0건 명시 확인',
                          proof=CatalogProof(expected=0, observed=0, company_count=0, pages_expected=1,
                                             pages_observed=1, complete=True, empty_confirmed=True,
                                             basis='official_explicit_empty_label'))
    jobs, errors = [], 0
    for url, title in dict(links).items():
        try:
            jobs.append(await tools.detail(url, company, title))
        except Exception:
            errors += 1
    return Collection(company['id'], jobs, 'partial',
                      f'공식 목록 상세 링크 {len(links)}건 / 상세 실패 {errors}건; 전체 목록 완전성은 추가 확인 필요')


async def collect_kakaobank(tools, company):
    jobs, ids, total, pages, complete = [], set(), None, 0, False
    for number in range(1, 21):
        payload = {'pageNumber': number, 'pageSize': 20, 'recruitEmployeeTypes': ['정규직']}
        response = await tools.fetch('https://recruit.kakaobank.com/api/recruits', 'POST', json=payload)
        data = response.json()
        paging, rows = data.get('paging', {}), data.get('list')
        if not isinstance(rows, list) or paging.get('recruitEmployeeTypes') != ['정규직']:
            raise CollectionError('카카오뱅크 정규직 필터 적용 응답 확인 실패')
        count = paging.get('totalElements')
        if not isinstance(count, int) or (total is not None and total != count):
            break
        total, pages = count, number
        for row in rows:
            ident = str(row['recruitNoticeSn'])
            if ident in ids:
                continue
            ids.add(ident)
            url = row['recruitNoticeUrl']
            if not url.startswith('https://'):
                url = 'https://'+url
            candidate = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                            employer_verified=True, external_id=ident, url=url, source_url=company['url'],
                            title=row['recruitNoticeName'], employment_type='정규직',
                            organization=row.get('recruitClassName', ''),
                            deadline=row.get('receiveEndDatetime', '').replace(' ', 'T')+'+09:00'
                            if row.get('receiveEndDatetime') else '')
            if prefilter(candidate, tools.settings) or not role_relevant(candidate, tools.settings):
                jobs.append(candidate)
                continue
            try:
                detail_company = dict(company, domains=company['domains']+['kakaobank.recruiter.co.kr'])
                detailed = await tools.detail(url, detail_company, candidate.title)
                if detailed.verified:
                    detailed.employment_type = candidate.employment_type
                    detailed.external_id = candidate.external_id
                    detailed.deadline = candidate.deadline
                    detailed.body = plain('정규직 '+detailed.body)
                    candidate = detailed
            except Exception:
                pass
            jobs.append(candidate)
        if len(ids) >= total:
            complete = len(ids) == total
            break
        if not rows:
            break
    related = [j for j in jobs if not prefilter(j, tools.settings) and role_relevant(j, tools.settings)]
    unresolved = sum(not j.verified for j in related)
    return Collection(company['id'], jobs, 'partial' if unresolved or not complete else 'ok',
                      f'공식 정규직 필터 목록 {len(ids)}/{total}건 · {pages}페이지 / 관련 후보 {len(related)}건 / '
                      f'관련 JD 미확인 {unresolved}건; 목록만으로 적합성 점수 생성 안 함',
                      proof=CatalogProof(expected=total, observed=len(ids), company_count=len(jobs),
                                         pages_observed=pages, complete=complete,
                                         empty_confirmed=(total == 0 and complete), basis='official_catalog'))


async def kofia_catalog(tools):
    rows, seen, notes = [], set(), []
    for word in ('AI', 'LLM', 'Agent'):
        for page in range(1, 5):
            url = 'https://www.kofia.or.kr/brd/m_96/list.do'
            response = await tools.fetch(url, params={'srchWord': word, 'srchTp': '0', 'page': page})
            soup = BeautifulSoup(response.text, 'html.parser')
            table = soup.select_one('tbody')
            if not table:
                raise CollectionError('금융투자협회 공개 목록 요소 미확인')
            count = 0
            for tr in table.select('tr'):
                cells = tr.select('td')
                anchor = tr.select_one('a[href*="view.do?seq="]')
                if len(cells) < 5 or not anchor:
                    continue
                count += 1
                detail_url = urljoin(url, anchor['href'])
                ident_match = re.search(r'[?&]seq=(\d+)', detail_url)
                if not ident_match or ident_match[1] in seen:
                    continue
                seen.add(ident_match[1])
                rows.append({'company': plain(cells[1].get_text()), 'title': plain(cells[2].get_text()),
                             'url': 'https://www.kofia.or.kr/brd/m_96/view.do?seq='+ident_match[1],
                             'published': plain(cells[-1].get_text())})
            if count == 0:
                break
        notes.append(word+' 제목 검색 최대 4페이지')
    return rows, '; '.join(notes)


async def supplement_kofia(tools, company, primary):
    async def loader():
        return await kofia_catalog(tools)

    try:
        catalog, scope = await cached_catalog(tools, 'kofia', loader)
    except Exception as exc:
        primary.detail += ' / 협회 보완 수집 실패: '+type(exc).__name__
        return primary
    aliases = {normalized(company['name']), normalized(company.get('portal_company_name') or company['name'])}
    targets = [r for r in catalog if normalized(r['company']) in aliases]
    added, failed = 0, 0
    for row in targets:
        if any(j.url == row['url'] for j in primary.jobs):
            continue
        try:
            response = await tools.fetch(row['url'])
            soup = BeautifulSoup(response.text, 'html.parser')
            headers = {plain(th.get_text()): plain(th.find_next_sibling('td').get_text())
                       for th in soup.select('th') if th.find_next_sibling('td')}
            if normalized(headers.get('회원사명', '')) not in aliases:
                raise CollectionError('협회 공고 실제 회원사 불일치')
            content = soup.select_one('td.first.lastline')
            if not content:
                raise CollectionError('협회 공고 본문 미확인')
            body = plain(content.get_text(' ', strip=True))
            period = headers.get('접수기간', '')
            end = re.search(r'~\s*(\d{8})', period)
            job = Job(company_id=company['id'], company=row['company'], tier=company['tier'],
                      employer_verified=True, url=row['url'], source_url=company['url'], source_kind='mirror',
                      title=headers.get('제목') or row['title'], body=body,
                      deadline=portal_deadline(end[1]) if end else deadline_from_text(body),
                      verified=len(body) >= 200, status='unknown')
            primary.jobs.append(job)
            added += 1
        except Exception:
            failed += 1
    if added and primary.status == 'failed':
        primary.status = 'partial'
    primary.detail += f' / 금융투자협회 원문 {added}건·실패 {failed}건 ({scope}; 전체 시장 보장 아님)'
    return primary
