from bs4 import BeautifulSoup

from career_scout.digest import card_summary, render_digest
from career_scout.reporting import report_rows


def sample_row(store, config, job, assessment):
    store.upsert(job)
    store.save_analysis(job.key, assessment, config[0]['fingerprint'])
    return report_rows(store, config[0])[0]


def test_email_is_single_column_bounded_and_keeps_action(store, config, job, assessment):
    assessment['strengths'] = ['첫 번째 장점', '두 번째 장점', '메일에서 생략할 세 번째 장점']
    assessment['important_unknowns'] = ['첫 번째 확인', '두 번째 확인', '메일에서 생략할 세 번째 확인']
    assessment['role_scope']['reason'] = '주 산출물: 고객 상담 AI Agent 서비스. 추가 설명은 상세 리포트에 남겨요.'
    row = sample_row(store, config, job, assessment)
    md, rendered = render_digest([(row, 'new')], [], [], config[0], {})
    soup = BeautifulSoup(rendered, 'html.parser')
    assert not soup.select('table, th, td')
    assert soup.select_one('meta[name=viewport]')
    assert soup.select_one('a')['href'] == job.url
    assert '메일에서 생략할' not in rendered
    assert '추가 설명은' not in rendered
    assert '점수 근거' not in rendered and '예측 확률' not in rendered
    assert len(soup.get_text()) < 1100
    assert '첫 번째 확인' in md and '고객 상담 AI Agent 서비스' in md


def test_digest_escapes_untrusted_text(store, config, job, assessment):
    job.title = '<img src=x onerror=alert(1)> AI Agent'
    row = sample_row(store, config, job, assessment)
    _, rendered = render_digest([(row, 'new')], [], [], config[0], {})
    assert not BeautifulSoup(rendered, 'html.parser').select('img, script')
    assert '&lt;img' in rendered
    assert len(card_summary(row)['duty']) <= 85


def test_incomplete_top_priority_source_is_prominent_even_with_no_jobs(config):
    source = {'company_id': 'samsung_ds', 'status': 'partial', 'count': 0, 'detail': '별도 채널 HTTP 403'}
    md, rendered = render_digest([], [], [source], config[0], {})
    assert '1순위 수집 미완료' in rendered and '삼성전자 DS' in rendered
    assert '공고가 없다고 판단할 수 없습니다' in md
    assert rendered.index('1순위 수집 미완료') < rendered.index('현재 조건에 맞는 추천·검토 공고가 없습니다')


def test_local_detail_and_email_digest_are_separate(store, config, job, assessment, tmp_path, monkeypatch):
    import career_scout.reporting as reporting
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    sample_row(store, config, job, assessment)
    result = reporting.build_report(store, config[0], config[1], 'layout-test', {})
    assert '점수 근거' not in result['html']
    full = (tmp_path/'reports'/'latest.html').read_text(encoding='utf-8')
    assert '<details' in full and '점수 근거' in full
    assert not BeautifulSoup(full, 'html.parser').select('table')
    assert (tmp_path/'reports'/'latest-email.html').read_text(encoding='utf-8') == result['html']


def test_image_discovery_is_unscored_separate_and_repeated(store, config, job, tmp_path, monkeypatch):
    import career_scout.reporting as reporting
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    job.source_note = '이미지 원문을 직접 대조한 세부 직무'
    key = store.upsert(job)
    result = reporting.build_report(store, config[0], config[1], 'discovery', {})
    assert '적합성 평가 대기' in result['html']
    assert '적합성 None점' not in result['html']
    assert result['items'][0]['kind'] == 'discovery'
    assert result['items'][0]['id'] == key
    mail_id = store.create_mail(result['subject'], result['html'], result['markdown'], result['items'])
    mail = dict(store.db.execute('SELECT * FROM outbox WHERE id=?', (mail_id,)).fetchone())
    store.mark_sent(mail)
    again = reporting.build_report(store, config[0], config[1], 'discovery-again', {})
    assert len(again['items']) == 1 and again['items'][0]['kind'] == 'discovery'


def test_every_digest_contains_previously_sent_eligible_jobs(store, config, job, assessment, tmp_path, monkeypatch):
    import career_scout.reporting as reporting
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    sample_row(store, config, job, assessment)
    first = reporting.build_report(store, config[0], config[1], 'first', {})
    ident = store.create_mail(first['subject'], first['html'], first['markdown'], first['items'])
    store.mark_sent(store.db.execute('SELECT * FROM outbox WHERE id=?', (ident,)).fetchone())
    second = reporting.build_report(store, config[0], config[1], 'second', {})
    assert len(second['items']) == 1 and second['items'][0]['kind'] == 'repeat'
    assert '신규 0건 · 기존 1건' in second['subject']
    assert '기존 검토' in second['html'] and '이전에 보낸 공고도 포함' in second['html']
    assert not store.db.execute('SELECT 1 FROM usage').fetchone()
    # An applied job disappears from subsequent complete digests.
    store.db.execute("UPDATE jobs SET feedback='applied'")
    store.db.commit()
    assert not reporting.build_report(store, config[0], config[1], 'applied', {})['items']


