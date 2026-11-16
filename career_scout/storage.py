from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import Job, canonical_url, now


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def storage_identity(payload):
    url = canonical_url(payload['url'])
    # POST-only catalogs and multi-role announcements share a real public URL.
    # Distinguish their postings by the site's actual ID internally.
    if payload.get('url_is_catalog') and payload.get('external_id'):
        url += '::posting::'+payload['external_id']
    return url


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, company_id TEXT NOT NULL, canonical TEXT NOT NULL,
          content_hash TEXT NOT NULL, payload TEXT NOT NULL, first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL, assessment TEXT, assessed_hash TEXT, fingerprint TEXT,
          exclusion TEXT NOT NULL DEFAULT '', feedback TEXT NOT NULL DEFAULT '',
          analysis_error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0);
        CREATE UNIQUE INDEX IF NOT EXISTS job_url ON jobs(company_id, canonical);
        CREATE TABLE IF NOT EXISTS history (
          id INTEGER PRIMARY KEY, job_id TEXT, content_hash TEXT, payload TEXT, seen TEXT);
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, started TEXT, finished TEXT, status TEXT, summary TEXT);
        CREATE TABLE IF NOT EXISTS sources (
          run_id TEXT, company_id TEXT, status TEXT, detail TEXT, count INTEGER,
          PRIMARY KEY(run_id, company_id));
        CREATE TABLE IF NOT EXISTS usage (
          id TEXT PRIMARY KEY, at TEXT, kind TEXT, reserved REAL, actual REAL,
          status TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS outbox (
          id TEXT PRIMARY KEY, created TEXT, status TEXT, subject TEXT, html TEXT,
          markdown TEXT, items TEXT, message_id TEXT, error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS notifications (
          job_id TEXT, content_hash TEXT, kind TEXT, sent TEXT,
          PRIMARY KEY(job_id,content_hash,kind));
        CREATE TABLE IF NOT EXISTS applications (
          job_id TEXT PRIMARY KEY, applied TEXT, screening TEXT, interview TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS role_decisions (
          job_id TEXT, content_hash TEXT, policy TEXT, payload TEXT, checked TEXT,
          PRIMARY KEY(job_id,content_hash,policy));
        ''')
        self.db.commit()

        columns = {row[1] for row in self.db.execute('PRAGMA table_info(usage)')}
        if 'error' not in columns:
            self.db.execute("ALTER TABLE usage ADD COLUMN error TEXT NOT NULL DEFAULT ''")
            self.db.commit()
        for name, sql_type in [('run_id', "TEXT NOT NULL DEFAULT ''"), ('budget_override', 'INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE usage ADD COLUMN {name} {sql_type}')
        self.db.commit()
        self.active_run_id = ''

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def close(self):
        self.db.close()

    def upsert(self, job: Job) -> str:
        canonical = storage_identity(job.model_dump())
        old = self.db.execute('SELECT * FROM jobs WHERE company_id=? AND canonical=?',
                              (job.company_id, canonical)).fetchone()
        if not old:
            old = self.db.execute('SELECT * FROM jobs WHERE id=?', (job.key,)).fetchone()
        if not old:
            old = next((r for r in self.db.execute('SELECT * FROM jobs WHERE company_id=? ORDER BY first_seen',
                        (job.company_id,)) if storage_identity(json.loads(r['payload'])) == canonical), None)
        if old and not job.verified and json.loads(old['payload']).get('verified'):
            # A failed refetch must not replace an actual JD with an empty search placeholder.
            return old['id']
        key = old['id'] if old else job.key
        stamp = now().isoformat()
        if old and old['content_hash'] != job.content_hash:
            self.db.execute('INSERT INTO history(job_id,content_hash,payload,seen) VALUES(?,?,?,?)',
                            (key, old['content_hash'], old['payload'], stamp))
        self.db.execute('''INSERT INTO jobs(id,company_id,canonical,content_hash,payload,first_seen,last_seen)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET canonical=excluded.canonical,
          content_hash=excluded.content_hash,payload=excluded.payload,last_seen=excluded.last_seen,
          attempts=CASE WHEN jobs.content_hash=excluded.content_hash THEN jobs.attempts ELSE 0 END''',
                        (key, job.company_id, canonical, job.content_hash, dump(job.model_dump()),
                         stamp, stamp))
        self.db.commit()
        return key

    def rows(self):
        rows = []
        seen = set()
        parents = set()
        for raw in self.db.execute('SELECT * FROM jobs ORDER BY first_seen'):
            row = dict(raw)
            payload = json.loads(row['payload'])
            identity = row['company_id'], storage_identity(payload)
            if identity in seen:
                continue
            seen.add(identity)
            if payload.get('parent_external_id'):
                parents.add((row['company_id'], payload['parent_external_id']))
            rows.append(row)
        for row in rows:
            row['superseded_group'] = (row['company_id'], json.loads(row['payload']).get('external_id')) in parents
        return rows

    def save_analysis(self, key, assessment, fingerprint):
        self.db.execute('''UPDATE jobs SET assessment=?,assessed_hash=content_hash,fingerprint=?,
          analysis_error='', attempts=0 WHERE id=?''', (dump(assessment), fingerprint, key))
        self.db.commit()

    def cached_scope(self, job_id, content_hash, policy):
        row = self.db.execute('SELECT payload FROM role_decisions WHERE job_id=? AND content_hash=? AND policy=?',
                              (job_id, content_hash, policy)).fetchone()
        return json.loads(row[0]) if row else None

    def save_scope(self, job_id, content_hash, policy, payload):
        self.db.execute('INSERT OR REPLACE INTO role_decisions VALUES(?,?,?,?,?)',
                        (job_id, content_hash, policy, dump(payload), now().isoformat()))
        self.db.commit()

    def analysis_failed(self, key, reason):
        self.db.execute('UPDATE jobs SET analysis_error=?,attempts=attempts+1 WHERE id=?', (reason, key))
        self.db.commit()

    def exclude(self, key, reason):
        self.db.execute('UPDATE jobs SET exclusion=? WHERE id=?', (reason, key))
        self.db.commit()

    def start_run(self, resume_id=None):
        if resume_id and not self.db.execute('SELECT 1 FROM runs WHERE id=?', (resume_id,)).fetchone():
            raise ValueError('이어 실행할 run_id가 없습니다')
        key = resume_id or uuid.uuid4().hex[:12]
        self.active_run_id = key
        # A prior process that stopped after SMTP DATA must not be automatically resent.
        self.db.execute("UPDATE outbox SET status='uncertain',error='이전 발송 중 프로세스 중단' WHERE status='sending'")
        self.db.execute("UPDATE runs SET status='interrupted' WHERE status='running' AND id<>?", (key,))
        if resume_id:
            self.db.execute("UPDATE runs SET status='running',finished=NULL WHERE id=?", (key,))
            self.db.execute("UPDATE usage SET status='uncertain',error='프로세스 중단으로 응답 확인 불가; 예약액 유지' "
                            "WHERE run_id=? AND status='reserved'", (key,))
        else:
            self.db.execute('INSERT INTO runs VALUES(?,?,NULL,?,NULL)', (key, now().isoformat(), 'running'))
        self.db.commit()
        return key

    def finish_run(self, key, summary, status='complete'):
        self.db.execute('UPDATE runs SET finished=?,status=?,summary=? WHERE id=?',
                        (now().isoformat(), status, dump(summary), key))
        self.db.commit()

    def source(self, run_id, company_id, status, detail, count=0):
        self.db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?)',
                        (run_id, company_id, status, detail[:1000], count))
        self.db.commit()

    def sources(self, run_id):
        return [dict(r) for r in self.db.execute('SELECT * FROM sources WHERE run_id=?', (run_id,))]

    def spend(self, prefix):
        return self.db.execute('''SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage
           WHERE at LIKE ?''', (prefix+'%',)).fetchone()[0]

    def reserve(self, kind, amount, daily, monthly, *, unlimited=False, subdaily=None, submonthly=None):
        key = uuid.uuid4().hex
        stamp = now().isoformat()
        with self.transaction():
            if kind.startswith('repair'):
                for prefix, limit in ((stamp[:10], subdaily), (stamp[:7], submonthly)):
                    spent = self.db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage WHERE kind LIKE 'repair%' AND at LIKE ?", (prefix+'%',)).fetchone()[0]
                    if limit is None or spent+amount > limit+1e-9:
                        raise BudgetExceeded('복구 내부 예산 부족')
            if not unlimited and (self.spend(stamp[:10])+amount > daily+1e-9 or self.spend(stamp[:7])+amount > monthly+1e-9):
                raise BudgetExceeded('API 예산 부족')
            self.db.execute('INSERT INTO usage(id,at,kind,reserved,status,run_id,budget_override) VALUES(?,?,?,?,?,?,?)',
                            (key, stamp, kind, amount, 'reserved', self.active_run_id, int(unlimited)))
        return key

    def settle(self, key, actual, input_tokens=0, output_tokens=0):
        self.db.execute('UPDATE usage SET actual=?,status=?,input_tokens=?,output_tokens=? WHERE id=?',
                        (actual, 'settled', input_tokens, output_tokens, key))
        self.db.commit()

    def uncertain_usage(self, key):
        # Keep the full reservation charged when the server outcome is unknown.
        self.db.execute("UPDATE usage SET status='uncertain' WHERE id=?", (key,))
        self.db.commit()

    def failed_usage(self, key, detail, unbilled):
        self.db.execute('UPDATE usage SET actual=?,status=?,error=? WHERE id=?',
                        (0.0 if unbilled else None, 'rejected' if unbilled else 'uncertain', detail, key))
        self.db.commit()

    def search_count(self):
        return self.db.execute("SELECT COUNT(*) FROM usage WHERE kind='search' AND status<>'rejected' AND at LIKE ?",
                               (now().date().isoformat()+'%',)).fetchone()[0]

    def notified(self, key, content_hash, kind):
        return bool(self.db.execute('SELECT 1 FROM notifications WHERE job_id=? AND content_hash=? AND kind=?',
                                    (key, content_hash, kind)).fetchone())

    def has_notification(self, key, content_hash=None):
        if content_hash is None:
            return bool(self.db.execute('SELECT 1 FROM notifications WHERE job_id=? LIMIT 1', (key,)).fetchone())
        return bool(self.db.execute('SELECT 1 FROM notifications WHERE job_id=? AND content_hash=? LIMIT 1',
                                    (key, content_hash)).fetchone())

    def uncertain_notifications(self):
        blocked = set()
        for row in self.db.execute("SELECT items FROM outbox WHERE status IN ('sending','uncertain')"):
            for item in json.loads(row['items']):
                blocked.add((item['id'], item['hash']))
        return blocked

    def create_mail(self, subject, html, markdown, items):
        key = uuid.uuid4().hex[:16]
        message_id = f'<career-scout-{key}@localhost>'
        # The new digest contains current unsent recommendations. Do not mail a backlog
        # of stale daily status messages when credentials are eventually registered.
        self.db.execute("UPDATE outbox SET status='superseded' WHERE status IN ('pending','failed')")
        self.db.execute('INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?)',
                        (key, now().isoformat(), 'pending', subject, html, markdown,
                         dump(items), message_id, ''))
        self.db.commit()
        return key

    def mail_status(self, key, status, error=''):
        self.db.execute('UPDATE outbox SET status=?,error=? WHERE id=?', (status, error, key))
        self.db.commit()

    def mark_sent(self, mail):
        with self.transaction():
            self.db.execute("UPDATE outbox SET status='sent',error='' WHERE id=?", (mail['id'],))
            for item in json.loads(mail['items']):
                self.db.execute('INSERT OR REPLACE INTO notifications VALUES(?,?,?,?)',
                                (item['id'], item['hash'], item['kind'], now().isoformat()))


class BudgetExceeded(Exception):
    pass
