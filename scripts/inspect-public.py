"""Inspect specified anonymous public pages, without sign-in or applications."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from career_scout.browser_read import public_page
from career_scout.collectors import PublicTools
from career_scout.core import load_config


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pages', nargs='+', help='name=https://public.example/')
    args = parser.parse_args()
    root = Path('analysis/public-browser')
    tools = PublicTools(load_config()[0])

    async def one(item):
        name, url = item.split('=', 1)
        replies = []
        requests = []
        try:
            async with public_page(tools, url, on_request=lambda req: requests.append(
                    dict(url=req.url, method=req.method, body=req.post_data))
                    if req.resource_type in ('fetch', 'xhr') else None) as page:
                page.on('response', lambda response: replies.append(response)
                        if response.request.resource_type in ('fetch', 'xhr') else None)
                await page.wait_for_timeout(5000)
                html, text = await page.content(), await page.locator('body').inner_text()
                (root/(name+'.html')).write_text(html, encoding='utf-8')
                (root/(name+'.txt')).write_text(text, encoding='utf-8')
                records = []
                for index, response in enumerate(replies):
                    record = dict(url=response.url, status=response.status, method=response.request.method)
                    try:
                        data = await response.json()
                        (root/f'{name}-data-{index}.json').write_text(
                            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                        record['json_file'] = f'{name}-data-{index}.json'
                    except Exception:
                        pass
                    records.append(record)
                (root/(name+'-network.json')).write_text(json.dumps(records, indent=2), encoding='utf-8')
                (root/(name+'-requests.json')).write_text(json.dumps(requests, indent=2), encoding='utf-8')
                print(json.dumps(dict(name=name, url=page.url, text=text[:2500], responses=records),
                                 ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps(dict(name=name, error=str(exc)[:180]), ensure_ascii=False), flush=True)

    try:
        await asyncio.gather(*(one(item) for item in args.pages))
    finally:
        await tools.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