def test_complete_digest_has_no_fifteen_job_truncation(store, config, job, assessment, tmp_path, monkeypatch):
    import career_scout.reporting as reporting
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    for i in range(17):
        item = job.model_copy(update=dict(external_id=str(i), url=f'https://careers.sktelecom.com/jobs/{i}'))
        key = store.upsert(item)
        store.save_analysis(key, assessment, config[0]['fingerprint'])
    first = reporting.build_report(store, config[0], config[1], 'first-17', {})
    assert len(first['items']) == 17
    ident = store.create_mail(first['subject'], first['html'], first['markdown'], first['items'])
    store.mark_sent(store.db.execute('SELECT * FROM outbox WHERE id=?', (ident,)).fetchone())
    second = reporting.build_report(store, config[0], config[1], 'again-17', {})
    assert len(second['items']) == 17
    assert {i['kind'] for i in second['items']} == {'repeat'}


def test_uncertain_old_message_does_not_hide_jobs_from_next_complete_digest(store, config, job, assessment, tmp_path, monkeypatch):
    import career_scout.reporting as reporting
    from career_scout.mailer import send_mail
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    sample_row(store, config, job, assessment)
    first = reporting.build_report(store, config[0], config[1], 'uncertain-first', {})
    ident = store.create_mail(first['subject'], first['html'], first['markdown'], first['items'])
    store.mail_status(ident, 'uncertain')
    second = reporting.build_report(store, config[0], config[1], 'new-scheduled-digest', {})
    assert len(second['items']) == 1
    # This is a new complete digest; the old SMTP transaction is never retried.
    monkeypatch.setattr('career_scout.mailer.credential', lambda name: (_ for _ in ()).throw(AssertionError('must not send old message')))
    assert send_mail(store, config[0], ident) == 'uncertain'


def test_closed_and_expired_jobs_do_not_reappear_in_complete_digest(store, config, job, assessment, tmp_path, monkeypatch):
    import career_scout.reporting as reporting
    from datetime import timedelta
    from career_scout.core import now
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    for item in (job.model_copy(update=dict(status='closed',external_id='closed')),
                 job.model_copy(update=dict(url=job.url+'/expired',external_id='expired',deadline=(now().date()-timedelta(days=1)).isoformat()))):
        key = store.upsert(item)
        store.save_analysis(key, assessment, config[0]['fingerprint'])
    assert not reporting.build_report(store, config[0], config[1], 'closed', {})['items']


def test_first_notification_is_separated_from_existing_and_updated_postings(store, config, job, assessment, tmp_path, monkeypatch):
    from datetime import timedelta
    import career_scout.reporting as reporting
    from career_scout.core import now
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    old = job.model_copy(update=dict(external_id='old',url=job.url+'/old',title='이전 AI Agent Engineer'))
    changed = job.model_copy(update=dict(external_id='changed',url=job.url+'/changed',title='변경 AI Agent Engineer'))
    for item in (old, changed):
        key = store.upsert(item)
        store.save_analysis(key, assessment, config[0]['fingerprint'])
    first = reporting.build_report(store, config[0], config[1], 'before', {})
    ident = store.create_mail(first['subject'], first['html'], first['markdown'], first['items'])
    store.mark_sent(store.db.execute('SELECT * FROM outbox WHERE id=?', (ident,)).fetchone())
    changed.body += ' 추가 업무: Agent 서비스 평가와 개선.'
    new = job.model_copy(update=dict(external_id='new',url=job.url+'/new',title='신규 AI Agent Engineer',
                                    deadline=(now().date()+timedelta(days=2)).isoformat()))
    for item in (changed, new):
        key = store.upsert(item)
        store.save_analysis(key, assessment, config[0]['fingerprint'])
    result = reporting.build_report(store, config[0], config[1], 'after', {})
    kinds = {i['id']:i['kind'] for i in result['items']}
    assert kinds == {old.key:'repeat',changed.key:'updated',new.key:'new'}
    assert '신규 1건 · 기존 2건' in result['subject']
    soup = BeautifulSoup(result['html'], 'html.parser')
    assert [h.get_text() for h in soup.select('h2')] == ['새로 추가된 공고 · 1건','이전에 알려드린 공고 · 2건']
    for card in soup.select('h3'):
        section = card.find_previous('h2').get_text()
        assert section == ('새로 추가된 공고 · 1건' if new.title in card.get_text() else '이전에 알려드린 공고 · 2건')
    assert '내용·평가 변경' in result['html'] and '신규 · 마감 임박' in result['html']
    # Once the changed content is delivered, it remains an ordinary existing job.
    ident = store.create_mail(result['subject'], result['html'], result['markdown'], result['items'])
    store.mark_sent(store.db.execute('SELECT * FROM outbox WHERE id=?', (ident,)).fetchone())
    again = reporting.build_report(store, config[0], config[1], 'again', {})
    assert '신규 0건 · 기존 3건' in again['subject']
    assert {i['kind'] for i in again['items']} == {'repeat','deadline'}


def test_previously_delivered_unscored_posting_is_not_new_when_scored(store, config, job, assessment, tmp_path, monkeypatch):
    import career_scout.reporting as reporting
    monkeypatch.setattr(reporting, 'ROOT', tmp_path)
    job.source_note = '이미지 원문을 직접 대조한 세부 직무'
    key = store.upsert(job)
    first = reporting.build_report(store, config[0], config[1], 'unscored', {})
    ident = store.create_mail(first['subject'],first['html'],first['markdown'],first['items'])
    store.mark_sent(store.db.execute('SELECT * FROM outbox WHERE id=?', (ident,)).fetchone())
    store.save_analysis(key,assessment,config[0]['fingerprint'])
    result = reporting.build_report(store, config[0], config[1], 'scored', {})
    assert len(result['items']) == 1 and result['items'][0]['kind'] == 'repeat'
    assert '신규 0건 · 기존 1건' in result['subject']
    assert '새로 추가된 공고가 없습니다.' in result['markdown']
