"""Observe public career-page loading; no sign-in, applications, or paid model calls."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from career_scout.collectors import PublicTools, USER_AGENT
from career_scout.core import load_config

URLS = {'kia': 'https://career.kia.com/apply/applyList.kc',
        'samsung': 'https://www.samsungcareers.com/hr/?ty=B',
        'sk': 'https://www.skcareers.com/Recruit'}


async def main():
    root = Path('analysis/portals')
    root.mkdir(exist_ok=True)
    tools = PublicTools(load_config()[0])
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def one(name, url):
            page = await browser.new_page(user_agent=USER_AGENT)
            responses, errors = [], []
            page.on('response', lambda r: responses.append(r)
                    if urlsplit(r.url).hostname == urlsplit(url).hostname
                    and r.request.resource_type in ('fetch', 'xhr') else None)
            page.on('pageerror', lambda e: errors.append(str(e)[:100]))
            await page.route('**/*', tools.route_public)
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(6000)
                root.joinpath(name+'-browser.html').write_text(await page.content(), encoding='utf-8')
                root.joinpath(name+'-browser.txt').write_text(await page.locator('body').inner_text(), encoding='utf-8')
                summary = []
                for index, response in enumerate(responses):
                    path = urlsplit(response.url).path
                    record = {'status': response.status, 'method': response.request.method, 'path': path}
                    if not any(word in path.lower() for word in ('login', 'auth', 'user', 'member')):
                        record['url'] = response.url
                        record['request_body'] = response.request.post_data
                        try:
                            data = await response.json()
                            record['keys'] = list(data)[:12] if isinstance(data, dict) else ['array', len(data)]
                            root.joinpath(f'{name}-response-{index}.json').write_text(
                                json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                            record['file'] = f'{name}-response-{index}.json'
                        except Exception:
                            pass
                    summary.append(record)
                print(json.dumps({'site': name, 'url': page.url, 'responses': summary,
                                  'js_errors': errors, 'text': (await page.locator('body').inner_text())[:900]},
                                 ensure_ascii=False), flush=True)
            except Exception as exc:
                print(name, type(exc).__name__, flush=True)
            finally:
                await page.close()

        try:
            await asyncio.gather(*(one(name, url) for name, url in URLS.items()))
        finally:
            await browser.close()
            await tools.close()


if __name__ == '__main__':
    asyncio.run(main())
