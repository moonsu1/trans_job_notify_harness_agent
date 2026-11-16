"""Current official KB group notices, resolved to the actual hiring corporation."""
from .health import CatalogProof
from urllib.parse import urlsplit

from .collectors import Collection, CollectionError, deadline_from_text, soup_text, verified_jd_body
from .core import Job
from .portals import cached_catalog


async def catalog(tools):
    rows, seen, total = [], set(), None
    for page in range(1, 101):
        data = (await tools.fetch('https://careers.kbfg.com/api/career/recruites', params={
            'page': page, 'pageSize': 100, 'totalCount': 0, 'totalPageCount': -1, 'ord': 'EDDT',
            'affcomCd': '', 'enggTypCd': '', 'carrTypCd': '', 'jbClsfiCd': ''})).json()
        value = data.get('result', {})
        batch, paging = value.get('recruties'), value.get('paging', {})
        if data.get('resultCode') != 200 or not isinstance(batch, list) or not isinstance(paging.get('totalCount'), int):
            raise CollectionError('KB그룹 공식 채용 목록·총건수 미확인')
        if total is not None and total != paging['totalCount']:
            raise CollectionError('KB그룹 페이지 사이 총건수 변경')
        total = paging['totalCount']
        for row in batch:
            if row['enggId'] in seen:
                raise CollectionError('KB그룹 페이지 공고 중복')
            seen.add(row['enggId'])
            rows.append(row)
        if len(rows) >= total:
            if len(rows) != total:
                raise CollectionError('KB그룹 목록 총건수 불일치')
            return rows, total
        if not batch:
            break
    raise CollectionError('KB그룹 전체 목록 확인 미완료')


async def collect(tools, company):
    rows, total = await cached_catalog(tools, 'kb-current-official', lambda: catalog(tools))
    jobs, unresolved = [], 0
    for row in rows:
        if row.get('affcomCd') != company['kb_code']:
            continue
        if row.get('affcomNm') != company['name']:
            raise CollectionError('KB그룹 채용 법인 코드·이름 불일치')
        body = soup_text(row.get('cn') or '')
        link = row.get('enggLink') or ''
        if not link.startswith('https://') or not any(
                (urlsplit(link).hostname or '') == d or (urlsplit(link).hostname or '').endswith('.'+d)
                for d in company['domains']):
            raise CollectionError('KB그룹 공식 지원 링크 허용 도메인 불일치')
        end = (row.get('enggEddt') or '').strip()
        job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                  employer_verified=True, external_id='kbgroup-'+str(row['enggId']),
                  title=row['enggTitl'], url=link, source_url='https://careers.kbfg.com/',
                  body=body, verified=verified_jd_body(body), employment_type=row.get('enggTypNm') or '',
                  experience=row.get('carrTypNm') or '', deadline=end or deadline_from_text(body),
                  source_note='공식 KB그룹의 실제 채용 법인 코드·공고 원문·지원 링크 대조')
        unresolved += not job.verified
        jobs.append(job)
    return Collection(company['id'], jobs, 'ok' if not unresolved else 'partial',
                      f'KB그룹 공식 목록 {len(rows)}/{total}건, 실제 {company["name"]} {len(jobs)}건; JD 미확인 {unresolved}건',
                      proof=CatalogProof(expected=total, observed=len(rows), company_count=len(jobs),
                                         pages_observed=1, complete=len(rows) == total,
                                         empty_confirmed=(total == 0 and len(rows) == total), basis='official_catalog'))
