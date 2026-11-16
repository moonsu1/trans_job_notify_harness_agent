"""Re-fetch bodies previously missed by conversational qualification headings."""
from __future__ import annotations

import asyncio
from collections import Counter

from filelock import FileLock

from career_scout.collectors import PublicTools, verified_jd_body
from career_scout.core import Job, data_dir, load_config
from career_scout.storage import Store


async def main():
    settings, _, companies = load_config()
    by_id = {c['id']: c for c in companies}
    with FileLock(data_dir()/'run.lock', timeout=0):
        store = Store(data_dir()/'scout.sqlite3')
        tools = PublicTools(settings)
        stats = Counter()
        try:
            for row in store.rows():
                job = Job.model_validate_json(row['payload'])
                if not job.verified and verified_jd_body(job.body):
                    try:
                        repaired = await tools.detail(job.url, by_id[job.company_id], job.title)
                        store.upsert(repaired)
                        stats['verified' if repaired.verified else 'still_unverified'] += 1
                    except Exception as exc:
                        stats['failed'] += 1
                        print(row['id'], type(exc).__name__)
            print(dict(stats))
        finally:
            await tools.close()
            store.close()


if __name__ == '__main__':
    asyncio.run(main())
