"""Normal, anonymous public-page reading with strict network and action boundaries.

robots.txt is a crawler preference, not an authentication response. The HTTP
crawler still honors it. These targeted readers use the site's ordinary public
browser flow; actual 401/403/429/999, login pages and CAPTCHA remain failures.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qs, urlsplit

from .collectors import CollectionError, USER_AGENT, public_url

PUBLIC_POSTS = {
    '/position/v1/jobflex', '/position/v2/jobflex/setting',
    '/appsite/company/getMainView', '/appsite/company/getJobflexProductInfo',
    '/v1/recruit/jd/list', '/v1/vwr/enter',
}


def public_read_post(request):
    parsed = urlsplit(request.url)
    if parsed.path in PUBLIC_POSTS:
        return True
    if (parsed.hostname == 'recruit.hmsec.com'
            and parsed.path == '/rem/apply/recruit/apply_list.jsp'):
        return True  # Public catalog menu submits its navigation form.
    if parsed.hostname == 'recruit.hmsec.com' and parsed.path == '/reMenuAction.do':
        values = parse_qs(request.post_data or '')
        return values.get('X_POP_URL') in (['/rem/apply/recruit/apply_list.jsp'],
                                          ['/rem/apply/recruit/apply_view.jsp'])
    if parsed.hostname == 'recruit.hmsec.com' and parsed.path == '/Application.do':
        values = parse_qs(request.post_data or '')
        return ((values.get('S_DSCLASS') == ['biz.rem.apply.recruit.Apply_list']
                 and values.get('S_DSMETHOD') in (['search01'], ['getPageCnt'], ['getTotCnt'], ['getApplyCnt']))
                or (values.get('S_DSCLASS') == ['biz.rem.apply.recruit.Apply_view']
                    and values.get('S_DSMETHOD') == ['search01']))
    return False


@asynccontextmanager
async def public_page(tools, url, *, on_request=None, on_response=None):
    if not tools.browser:
        raise CollectionError('공개 페이지 브라우저 읽기 비활성')
    await asyncio.to_thread(public_url, url)
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=USER_AGENT)
        page._scout_tools = tools
        if on_response:
            page.on('response', on_response)

        async def route(request):
            # Playwright's page event emitter may release pending route tasks on
            # teardown. Retain their owners just as the HTTP reader does.
            route_impl = getattr(request, '_impl_obj', None)
            for owner in asyncio.all_tasks():
                frame = getattr(owner.get_coro(), 'cr_frame', None)
                if (route_impl is not None and frame and frame.f_locals.get('route') is route_impl
                        and owner not in tools._route_tasks):
                    tools._route_tasks.add(owner)
                    owner.add_done_callback(tools._route_tasks.discard)
            req = request.request
            if on_request:
                on_request(req)
            parsed = urlsplit(req.url)
            if (req.resource_type in ('image', 'font', 'media')
                    or any(x in (parsed.hostname or '') for x in ('sentry.', 'google-analytics.', 'googletagmanager.'))
                    or req.method not in ('GET', 'HEAD', 'OPTIONS', 'POST')
                    or (req.method == 'POST' and not public_read_post(req))):
                await request.abort()
                return
            try:
                await asyncio.to_thread(public_url, req.url)
                await request.continue_()
            except Exception:
                with suppress(Exception):
                    await request.abort()

        await page.route('**/*', route)
        try:
            response = await page.goto(url, wait_until='domcontentloaded', timeout=25000)
            if response and response.status >= 400:
                raise CollectionError(f'공개 브라우저 HTTP {response.status}')
            if '/cus/login' in page.url or '/admin' in page.url:
                raise CollectionError('공개 채용 페이지 대신 관리자·로그인 화면으로 이동')
            yield page
        finally:
            if hasattr(tools, 'vault'):
                with suppress(Exception):
                    tools.vault.capture(page.url, await page.content(), status=response.status if response else 0, kind='render')
            await page.unroute_all(behavior='ignoreErrors')
            await browser.close()


async def jobflex_request(page, path, payload=None):
    """Read-only API paths observed in the platform's public browser frontend."""
    import re
    if not (path.startswith('/design/v2?') or path == '/position/v1/jobflex'
            or re.fullmatch(r'/position/v2/jobflex/\d+', path)):
        raise CollectionError('공개 채용 목록·JD 읽기 경로 밖 요청')
    result = await page.evaluate('''async ({path, payload}) => {
        const response = await fetch("https://api-recruiter.recruiter.co.kr"+path, {
            method: payload === null ? "GET" : "POST",
            headers: payload === null ? {"prefix": window.location.hostname}
                : {"prefix": window.location.hostname, "Content-Type": "application/json"},
            body: payload === null ? undefined : JSON.stringify(payload),
            credentials: "omit"
        });
        return {status: response.status, text: await response.text()};
    }''', {'path': path, 'payload': payload})
    if hasattr(page, '_scout_tools') and hasattr(page._scout_tools, 'vault'):
        page._scout_tools.vault.capture('https://api-recruiter.recruiter.co.kr'+path, result['text'],
                                       status=result['status'], kind='jobflex',
                                       request={'payload': payload})
    if result['status'] != 200:
        raise CollectionError('공개 채용 API HTTP '+str(result['status']))
    import json
    try:
        return json.loads(result['text'])
    except ValueError:
        raise CollectionError('공개 채용 API의 JSON 응답 미확인') from None
