"""Read the official public catalog through its ordinary menu/navigation forms."""
from .health import CatalogProof
from xml.etree import ElementTree

from .browser_read import public_page
from .collectors import Collection, CollectionError, verified_jd_body
from .core import Job, plain, prefilter
from .portals import portal_deadline


def xml_rows(text):
    try:
        root = ElementTree.fromstring(text.strip())
        table = root.find('./DATA')
        keys = [c.text or '' for c in table.findall('./HTR/TD')]
        if root.tag != 'SHEET' or not keys or (root.findtext('MESSAGE') or '').strip():
            raise ValueError
        rows = []
        for row in table.findall('TR'):
            values = [c.text or '' for c in row.findall('TD')]
            if len(keys) != len(values):
                raise ValueError
            rows.append(dict(zip(keys, values)))
        return rows
    except (ElementTree.ParseError, ValueError, AttributeError):
        raise CollectionError('현대차증권 공개 목록 XML 형식·오류 응답') from None


async def read_list(page, method, number=1):
    if method not in ('search01', 'getTotCnt', 'getPageCnt'):
        raise CollectionError('목록 조회 외 동작')
    fields = {'getTotCnt': ['CNT1'], 'getPageCnt': ['PAGE_CNT'],
              'search01': ['C_CD', 'RE_NO', 'NOTI_SEQ_NO', 'TITLE', 'RCP_END_YMD', 'RCP_END_HMS',
                           'RE_SCR_CLASS_NM', 'SUPT_YN']}
    # The public frontend appends its navigation/CSRF fields. Let that same
    # read-only helper do so; never construct or persist access tokens ourselves.
    return await page.evaluate('''({method, number, keys}) => new Promise((resolve,reject) => {
        const timer = setTimeout(()=>reject(Error('Public catalog read timeout')),10000);
        document.f1.S_DSCLASS.value='biz.rem.apply.recruit.Apply_list';
        document.f1.S_DSMETHOD.value=method; document.f1.S_PAGE_NO.value=number;
        const form = DomUtil.serializeForm('f1');
        AjaxUtil.ajaxRequestXSProg('biz.rem.apply.recruit.Apply_list',method,form,function(xs) {
            clearTimeout(timer);
            resolve(Array.from({length:xs.RowCount()},(_,i)=>Object.fromEntries(
                keys.map(key=>[key,String(xs.GetCellValue(i,key))]))));
        });
    })''', {'method': method, 'number': str(number), 'keys': fields[method]})


async def menu(page):
    await page.evaluate("menu('anno')")
    # The site's temporary wait document is aborted before its real navigation;
    # wait for the list DOM instead of accepting that intermediate event.
    await page.wait_for_function("document.f1 && document.f1.S_DSCLASS.value.endsWith('Apply_list') && document.querySelector('#allList')",
                                 timeout=15000)


async def collect(tools, company):
    jobs, seen, unresolved = [], set(), 0
    async with public_page(tools, company['url']) as page:
        await menu(page)
        counts = await read_list(page, 'getTotCnt')
        pages = await read_list(page, 'getPageCnt')
        total, last = int(counts[0]['CNT1']), int(pages[0]['PAGE_CNT'])
        if not 0 <= last <= 100:
            raise CollectionError('현대차증권 공개 페이지 수 오류')
        rows = []
        for number in range(1, last+1):
            for row in await read_list(page, 'search01', number):
                ident = row['C_CD']+'-'+row['RE_NO']+'-'+row['NOTI_SEQ_NO']
                if ident in seen or any(not row[k].isdigit() for k in ('C_CD', 'RE_NO', 'NOTI_SEQ_NO')):
                    raise CollectionError('현대차증권 목록 페이지 중복·ID 오류')
                seen.add(ident)
                rows.append((ident, row))
        if len(seen) != total:
            raise CollectionError('현대차증권 전체 목록 건수 불일치')
        for ident, row in rows:
            job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                      title=row['TITLE'], external_id=ident, url=company['url'], url_is_catalog=True,
                      source_url=company['url'], employer_verified=True,
                      experience=row['RE_SCR_CLASS_NM'],
                      deadline=portal_deadline(row['RCP_END_YMD']+row['RCP_END_HMS']),
                      status='closed' if row['SUPT_YN'] != 'Y' else 'unknown',
                      source_note='공식 목록 링크 · 상세는 채용공고 메뉴에서 제목 선택 · 공고번호 '+row['RE_NO'])
            if not prefilter(job, tools.settings):
                try:
                    if jobs:
                        await menu(page)
                    await page.evaluate("r => doAction('view','page',r.C_CD,r.RE_NO,r.NOTI_SEQ_NO)", row)
                    await page.wait_for_function("title => !document.querySelector('#allList') && document.body?.innerText.includes(title)",
                                                 arg=job.title, timeout=6000)
                    await page.wait_for_timeout(350)
                    text = await page.locator('body').inner_text()
                    if job.title not in plain(text):
                        raise CollectionError('현대차증권 상세 제목 불일치')
                    job.body = plain(text)
                    job.verified = verified_jd_body(job.body)
                    unresolved += not job.verified
                except Exception:
                    unresolved += 1
            jobs.append(job)
    return Collection(company['id'], jobs, 'partial' if unresolved else 'ok',
                      f'공식 공개 목록 {len(seen)}/{total}건 · {last}페이지 대조; 현재 후보 JD 미확인 {unresolved}건',
                      proof=CatalogProof(expected=total, observed=len(seen), company_count=len(jobs),
                                         pages_observed=last, complete=len(seen) == total,
                                         empty_confirmed=(total == 0 and len(seen) == total), basis='official_catalog'))
