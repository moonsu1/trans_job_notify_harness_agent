"""Apply the user's narrower scope to verified cached evidence, without API calls.

Use only for this scope-only change: scores/profile experience/rubric stay unchanged.
The old fingerprint must be supplied explicitly and all source hashes must still match.
"""
from __future__ import annotations

import argparse
import json

from filelock import FileLock

from career_scout.agent import validate_assessment
from career_scout.core import ROOT, Assessment, Job, data_dir, load_config, now, prefilter
from career_scout.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-fingerprint', required=True)
    parser.add_argument('--reuse-scope-policy', help='Explicit prior JD-only scope cache to revalidate during this migration')
    args = parser.parse_args()
    settings, profile, _ = load_config()
    changes = []
    with FileLock(data_dir()/'run.lock', timeout=0):
        store = Store(data_dir()/'scout.sqlite3')
        try:
            for row in store.rows():
                job = Job.model_validate_json(row['payload'])
                store.exclude(row['id'], prefilter(job, settings))
                if (row['fingerprint'] != args.from_fingerprint or not row['assessment']
                        or row['assessed_hash'] != row['content_hash']):
                    continue
                old = Assessment.model_validate_json(row['assessment'])
                before = old.role_scope.decision
                if args.reuse_scope_policy:
                    from career_scout.core import RoleScope
                    original = store.cached_scope(job.key, job.content_hash, args.reuse_scope_policy)
                    if original:
                        old.role_scope = RoleScope(**original)
                result = validate_assessment(old, job, profile)
                store.save_analysis(row['id'], result.model_dump(), settings['fingerprint'])
                changes.append({'id': row['id'], 'title': job.title, 'before': before,
                                'after': result.role_scope.decision, 'reason': result.role_scope.reason})
            audit = {'at': now().isoformat(), 'from_fingerprint': args.from_fingerprint,
                     'to_fingerprint': settings['fingerprint'], 'api_calls': 0,
                     'note': 'Scope-only restriction of existing source-verified evidence; no new model assessment.',
                     'changes': changes}
            (ROOT/'evaluation'/f'agent-focus-upgrade-{now():%Y%m%d-%H%M%S}.json').write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
            (ROOT/'evaluation'/'agent-focus-upgrade.json').write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'revalidated': len(changes),
                              'changed': [c for c in changes if c['before'] != c['after']]}, ensure_ascii=False))
        finally:
            store.close()


if __name__ == '__main__':
    main()
