"""Rebuild a compact report from stored evidence, with no crawling or model calls."""
from __future__ import annotations

import argparse
import json

from filelock import FileLock

from career_scout.core import data_dir, load_config
from career_scout.reporting import build_report
from career_scout.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True, help='Stored source status snapshot to display')
    parser.add_argument('--subject-prefix', default='[형식 수정]')
    args = parser.parse_args()
    settings, profile, _ = load_config()
    settings['_full_scan'] = True
    with FileLock(data_dir()/'run.lock', timeout=0):
        store = Store(data_dir()/'scout.sqlite3')
        try:
            source_run = store.db.execute('SELECT * FROM runs WHERE id=?', (args.source_run,)).fetchone()
            if not source_run:
                raise ValueError('원본 실행 ID를 찾을 수 없습니다')
            run_id = store.start_run()
            for source in store.sources(args.source_run):
                store.source(run_id, source['company_id'], source['status'],
                             f'원수집 {source_run["started"][:16]} · '+source['detail'], source['count'])
            summary = {'run_cost_usd': 0, 'stored_only': True, 'source_run': args.source_run,
                       'warnings': ['최근 저장된 수집 결과로 리포트를 구성했습니다. 추가 수집·유료 API 분석 없이 만들었으며 새 JD의 적합성 평가는 대기 중입니다.']}
            result = build_report(store, settings, profile, run_id, summary)
            mail_id = store.create_mail(args.subject_prefix+' '+result['subject'], result['html'],
                                        result['markdown'], result['items'])
            store.finish_run(run_id, dict(summary, mail_id=mail_id), 'preview')
            print(json.dumps({'run_id': run_id, 'mail_id': mail_id, 'report': result['report'],
                              'cards': len(result['items']), 'api_calls': 0}, ensure_ascii=False))
        finally:
            store.close()


if __name__ == '__main__':
    main()
