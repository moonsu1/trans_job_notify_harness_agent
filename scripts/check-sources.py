"""Collect selected public sources and refresh local evidence without model calls or mail."""
from __future__ import annotations

import argparse
import asyncio
import json

from filelock import FileLock

from career_scout.collectors import Collection, PublicTools
from career_scout.core import ROOT, data_dir, load_config, now, prefilter, role_relevant
from career_scout.reporting import build_report
from career_scout.storage import Store


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('companies', nargs='+', help='e.g. kia samsung_ds skhynix skt')
    args = parser.parse_args()
    settings, profile, companies = load_config()
    by_id = {c['id']: c for c in companies}
    if set(args.companies)-set(by_id):
        raise ValueError('설정에 없는 기업 ID')
    settings['_full_scan'] = True
    with FileLock(data_dir()/'run.lock', timeout=0):
        store = Store(data_dir()/'scout.sqlite3')
        tools = PublicTools(settings)
        run_id = store.start_run()
        semaphore = asyncio.Semaphore(3)

        async def one(company_id):
            async with semaphore:
                try:
                    return await asyncio.wait_for(tools.collect(by_id[company_id]), timeout=90)
                except Exception as exc:
                    return Collection(company_id, status='failed', detail=type(exc).__name__)

        try:
            results = await asyncio.gather(*(one(c) for c in args.companies))
            sources = []
            for result in results:
                store.source(run_id, result.company_id, result.status, result.detail, len(result.jobs))
                for job in result.jobs:
                    key = store.upsert(job)
                    reason = prefilter(job, settings)
                    if not reason and job.verified and not role_relevant(job, settings):
                        reason = 'AI Agent 개발·기획 직무 관련 근거 부족'
                    store.exclude(key, reason)
                sources.append({'company': result.company_id, 'status': result.status,
                                'detail': result.detail, 'jobs': len(result.jobs),
                                'verified': sum(j.verified for j in result.jobs)})
            summary = {'api_calls': 0, 'run_cost_usd': 0, 'sources': sources,
                       'warnings': ['지정한 기업의 공개 수집만 점검했습니다. 다른 기업 수집·새 후보의 모델 평가는 갱신하지 않았습니다.']}
            report = build_report(store, settings, profile, run_id, summary)
            store.finish_run(run_id, summary, 'complete' if all(r.status == 'ok' for r in results) else 'degraded')
            audit = dict(summary, run_id=run_id, checked_at=now().isoformat(), report=report['report'])
            (ROOT/'evaluation'/f'source-check-{now():%Y%m%d-%H%M%S}.json').write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(audit, ensure_ascii=False))
        finally:
            await tools.close()
            store.close()


if __name__ == '__main__':
    asyncio.run(main())
