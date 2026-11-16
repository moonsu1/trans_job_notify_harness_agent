"""Structured source observations and incident history, independent of the model."""
from __future__ import annotations

import re
import os
import statistics
import uuid
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from .core import digest, now
from .errors import safe_text


class CatalogProof(BaseModel):
    expected: int | None = None
    observed: int = 0
    company_count: int = 0
    pages_expected: int | None = None
    pages_observed: int = 0
    complete: bool = False
    empty_confirmed: bool = False
    basis: str = ''


class SourceSnapshot(BaseModel):
    company_id: str
    channel: str = 'primary'
    channel_role: str = 'primary_catalog'
    at: str = Field(default_factory=lambda: now().isoformat())
    status: str
    access: str = 'ok'
    code: str = ''
    detail: str = ''
    version: str = 'builtin'
    schema_hash: str = ''
    proof: CatalogProof = Field(default_factory=CatalogProof)
    jd_required: int = 0
    jd_verified: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


def fault_type(detail):
    text = detail.casefold()
    for pattern, code in [
        (r'http (401|403|999)|captcha|접근 제한|접근이 제한|blocked', 'access_blocked'),
        (r'로그인|/cus/login|sign.in required', 'login_required'),
        (r'점검|maintenance|시스템 개선', 'maintenance'),
        (r'http 429', 'rate_limit'),
        (r'timeout|타임아웃|시간 초과|연결 실패|http 50[0234]', 'transient'),
        (r'법인.*불일치|회사.*불일치|게시판.*불일치', 'employer_mismatch'),
        (r'중복|페이지.*누락|건수.*불일치', 'coverage_mismatch'),
        (r'이미지|ocr|image', 'image_unverified'),
    ]:
        if re.search(pattern, text):
            return code
    return 'parser_change'


def observe_result(result, company, settings, schema_hash='', evidence_ids=()):
    from .collectors import allowed_url, verified_jd_body, MIRROR_DOMAINS
    from .core import prefilter, role_relevant
    from urllib.parse import urlsplit
    proof = result.proof or CatalogProof(observed=len(result.jobs), company_count=len(result.jobs))
    snap = SourceSnapshot(company_id=company['id'], status=result.status, detail=safe_text(result.detail),
                          proof=proof, schema_hash=schema_hash, evidence_ids=list(evidence_ids), version=result.version)
    safe_jobs, ids = [], set()
    for job in result.jobs:
        mirror = job.source_kind == 'mirror' and job.employer_verified and any(
            (urlsplit(job.url).hostname or '').endswith(d) for d in MIRROR_DOMAINS)
        if job.company_id != company['id'] or not (allowed_url(job.url, company) or mirror):
            snap.code, snap.status = 'employer_mismatch', 'failed'
            continue
        if job.key in ids:
            snap.code, snap.status = 'coverage_mismatch', 'partial'
            continue
        ids.add(job.key)
        safe_jobs.append(job)
        if not prefilter(job, settings) and role_relevant(job, settings):
            snap.jd_required += 1
            snap.jd_verified += int(job.verified and verified_jd_body(job.body))
            if job.verified and not verified_jd_body(job.body):
                job.verified = False
                snap.code, snap.status = 'jd_invalid', 'partial'
    result.jobs = safe_jobs
    if proof.complete and (proof.expected is None or proof.observed != proof.expected):
        snap.code, snap.status = 'coverage_mismatch', 'partial'
    if snap.status == 'ok' and not proof.complete:
        snap.code, snap.status = 'coverage_mismatch', 'partial'
    if snap.jd_required > snap.jd_verified and snap.status == 'ok':
        snap.code, snap.status = 'jd_invalid', 'partial'
        snap.detail += f' / 관련 JD 원문 확인 {snap.jd_verified}/{snap.jd_required}건'
    if result.channels and proof.complete and snap.jd_verified == snap.jd_required and not snap.code:
        snap.status = 'ok'
    if snap.status != 'ok' and not snap.code:
        snap.code = ('image_unverified' if any(not j.verified and '이미지 JD' in j.source_note for j in result.jobs)
                     else fault_type(result.detail))
        if snap.status == 'partial' and snap.code == 'parser_change' and not proof.complete:
            snap.code = 'coverage_unverified'
    if snap.code in ('access_blocked', 'login_required', 'maintenance', 'rate_limit', 'transient'):
        snap.access = snap.code
    result.status = 'partial' if any(c['status'] != 'ok' for c in result.channels) and snap.status == 'ok' else snap.status
    result.detail = snap.detail
    return snap


