import asyncio
import smtplib

from career_scout.harness import Harness
from career_scout.mailer import send_mail


class FakeSMTP:
    failure = None
    connection_options = None

    def __init__(self, *args, **kwargs):
        FakeSMTP.connection_options = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.failure == 'quit':
            raise smtplib.SMTPServerDisconnected()

    def ehlo(self):
        pass

    def starttls(self, **kwargs):
        pass

    def login(self, *args):
        if self.failure == 'auth':
            raise smtplib.SMTPAuthenticationError(535, b'bad auth')

    def mail(self, *args):
        return 250, b'ok'

    def rcpt(self, *args):
        return 250, b'ok'

    def data(self, *args):
        if self.failure == 'data':
            raise smtplib.SMTPServerDisconnected()
        if self.failure == 'data-rejected':
            raise smtplib.SMTPDataError(550, b'data rejected')
        return 250, b'ok'


def test_smtp_uncertainty_and_quit(store, config, monkeypatch):
    import career_scout.mailer as m
    monkeypatch.setattr(m, 'credential', lambda _: 'not-a-real-secret')
    monkeypatch.setattr(m.smtplib, 'SMTP', FakeSMTP)
    for failure, expected in [('auth', 'failed'), ('data', 'uncertain'), ('data-rejected', 'failed'),
                              ('quit', 'sent'), (None, 'sent')]:
        FakeSMTP.failure = failure
        key = store.create_mail('test', '<p>test</p>', 'test', [])
        assert send_mail(store, config[0], key) == expected
        assert FakeSMTP.connection_options['local_hostname'].isascii()
        assert FakeSMTP.connection_options['local_hostname'] == 'career-scout.local'


def test_missing_credentials_preserves_pending(store, config, monkeypatch):
    import career_scout.mailer as m
    monkeypatch.setattr(m, 'credential', lambda _: None)
    key = store.create_mail('test', '', '', [])
    assert send_mail(store, config[0], key) == 'pending'


def test_langgraph_offline_end_to_end(store, config, job, monkeypatch, tmp_path):
    import career_scout.reporting as reporting
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    store.upsert(job)
    result = asyncio.run(Harness(*config, store, offline=True, no_api=True).run())
    assert result['mail_status'] == 'preview'
    assert (tmp_path/'reports'/'latest.html').exists()
    assert '산정 불가' in (tmp_path/'reports'/'latest.md').read_text(encoding='utf-8')
    assert not store.db.execute('SELECT 1 FROM notifications').fetchone()


def test_explicit_full_report_contains_previously_notified_jobs(store, config, job, assessment):
    from career_scout.reporting import report_rows, selection
    store.upsert(job)
    store.save_analysis(job.key, assessment, config[0]['fingerprint'])
    rows = report_rows(store, config[0])
    first = selection(store, rows, include_weekly=False)
    assert len(first) == 1
    key = store.create_mail('test', '', '', [
        {'id': first[0][0]['id'], 'hash': first[0][0]['notify_hash'], 'kind': first[0][1]}])
    store.mark_sent(store.db.execute('SELECT * FROM outbox WHERE id=?', (key,)).fetchone())
    assert not selection(store, rows, include_weekly=False)
    assert selection(store, rows, include_weekly=False, include_all=True)[0][1] == 'full-test'
