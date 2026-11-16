"""Real-JD evaluation: reviewers supply labels; synthetic labels never count as accuracy."""
from __future__ import annotations

import json
from pathlib import Path

from .agent import Agent
from .core import Job, assessment_view, now, prefilter, role_relevant
from .storage import BudgetExceeded


def export_cases(store, path: Path, count=20):
    candidates = []
    for row in store.rows():
        job = Job.model_validate_json(row['payload'])
        if job.verified and job.body and role_relevant(job, {'role_keywords': []}):
            candidates.append(job)
    candidates.sort(key=lambda j: (j.tier, j.company_id, j.title))
    # Spread employers first, then fill in stable order; retain the full frozen JD.
    selected, used = [], set()
    for job in candidates:
        if job.company_id not in used:
            selected.append(job)
            used.add(job.company_id)
    selected += [j for j in candidates if j not in selected]
    records = [{'job': j.model_dump(), 'expected': None, 'reference_reason': ''} for j in selected[:count]]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    return len(records)


def evaluate(store, settings, profile, path: Path):
    cases = json.loads(path.read_text(encoding='utf-8'))
    if len(cases) < 20 or any(c.get('expected') not in ('recommend', 'exclude', 'review')
                              or not c.get('reference_reason') for c in cases):
        raise ValueError('실제 JD 최소 20건의 expected(recommend/exclude/review)와 reference_reason을 먼저 검토·기입하세요')
    agent = Agent(settings, profile, store)
    if not agent.client:
        raise ValueError('OpenAI 인증 등록 후 실제 모델 평가를 실행하세요')
    results = []
    for case in cases:
        job = Job(**case['job'])
        reason = prefilter(job, settings)
        if reason:
            predicted, details = 'exclude', reason
        else:
            try:
                a = agent.analyze(job)
            except BudgetExceeded:
                break
            view = assessment_view(a, job.verified)
            predicted = ('exclude' if a.hard_failures or a.scope_failure else
                         'recommend' if view['recommended'] else 'review')
            details = a.model_dump()
        results.append({'url': job.url, 'expected': case['expected'], 'predicted': predicted,
                        'correct': predicted == case['expected'], 'details': details})
    total = len(results)
    accuracy = sum(r['correct'] for r in results)/total if total else None
    report = {'at': now().isoformat(), 'fingerprint': settings['fingerprint'], 'count': total,
              'total_cases': len(cases), 'accuracy': accuracy,
              'accepted': total == len(cases) and total >= 20 and accuracy is not None and accuracy >= .9,
              'results': results}
    path.with_suffix('.results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'count': total, 'total_cases': len(cases), 'accuracy': accuracy, 'accepted': report['accepted']}