class HealthStore:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS source_snapshots(
          id TEXT PRIMARY KEY, run_id TEXT, company_id TEXT, channel TEXT, at TEXT,
          baseline INTEGER, payload TEXT);
        CREATE INDEX IF NOT EXISTS source_health_history ON source_snapshots(company_id,channel,at);
        CREATE TABLE IF NOT EXISTS incidents(
          id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, company_id TEXT, channel TEXT,
          code TEXT, state TEXT, first_seen TEXT, last_seen TEXT, occurrences INTEGER,
          cooldown_until TEXT, detail TEXT, snapshot_id TEXT);
        CREATE TABLE IF NOT EXISTS repair_artifacts(
          id TEXT PRIMARY KEY, company_id TEXT, incident_id TEXT, created TEXT, state TEXT,
          code_hash TEXT, contract_hash TEXT, path TEXT, validation TEXT, successes INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS repair_registry(
          company_id TEXT PRIMARY KEY, active TEXT, previous TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS repair_attempts(
          id TEXT PRIMARY KEY, incident_id TEXT, at TEXT, outcome TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS maintenance_events(
          id TEXT PRIMARY KEY, at TEXT, company_id TEXT, kind TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS operation_slots(
          slot TEXT PRIMARY KEY, kind TEXT, expected TEXT, started TEXT, finished TEXT,
          heartbeat TEXT, phase TEXT, status TEXT, retries INTEGER DEFAULT 0, pid INTEGER);
        ''')
        self.db.commit()

    def record(self, run_id, snapshot, *, reconfirmed=False):
        baseline_rows = self.db.execute('''SELECT payload FROM source_snapshots WHERE company_id=?
          AND channel=? AND baseline=1 ORDER BY at DESC LIMIT 5''', (snapshot.company_id, snapshot.channel)).fetchall()
        prior = [SourceSnapshot.model_validate_json(r[0]) for r in baseline_rows]
        prior_counts = [r.proof.company_count for r in prior]
        if snapshot.status == 'ok' and prior_counts and not reconfirmed:
            median = statistics.median(prior_counts)
            if snapshot.proof.company_count == 0 and max(prior_counts) > 0:
                snapshot.code, snapshot.status = ('count_drop' if snapshot.proof.empty_confirmed else 'zero_unconfirmed'), 'partial'
            elif median >= 5 and snapshot.proof.company_count <= median*.3:
                # Explicit zero is an observation to verify, never proof of closure.
                snapshot.code, snapshot.status = 'count_drop', 'partial'
        key = uuid.uuid4().hex[:16]
        baseline = (snapshot.status == 'ok' and snapshot.proof.complete and snapshot.proof.expected is not None
                    and (snapshot.proof.observed > 0 or snapshot.proof.empty_confirmed))
        self.db.execute('INSERT INTO source_snapshots VALUES(?,?,?,?,?,?,?)',
                        (key, run_id, snapshot.company_id, snapshot.channel, snapshot.at,
                         int(baseline), snapshot.model_dump_json()))
        if snapshot.code:
            fingerprint = digest([snapshot.company_id, snapshot.channel, snapshot.code, snapshot.schema_hash])
            self.db.execute("""UPDATE incidents SET state='superseded' WHERE company_id=? AND channel=?
                AND fingerprint<>? AND state IN ('pending','external')""", (snapshot.company_id, snapshot.channel, fingerprint))
            existing = self.db.execute('SELECT * FROM incidents WHERE fingerprint=?', (fingerprint,)).fetchone()
            if snapshot.channel.startswith('supplemental:'):
                url = snapshot.channel.removeprefix('supplemental:')
                for legacy in self.db.execute('SELECT * FROM incidents WHERE company_id=?', (snapshot.company_id,)).fetchall():
                    if re.fullmatch(r'supplemental-\d+', legacy['channel']) and url in legacy['detail']:
                        if not existing:
                            self.db.execute('UPDATE incidents SET channel=?,fingerprint=? WHERE id=?',
                                            (snapshot.channel, fingerprint, legacy['id']))
                            existing = self.db.execute('SELECT * FROM incidents WHERE id=?', (legacy['id'],)).fetchone()
                        else:
                            self.db.execute("UPDATE incidents SET state='superseded' WHERE id=?", (legacy['id'],))
            cooldown = (now()+timedelta(days=7)).isoformat() if snapshot.code in (
                'access_blocked', 'login_required', 'maintenance') else ''
            state = 'external' if cooldown else 'pending'
            if existing:
                self.db.execute('''UPDATE incidents SET last_seen=?,occurrences=occurrences+1,detail=?,snapshot_id=?,
                  state=CASE WHEN state IN ('resolved','dismissed') THEN ? ELSE state END WHERE id=?''',
                                (snapshot.at, snapshot.detail, key, state, existing['id']))
            else:
                self.db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                (uuid.uuid4().hex[:16], fingerprint, snapshot.company_id, snapshot.channel,
                                 snapshot.code, state, snapshot.at, snapshot.at, 1, cooldown, snapshot.detail, key))
        elif snapshot.status == 'ok' and baseline:
            self.db.execute("UPDATE incidents SET state='resolved',last_seen=? WHERE company_id=? AND channel=? AND state IN ('pending','external')",
                            (snapshot.at, snapshot.company_id, snapshot.channel))
        self.db.commit()
        return snapshot

    def incidents(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM incidents ORDER BY first_seen DESC')]

    def event(self, company_id, kind, detail):
        self.db.execute('INSERT INTO maintenance_events VALUES(?,?,?,?,?)',
                        (uuid.uuid4().hex[:16], now().isoformat(), company_id, kind, safe_text(detail)))
        self.db.commit()

    def summary(self):
        counts = dict(self.db.execute("SELECT state,COUNT(*) FROM incidents GROUP BY state").fetchall())
        since = self.db.execute("SELECT COALESCE(MAX(created),'') FROM outbox WHERE status='sent'").fetchone()[0]
        events = [dict(r) for r in self.db.execute('SELECT * FROM maintenance_events WHERE at>? ORDER BY at DESC LIMIT 20', (since,))]
        from .watchdog import directory, read
        operations = [read(p, {}) for p in (directory()/'operations').glob('*.json') if not p.name.endswith('.requested.json')]
        execution = [s for s in operations if s.get('status') in ('failed', 'interrupted', 'uncertain')
                     and s.get('started', '') > since]
        issues = self.incidents()
        reasons = list(dict.fromkeys(safe_text(i['company_id']+': '+(i['detail'] or i['code']))[:130]
                                     for i in issues if i['state'] in ('pending', 'external')))[:3]
        return {'pending': counts.get('pending', 0)+counts.get('testing', 0), 'external': counts.get('external', 0),
                'recovered': sum(e['kind'] == 'recovered' for e in events), 'execution': len(execution),
                'reasons': reasons, 'events': events}

    def write_report(self, directory):
        import html
        import json
        directory.mkdir(parents=True, exist_ok=True)
        rows = [dict(r) for r in self.db.execute('''SELECT s.* FROM source_snapshots s JOIN
            (SELECT company_id,channel,MAX(at) at FROM source_snapshots GROUP BY company_id,channel) x
            ON s.company_id=x.company_id AND s.channel=x.channel AND s.at=x.at ORDER BY s.company_id,s.channel''')]
        observations = [json.loads(r['payload']) for r in rows]
        observations = [x for x in observations if not re.fullmatch(r'supplemental-\d+', x['channel']) or
                        not any(y['company_id'] == x['company_id'] and y['channel'].startswith('supplemental:') for y in observations)]
        value = dict(at=now().isoformat(), summary=self.summary(), observations=observations,
                     incidents=self.incidents(), versions=[dict(r) for r in self.db.execute('SELECT * FROM repair_registry')])
        path = directory/'latest-health.json'
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        cards = []
        for item in observations:
            p = item['proof']
            label = f"{item['company_id']} / {item['channel']} · {item['status']}"
            facts = f"목록 {p['observed']} / 공식 {p['expected']} · 해당 기업 {p['company_count']} · JD {item['jd_verified']}/{item['jd_required']} · 버전 {item['version']}"
            cards.append('<article><h2>'+html.escape(label)+'</h2><p>'+html.escape(facts)+'</p><p>'
                         +html.escape(item['detail'])+'</p></article>')
        body = ('<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>채용 수집 상태</title><style>body{font:16px/1.7 sans-serif;max-width:900px;margin:auto;padding:16px;'
                'overflow-wrap:anywhere}article{border-bottom:1px solid #ddd;padding:12px 0}h2{font-size:18px}</style>'
                '<h1>채널별 수집 상태</h1><p>'+html.escape(value['at'])+'</p>'+''.join(cards)
                +'<h2>장애·복구 상세</h2><pre style="white-space:pre-wrap">'
                +html.escape(json.dumps(dict(incidents=value['incidents'], summary=value['summary']), ensure_ascii=False, indent=2))+'</pre></html>')
        (directory/'latest-health.html').write_text(body, encoding='utf-8')
        from .core import ROOT
        report_dir = Path(os.environ.get('CAREER_SCOUT_REPORT_DIR') or ROOT/'reports')
        report_dir.mkdir(exist_ok=True)
        (report_dir/'latest-health.html').write_text(body, encoding='utf-8')
        return str(directory/'latest-health.html')

    def cleanup(self, directory):
        cutoff = (now()-timedelta(days=30)).isoformat()
        self.db.execute('DELETE FROM source_snapshots WHERE at<? AND id NOT IN (SELECT snapshot_id FROM incidents WHERE state!=?)', (cutoff, 'resolved'))
        self.db.commit()
        for path in directory.glob('evidence/*.json'):
            if path.stat().st_mtime < (now()-timedelta(days=30)).timestamp():
                path.unlink()
        retained = {r[0] for r in self.db.execute('SELECT active FROM repair_registry UNION SELECT previous FROM repair_registry') if r[0]}
        for row in self.db.execute('SELECT * FROM repair_artifacts WHERE created<?', (cutoff,)).fetchall():
            path = Path(row['path']).resolve()
            if row['id'] not in retained and path.is_relative_to((directory/'artifacts').resolve()):
                path.unlink(missing_ok=True)
        retained_hashes = {r['code_hash'] for r in self.db.execute('SELECT id,code_hash FROM repair_artifacts') if r['id'] in retained}
        for path in directory.glob('runtime/*/plugin.py'):
            safe = path.resolve().is_relative_to((directory/'runtime').resolve()) and re.fullmatch(r'[a-f0-9]{64}', path.parent.name)
            if safe and path.parent.name not in retained_hashes and path.stat().st_mtime < (now()-timedelta(days=30)).timestamp():
                path.unlink()
                if not any(path.parent.iterdir()):
                    path.parent.rmdir()
        for path in directory.glob('dry-run-*.py'):
            if path.stat().st_mtime < (now()-timedelta(days=30)).timestamp():
                path.unlink()
        for path in directory.glob('attempts/*.json'):
            if path.stat().st_mtime < (now()-timedelta(days=30)).timestamp():
                path.unlink()
