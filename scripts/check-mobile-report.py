"""Render the exact email HTML at phone widths, including stripped-style fallback."""
import asyncio
import json

from playwright.async_api import async_playwright

from career_scout.core import ROOT


async def main():
    path = ROOT/'reports'/'latest-email.html'
    checks = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for width in (320, 390, 430):
                page = await browser.new_page(viewport={'width': width, 'height': 1000}, device_scale_factor=1)
                await page.goto(path.as_uri())
                await page.evaluate('document.fonts.ready')
                for stripped in (False, True):
                    if stripped:
                        await page.evaluate("document.querySelectorAll('[style]').forEach(e => e.removeAttribute('style'))")
                    metrics = await page.evaluate('''() => ({
                        viewport: innerWidth, scroll: document.documentElement.scrollWidth,
                        tables: document.querySelectorAll('table').length,
                        textLength: document.body.innerText.length,
                        cards: document.querySelectorAll('h2').length
                    })''')
                    metrics.update(width=width, stripped_styles=stripped)
                    checks.append(metrics)
                    assert metrics['scroll'] <= width, metrics
                    assert metrics['tables'] == 0, metrics
                    if width == 390 and not stripped:
                        await page.screenshot(path=str(ROOT/'reports'/'mobile-preview.png'))
                await page.close()
        finally:
            await browser.close()
    (ROOT/'evaluation'/'mobile-layout-checks.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
    print(json.dumps(checks))


if __name__ == '__main__':
    asyncio.run(main())
