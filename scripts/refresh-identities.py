"""Repair already-stored shared-portal identities without any LLM calls."""
from __future__ import annotations

import asyncio
from collections import Counter

from filelock import FileLock

from career_scout.collectors import PublicTools, deadline_from_text
from career_scout.core import Job, data_dir, load_config, prefilter
from career_scout.storage import Store


async def refresh():
    settings, _, companies = load_config()
    by_id = {c['id']: c for c in companies}
    directory = data_dir()
    with FileLock(directory/'run.lock', timeout=0):
        store = Store(directory/'scout.sqlite3')
        tools = PublicTools(settings)
        stats = Counter()
        try:
            for row in store.rows():
                job = Job.model_validate_json(row['payload'])
                if 'skcareers.com' in job.url and '/Recruit/Detail/' in job.url:
                    try:
                        repaired = await tools.detail(job.url, by_id[job.company_id])
                        key = store.upsert(repaired)
                        reason = prefilter(repaired, settings)
                        store.exclude(key, reason)
                        stats['portal_refreshed'] += 1
                        stats['excluded_other_company_or_conditions' if reason else 'matching_employer'] += 1
                    except Exception as exc:
                        print('IDENTITY_REVIEW', row['id'], type(exc).__name__)
                        stats['refresh_failed'] += 1
                elif not job.deadline:
                    deadline = deadline_from_text(job.body)
                    if deadline:
                        job.deadline = deadline
                        key = store.upsert(job)
                        store.exclude(key, prefilter(job, settings))
                        # Only the deadline changed; the frozen AI-role assessment remains valid.
                        if row['assessment'] and row['fingerprint'] == settings['fingerprint']:
                            import json
                            store.save_analysis(key, json.loads(row['assessment']), row['fingerprint'])
                        stats['deadline_recovered'] += 1
            print(dict(stats))
        finally:
            await tools.close()
            store.close()


if __name__ == '__main__':
    asyncio.run(refresh())
