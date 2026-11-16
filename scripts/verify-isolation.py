"""Post-install smoke test. Generated parsers are executed only inside Docker."""
from __future__ import annotations

import asyncio
import json

from career_scout.core import data_dir, load_config, now
from career_scout.maintenance import Registry
from career_scout.repair_runtime import DockerRunner, RepairRejected, ReplayBroker, sandbox_fingerprint
from career_scout.storage import Store

BROKEN = '''import json
def collect(api, company):
    r = api.fetch(company['url'])
    values = json.loads(r['text'])['old_jobs']
    return {'pages': [], 'jobs': values}
'''
FIXED = '''import json
def collect(api, company):
    r = api.fetch(company['url'])
    data = json.loads(r['text'])
    key = 'old_jobs' if 'old_jobs' in data else 'postings'
    def ref(path):
        return {'evidence_id':r['id'], 'pointer':path}
    jobs = [{k:ref('/'+key+'/'+str(i)+'/'+k) for k in ('id','title','url','body')}
            for i, row in enumerate(data[key])]
    return {'pages':[{'page':1,'total':ref('/total'),'ids':ref('/'+key+'/*/id')}], 'jobs':jobs}
'''


async def main():
    settings, _, companies = load_config()
    company = next(c for c in companies if c['id'] == 'kakao')
    root = data_dir()/'maintenance'
    runner = DockerRunner(root, settings.get('repair_image', 'career-scout-parser:1'))
    runner.settings = settings
    if not await runner.ready(require_attestation=False):
        raise RuntimeError('Docker engine/image unavailable')
    job = dict(id='repair-smoke', title='AI Agent Engineer', url='https://careers.kakao.com/jobs/repair-smoke',
               body='담당업무: LLM Agent 및 RAG 서비스 개발. 필수 자격: Python. 우대사항: 서비스 운영. '*20)
    def broker(key, jobs):
        value = dict(id='a'*32, url=company['url'], status=200, kind='http',
                     text=json.dumps({'total':len(jobs),key:jobs}, ensure_ascii=False))
        return ReplayBroker([value])
    failed = False
    try:
        await runner.run(BROKEN, company, broker('postings', [job]), timeout=20)
    except RepairRejected:
        failed = True
    if not failed:
        raise RuntimeError('Broken parser unexpectedly passed')
    for key in ('old_jobs', 'postings'):
        result = await runner.run(FIXED, company, broker(key, [job]))
        if len(result.jobs) != 1 or not result.jobs[0].verified:
            raise RuntimeError('Fixed parser did not preserve original JD')
    empty = await runner.run(FIXED, company, broker('postings', []))
    if not empty.proof.empty_confirmed:
        raise RuntimeError('Verified empty catalog failed')
    # The installed project tests remain host-owned and cannot be changed by a parser.
    store = Store(data_dir()/'scout.sqlite3')
    try:
        await Registry(store, settings, runner=runner).regression()
    finally:
        store.db.close()
    evidence = dict(passed=True, image_id=runner.image_id, fingerprint=sandbox_fingerprint(), at=now().isoformat(),
                    checks=['broken_schema_rejected','old_and_new_schema_preserved','genuine_zero','protected_regressions'])
    (root/'sandbox-validation.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence))


if __name__ == '__main__':
    asyncio.run(main())
