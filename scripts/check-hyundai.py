"""Check Hyundai public-source coverage and update the local report, without paid calls/mail."""
import asyncio
import json

from filelock import FileLock

from career_scout.collectors import PublicTools
from career_scout.core import data_dir, load_config, prefilter, role_relevant
from career_scout.reporting import build_report
from career_scout.storage import Store


async def main():
    settings, profile, companies = load_config()
    company = next(c for c in companies if c['id'] == 'hyundai')
    settings['_full_scan'] = True
    with FileLock(data_dir()/'run.lock', timeout=0):
        store = Store(data_dir()/'scout.sqlite3')
        tools = PublicTools(settings)
        run_id = store.start_run()
        try:
            result = await tools.collect(company)
            store.source(run_id, company['id'], result.status, result.detail, len(result.jobs))
            relevant = []
            for job in result.jobs:
                key = store.upsert(job)
                reason = prefilter(job, settings)
                if not reason and job.verified and not role_relevant(job, settings):
                    reason = 'AI Agent 개발·기획 직무 관련 근거 부족'
                store.exclude(key, reason)
                if not reason:
                    relevant.append({'id': key, 'title': job.title, 'url': job.url, 'verified': job.verified})
            summary = {'api_calls': 0, 'run_cost_usd': 0, 'source': result.detail, 'candidates': relevant,
                       'warnings': ['현대차 수집만 재점검했습니다. 다른 기업의 수집·모델 평가는 갱신하지 않았습니다.']}
            report = build_report(store, settings, profile, run_id, summary)
            store.finish_run(run_id, summary, 'complete' if result.status == 'ok' else 'degraded')
            print(json.dumps(dict(summary, run_id=run_id, source_status=result.status, report=report['report']),
                             ensure_ascii=False))
        finally:
            await tools.close()
            store.close()


if __name__ == '__main__':
    asyncio.run(main())
