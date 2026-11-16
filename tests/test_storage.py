from datetime import timedelta

import pytest

from career_scout.core import now
from career_scout.reporting import report_rows, selection
from career_scout.storage import BudgetExceeded


def test_dedup_and_change_history(store, job):
    key = store.upsert(job)
    job.url += '?utm_source=newsletter'
    assert store.upsert(job) == key
    assert len(store.rows()) == 1
    job.body += ' 변경된 필수 조건'
    store.upsert(job)
    assert store.db.execute('SELECT COUNT(*) FROM history').fetchone()[0] == 1


def test_crash_reservation_remains_charged(store):
    ticket = store.reserve('analysis', .2, .3, 10)
    store.uncertain_usage(ticket)
    assert store.spend(now().isoformat()[:10]) == .2
    with pytest.raises(BudgetExceeded):
        store.reserve('analysis', .11, .3, 10)
    store.settle(ticket, .01, 100, 100)
    store.reserve('analysis', .2, .3, 10)


def test_monthly_cap(store):
    store.reserve('analysis', .29, .3, .3)
    with pytest.raises(BudgetExceeded):
        store.reserve('analysis', .02, 1, .3)


def test_no_repeat_until_delivery_and_profile_invalidation(store, config, job, assessment):
    settings = config[0]
    key = store.upsert(job)
    store.save_analysis(key, assessment, settings['fingerprint'])
    rows = report_rows(store, settings)
    chosen = selection(store, rows, include_weekly=False)
    assert len(chosen) == 1
    r, kind = chosen[0]
    mail_id = store.create_mail('test', '<p>test</p>', 'test',
                               [{'id': key, 'hash': r['notify_hash'], 'kind': kind}])
    assert len(selection(store, rows, include_weekly=False)) == 1
    mail = store.db.execute('SELECT * FROM outbox WHERE id=?', (mail_id,)).fetchone()
    store.mark_sent(mail)
    assert not selection(store, rows, include_weekly=False)
    changed = dict(settings, fingerprint='new-profile')
    assert report_rows(store, changed)[0]['analysis'] is None
    store.save_analysis(key, assessment, changed['fingerprint'])
    assert selection(store, report_rows(store, changed), include_weekly=False)


def test_interrupted_smtp_marked_uncertain(store):
    key = store.create_mail('test', '', '', [])
    store.mail_status(key, 'sending')
    store.start_run()
    assert store.db.execute('SELECT status FROM outbox WHERE id=?', (key,)).fetchone()[0] == 'uncertain'


def test_deadline_notification_once(store, config, job, assessment):
    job.deadline = (now().date()+timedelta(days=3)).isoformat()
    key = store.upsert(job)
    store.save_analysis(key, assessment, config[0]['fingerprint'])
    row = report_rows(store, config[0])[0]
    for kind in ['new', 'deadline']:
        chosen = selection(store, [row], include_weekly=False)
        assert chosen[0][1] == kind
        mail_id = store.create_mail('test', '', '', [{'id': key, 'hash': row['notify_hash'], 'kind': kind}])
        store.mark_sent(store.db.execute('SELECT * FROM outbox WHERE id=?', (mail_id,)).fetchone())
    assert not selection(store, [row], include_weekly=False)


def test_ignored_excluded(store, job, assessment, config):
    key = store.upsert(job)
    store.save_analysis(key, assessment, config[0]['fingerprint'])
    store.db.execute("UPDATE jobs SET feedback='ignored' WHERE id=?", (key,))
    store.db.commit()
    assert not selection(store, report_rows(store, config[0]))


def test_uncertain_delivery_blocks_new_daily_digest(store, job, assessment, config):
    key = store.upsert(job)
    store.save_analysis(key, assessment, config[0]['fingerprint'])
    row = report_rows(store, config[0])[0]
    mail_id = store.create_mail('test', '', '', [{'id': key, 'hash': row['notify_hash'], 'kind': 'new'}])
    store.mail_status(mail_id, 'uncertain')
    assert not selection(store, [row])
    store.mail_status(mail_id, 'failed')
    assert selection(store, [row])


def test_new_digest_supersedes_old_unsent_status(store):
    old = store.create_mail('old', '', '', [])
    store.create_mail('new', '', '', [])
    assert store.db.execute('SELECT status FROM outbox WHERE id=?', (old,)).fetchone()[0] == 'superseded'


def test_high_fit_review_is_reported_as_review_not_recommendation(store, job, assessment, config):
    assessment['important_unknowns'] = ['근무지 세부 확인 필요']
    key = store.upsert(job)
    store.save_analysis(key, assessment, config[0]['fingerprint'])
    row = report_rows(store, config[0])[0]
    assert not row['view']['recommended']
    assert row['view']['grade'] == '판단 보류'
    assert selection(store, [row])


def test_local_rejected_search_does_not_consume_daily_tool_quota(store):
    ticket = store.reserve('search', .06, .3, 10)
    assert store.search_count() == 1
    store.failed_usage(ticket, 'local import failure before request', True)
    assert store.search_count() == 0


def test_unlimited_budget_is_explicit_and_audited(store):
    ticket = store.reserve('analysis', 20, .9, 10, unlimited=True)
    assert store.db.execute('SELECT budget_override FROM usage WHERE id=?', (ticket,)).fetchone()[0] == 1
    with pytest.raises(BudgetExceeded):
        store.reserve('analysis', .01, .9, 10)


def test_same_job_language_variants_are_deduplicated(store, job):
    job.url = 'https://recruit.navercorp.com/rcrt/view.do?annoId=30005640&lang=ko'
    first = store.upsert(job)
    job.url = 'https://recruit.navercorp.com/rcrt/view.do?lang=en&annoId=30005640&utm_source=openai'
    assert store.upsert(job) == first
    assert len(store.rows()) == 1


def test_resume_preserves_work_and_marks_inflight_usage_uncertain(store):
    run_id = store.start_run()
    ticket = store.reserve('analysis', .02, .9, 10)
    store.source(run_id, 'skt', 'partial', 'test', 2)
    assert store.start_run(run_id) == run_id
    assert store.sources(run_id)[0]['count'] == 2
    assert store.db.execute('SELECT status FROM usage WHERE id=?', (ticket,)).fetchone()[0] == 'uncertain'
