"""Revalidate cached quote evidence after fixing Korean particle token boundaries."""
import json

from filelock import FileLock

from career_scout.agent import validate_assessment
from career_scout.core import ROOT, Assessment, Job, POLICY_VERSION, data_dir, digest, load_config, prefilter
from career_scout.storage import Store


def main():
    settings, profile, _ = load_config()
    policy = digest([POLICY_VERSION, settings['model'], profile.get('role_boundaries', [])])
    changes = []
    with FileLock(data_dir()/'run.lock', timeout=0):
        store = Store(data_dir()/'scout.sqlite3')
        try:
            for row in store.rows():
                job = Job.model_validate_json(row['payload'])
                store.exclude(row['id'], prefilter(job, settings))
                if not row['assessment'] or row['fingerprint'] != settings['fingerprint']:
                    continue
                if row['assessed_hash'] != row['content_hash']:
                    continue
                data = json.loads(row['assessment'])
                scope = store.cached_scope(job.key, job.content_hash, policy)
                if data['role_scope']['decision'] != 'unclear' or not scope:
                    continue
                before = data['role_scope']['decision']
                data['role_scope'] = scope
                result = validate_assessment(Assessment(**data), job, profile)
                if result.role_scope.decision != before:
                    store.save_analysis(row['id'], result.model_dump(), settings['fingerprint'])
                    changes.append({'id': row['id'], 'title': job.title, 'before': before,
                                    'after': result.role_scope.decision})
            (ROOT/'evaluation'/'korean-evidence-revalidation.json').write_text(
                json.dumps(changes, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(changes, ensure_ascii=False))
        finally:
            store.close()


if __name__ == '__main__':
    main()
