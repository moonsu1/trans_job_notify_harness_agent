"""Export the exact full-run source evidence, without API calls or DB changes."""
import argparse
import json
import sqlite3
import sys
from collections import Counter

from career_scout.core import ROOT, data_dir, load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    args = parser.parse_args()
    _, _, companies = load_config()
    db = sqlite3.connect(data_dir()/'scout.sqlite3')
    db.row_factory = sqlite3.Row
    try:
        run = db.execute('SELECT * FROM runs WHERE id=?', (args.run,)).fetchone()
        if not run:
            raise ValueError('Unknown run')
        sources = {r['company_id']: dict(r) for r in db.execute('SELECT * FROM sources WHERE run_id=?', (args.run,))}
        records = [dict(company=c['name'], tier=c['tier'], url=c['url'],
                        **sources.get(c['id'], dict(company_id=c['id'], status='skipped', count=0, detail='실행 결과 없음')))
                   for c in companies]
        counts = Counter(r['status'] for r in records)
        data = dict(run_id=run['id'], started=run['started'], finished=run['finished'], counts=dict(counts), sources=records)
        (ROOT/'evaluation'/'source-audit-latest.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        md = ['# 채용 사이트 수집 상태', '', f'실행: {run["id"]} · 시작: {run["started"]} · 완료: {run["finished"]}', '',
              f'대상 {len(records)}곳 · 정상 {counts["ok"]} · 부분 {counts["partial"]} · 실패 {counts["failed"]}', '',
              '정상은 해당 공개 채널의 목록·현재 후보 JD 확인 기준을 통과했다는 뜻입니다. 모든 회사가 현재 모집 중이거나 추천된다는 뜻은 아닙니다.',
              '목록 항목 수에는 마감·비대상 공고가 포함됩니다. 부분·실패는 공고 없음과 구분합니다.', '',
              '|순위|기업|상태|목록 항목|확인 근거|', '|---|---|---|---:|---|']
        labels = dict(ok='정상', partial='부분', failed='실패', skipped='미실행')
        for r in records:
            detail = r['detail'].replace('|', '/').replace('\n', ' ')
            md.append(f'|{r["tier"]}|[{r["company"]}]({r["url"]})|{labels.get(r["status"],r["status"])}|{r["count"]}|{detail}|')
        (ROOT/'수집상태.md').write_text('\n'.join(md)+'\n', encoding='utf-8')
        print(json.dumps(dict(run_id=run['id'], counts=dict(counts)), ensure_ascii=False))
    finally:
        db.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
