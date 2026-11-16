import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from career_scout.collectors import Collection, PublicTools
from career_scout.core import Job, now
from career_scout.health import CatalogProof, HealthStore, SourceSnapshot, observe_result
from career_scout.maintenance import Maintenance, Registry
from career_scout.repair_runtime import (DockerRunner, PublicBroker, ReplayBroker, RepairRejected, contract_hash, resolve,
                                        validate_code, validate_output)
from career_scout.storage import BudgetExceeded
from career_scout import watchdog


def snapshot(count=10, *, status='ok', code='', complete=True, empty=False, schema='a'):
    return SourceSnapshot(company_id='skt', status=status, code=code, schema_hash=schema,
                          proof=CatalogProof(expected=count, observed=count, company_count=count,
                                             complete=complete, empty_confirmed=empty))


def test_baseline_excludes_bad_results_and_detects_silent_zero(store):
    health = HealthStore(store)
    for _ in range(5):
        health.record('good', snapshot())
    result = health.record('broken', snapshot(0, complete=False))
    assert result.status == 'partial' and result.code == 'zero_unconfirmed'
    assert store.db.execute('SELECT COUNT(*) FROM source_snapshots WHERE baseline=1').fetchone()[0] == 5


def test_explicit_zero_is_reconfirmed_without_inventing_closure(store):
    health = HealthStore(store)
    health.record('first', snapshot())
    assert health.record('suspected', snapshot(0, empty=True)).code == 'count_drop'
    confirmed = health.record('second', snapshot(0, empty=True), reconfirmed=True)
    assert confirmed.status == 'ok'
    assert health.incidents()[0]['state'] == 'resolved'


def test_small_counts_do_not_trigger_percentage_alarm(store):
    health = HealthStore(store)
    health.record('a', snapshot(4))
    assert health.record('b', snapshot(1)).status == 'ok'


def test_seventy_percent_and_positive_to_zero_are_rechecked(store):
    health = HealthStore(store)
    health.record('a', snapshot(10))
    assert health.record('b', snapshot(3)).code == 'count_drop'
    health.record('c', snapshot(2), reconfirmed=True)
    assert health.record('d', snapshot(0, empty=True)).status == 'partial'


def test_same_incident_deduplicates_but_new_structure_is_new(store):
    health = HealthStore(store)
    for _ in range(3):
        health.record('bad', snapshot(status='failed', code='parser_change'))
    assert len(health.incidents()) == 1
    assert health.incidents()[0]['occurrences'] == 3
    health.record('changed', snapshot(status='failed', code='parser_change', schema='new'))
    assert len(health.incidents()) == 2


def test_external_block_has_cooldown_and_never_baseline(store):
    health = HealthStore(store)
    health.record('blocked', snapshot(status='failed', code='access_blocked'))
    incident = health.incidents()[0]
    assert incident['state'] == 'external'
    assert incident['cooldown_until'] > now().isoformat()
    assert store.db.execute('SELECT SUM(baseline) FROM source_snapshots').fetchone()[0] == 0


def test_samsung_group_count_and_ds_zero_are_separate(config):
    c = next(c for c in config[2] if c['id'] == 'samsung_ds')
    result = Collection(c['id'], status='partial', detail='DS supplemental HTTP 403',
                        proof=CatalogProof(expected=7, observed=7, company_count=0, complete=True),
                        channels=[dict(channel='ds-extra', status='partial', detail='HTTP 403')])
    view = observe_result(result, c, config[0])
    assert view.status == 'ok' and view.proof.observed == 7 and view.proof.company_count == 0
    assert result.status == 'partial'


def test_catalog_completeness_cannot_be_claimed_with_wrong_count(config):
    c = next(c for c in config[2] if c['id'] == 'skt')
    r = Collection(c['id'], status='ok', proof=CatalogProof(expected=20, observed=1, complete=True))
    assert observe_result(r, c, config[0]).code == 'coverage_mismatch'


def test_wrong_company_board_is_quarantined_before_recommendation(config):
    c = next(c for c in config[2] if c['id'] == 'krafton')
    job = Job(company_id='krafton', company='크래프톤', tier=3, title='AI Agent Engineer',
              url='https://job-boards.greenhouse.io/another-company/jobs/1')
    snap = observe_result(Collection('krafton', [job], 'ok'), c, config[0])
    assert snap.status == 'failed' and snap.code == 'employer_mismatch'


def test_repair_budget_is_a_subcap_of_the_same_global_budget(store):
    store.reserve('repair_patch', .08, .9, 10, subdaily=.1, submonthly=1)
    with pytest.raises(BudgetExceeded):
        store.reserve('repair_patch', .03, .9, 10, subdaily=.1, submonthly=1)
    with pytest.raises(BudgetExceeded):
        store.reserve('analysis', .83, .9, 10)
    with pytest.raises(BudgetExceeded):
        store.reserve('repair_patch', .001, .9, 10)


@pytest.mark.parametrize('code', [
    'import os\ndef collect(api, company): return {}',
    'import subprocess\ndef collect(api, company): return {}',
    'def collect(api, company): return open("key.txt").read()',
    'def collect(api, company): return api.__dict__',
    'from keyring import get_password\ndef collect(api, company): return {}',
    'def collect(api, company): return eval("1")',
])
def test_generated_code_has_no_host_authority(code):
    with pytest.raises(RepairRejected):
        validate_code(code)


@pytest.fixture
def source_evidence(config):
    c = next(c for c in config[2] if c['id'] == 'kakao')
    body = '담당업무: LLM Agent 개발 및 RAG 서비스 운영. 필수 자격: Python 개발 경험. 우대사항: 도구 연동. '*15
    raw = {'count': 1, 'items': [{'id': '1', 'title': 'AI Agent Engineer',
                                'url': 'https://careers.kakao.com/jobs/1', 'body': body}]}
    record = dict(id='a'*32, url=c['url'], status=200, text=json.dumps(raw, ensure_ascii=False))
    ledger = {record['id']: record}

    def ref(path):
        return {'evidence_id': record['id'], 'pointer': path}
    output = {'pages': [{'page': 1, 'total': ref('/count'), 'ids': ref('/items/*/id')}],
              'jobs': [{key: ref('/items/0/'+key) for key in ('id', 'title', 'url', 'body')}]}
    return c, ledger, output


def test_field_values_come_from_original_evidence(config, source_evidence):
    c, ledger, output = source_evidence
    result = validate_output(output, c, ledger, config[0])
    assert result.status == 'ok' and result.jobs[0].verified
    output['jobs'][0]['location'] = '서울'
    with pytest.raises(RepairRejected):
        validate_output(output, c, ledger, config[0])


def test_unavailable_field_is_not_filled_in(source_evidence):
    _, ledger, _ = source_evidence
    with pytest.raises(RepairRejected):
        resolve({'evidence_id': 'a'*32, 'pointer': '/salary'}, ledger)


def test_fake_zero_and_missing_pages_are_rejected(config, source_evidence):
    c, ledger, output = source_evidence
    output['jobs'] = []
    with pytest.raises(RepairRejected, match='누락'):
        validate_output(output, c, ledger, config[0])
    output['pages'][0]['page'] = 2
    with pytest.raises(RepairRejected, match='페이지'):
        validate_output(output, c, ledger, config[0])


def test_closed_or_blocked_response_is_not_a_success(config, source_evidence):
    c, ledger, output = source_evidence
    ledger['a'*32]['status'] = 403
    with pytest.raises(RepairRejected):
        validate_output(output, c, ledger, config[0])


def test_catalog_duplicate_ids_are_rejected(config, source_evidence):
    c, ledger, output = source_evidence
    value = json.loads(ledger['a'*32]['text'])
    value['count'] = 2
    value['items'].append(dict(value['items'][0]))
    ledger['a'*32]['text'] = json.dumps(value)
    with pytest.raises(RepairRejected, match='중복'):
        validate_output(output, c, ledger, config[0])


def verified_report(company):
    return dict(passed=True, contract_hash=contract_hash(company), fixture_count=1, image_id='sha256:test',
                first_at=now().isoformat(), second_at=(now()+timedelta(seconds=31)).isoformat())


def test_activation_is_atomic_and_rollback_restores_previous(store, config, tmp_path):
    c = next(c for c in config[2] if c['id'] == 'kakao')
    registry = Registry(store, config[0], tmp_path)
    code = 'def collect(api, company):\n    return {"pages": [], "jobs": []}\n'
    first = registry.activate(c, 'test', code, verified_report(c))
    second = registry.activate(c, 'test', code+'\n# next\n', verified_report(c))
    assert registry.active(c)['id'] == second
    assert registry.rollback(c['id'])
    assert registry.active(c)['id'] == first
    assert store.db.execute('SELECT state FROM repair_artifacts WHERE id=?', (second,)).fetchone()[0] == 'quarantined'


def test_activation_requires_live_interval_and_current_contract(store, config, tmp_path):
    c = next(c for c in config[2] if c['id'] == 'kakao')
    registry = Registry(store, config[0], tmp_path)
    report = verified_report(c)
    report['second_at'] = report['first_at']
    with pytest.raises(RepairRejected, match='간격'):
        registry.activate(c, 'test', 'def collect(api,c): return {}', report)
    report = verified_report(c)
    report['contract_hash'] = 'old'
    with pytest.raises(RepairRejected, match='검증'):
        registry.activate(c, 'test', 'def collect(api,c): return {}', report)


def test_docker_unavailable_never_executes_python_on_host(store, config, tmp_path):
    c = next(c for c in config[2] if c['id'] == 'kakao')

    class Unavailable:
        async def ready(self):
            return False
        async def run(self, *args, **kwargs):
            raise AssertionError('must not execute')

    registry = Registry(store, config[0], tmp_path, runner=Unavailable())
    registry.activate(c, 'test', 'def collect(api,c): return {}', verified_report(c))
    assert asyncio.run(registry.execute(NS(), c)) is None


def test_failed_generated_result_rolls_back_without_changing_preferences(store, config, tmp_path):
    c = next(c for c in config[2] if c['id'] == 'kakao')
    original_recipient = config[0]['recipient']

    class Invalid:
        image_id = 'sha256:test'
        async def ready(self):
            return True
        async def run(self, *args, **kwargs):
            raise RepairRejected('다른 기업 공고')

    registry = Registry(store, config[0], tmp_path, runner=Invalid())
    registry.activate(c, 'test', 'def collect(api,c): return {}', verified_report(c))
    assert asyncio.run(registry.execute(NS(), c)) is None
    assert registry.active(c) is None
    assert config[0]['recipient'] == original_recipient


def test_schema_fingerprint_ignores_counts_and_timestamps():
    from career_scout.evidence import schema_hash
    assert schema_hash('{"total":1,"items":[{"id":1,"at":"today"}]}') == schema_hash('{"total":3,"items":[{"id":99,"at":"tomorrow"}]}')
    assert schema_hash('{"total":1}') != schema_hash('{"count":1}')


def test_http_retries_are_bounded_and_honor_retry_after(config):
    async def run():
        tools = PublicTools(config[0])
        calls = []
        async def permitted(url):
            pass
        async def raw(method, url, **kwargs):
            from career_scout.collectors import CollectionError
            calls.append(url)
            raise CollectionError('HTTP 429', http_status=429, retry_after=120)
        tools.permitted, tools._raw = permitted, raw
        try:
            with pytest.raises(Exception, match='429'):
                await tools.fetch('https://example.com/jobs')
            assert len(calls) == 1
        finally:
            await tools.close()
    asyncio.run(run())


def test_watchdog_only_catches_latest_due_slot_and_skips_install_history(tmp_path, monkeypatch):
    at = now().replace(year=2026, month=11, day=12, hour=15, minute=0)
    config = dict(anchor='2026-11-09', interval=3, installed_at=at.replace(day=10).isoformat())
    assert watchdog.due_slot('report', config, at)[0] == 'report-20261112T1300'
    assert watchdog.due_slot('report', config, at.replace(day=20))[0] == 'report-20261118T1300'
    monkeypatch.setattr(watchdog, 'timestamp', lambda: at)
    results = watchdog.inspect_operations(config, dry_run=True)
    assert len(results) == 2
    assert all(r['state'] == 'would_launch' or r['state'] == 'report_has_priority' for r in results)


def test_watchdog_does_not_resend_uncertain_delivery(monkeypatch):
    at = now().replace(year=2026, month=11, day=12, hour=15, minute=0)
    config = dict(anchor='2026-11-09', interval=3, installed_at=at.replace(day=10).isoformat())
    monkeypatch.setattr(watchdog, 'timestamp', lambda: at)
    root = watchdog.directory()
    slot, _ = watchdog.due_slot('report', config)
    watchdog.atomic(root/'operations'/(slot+'.json'), dict(status='uncertain'))
    assert watchdog.operation(config, 'report', retry=True, dry_run=True)['state'] == 'already_handled'


def test_paused_schedule_blocks_both_direct_launch_and_watchdog(monkeypatch):
    def unexpected_launch(*args, **kwargs):
        raise AssertionError('paused schedule must not launch any job')
    monkeypatch.setattr(watchdog.subprocess, 'Popen', unexpected_launch)
    config = dict(paused=True)
    assert watchdog.operation(config, 'report', retry=True) == {'state': 'paused'}
    assert watchdog.operation(config, 'maintenance') == {'state': 'paused'}
    assert watchdog.inspect_operations(config) == [{'state': 'paused'}]


def test_watchdog_rechecks_disk_pause_instead_of_using_old_loaded_config():
    old = dict(paused=False)
    watchdog.atomic(watchdog.directory()/'schedule.json', dict(paused=True))
    assert watchdog.operation(old, 'report')['state'] == 'paused'
    watchdog.atomic(watchdog.directory()/'schedule.json', dict(paused=False))
    assert not watchdog.scheduling_paused(old)


def test_resume_skips_disabled_dates_but_keeps_three_day_calendar(monkeypatch):
    resumed = now().replace(year=2026, month=11, day=12, hour=15, minute=0)
    config = dict(anchor='2026-11-09', interval=3, installed_at=resumed.isoformat(), paused=False)
    monkeypatch.setattr(watchdog, 'timestamp', lambda: resumed)
    assert watchdog.operation(config, 'report', dry_run=True)['state'] == 'before_installation'
    monkeypatch.setattr(watchdog, 'timestamp', lambda: resumed.replace(day=15))
    result = watchdog.operation(config, 'report', dry_run=True)
    assert result == dict(state='would_launch', slot='report-20261115T1300')


def test_new_schema_round_trip_recovery_orchestration(store, config, tmp_path):
    """Code execution is stubbed; host validation of changed-schema references is real."""
    c = next(c for c in config[2] if c['id'] == 'kakao')
    health = HealthStore(store)
    health.record('broken', SourceSnapshot(company_id=c['id'], status='failed', code='parser_change'))
    registry = Registry(store, config[0], tmp_path)
    incident = health.incidents()[0]
    validation = verified_report(c)
    artifact = registry.activate(c, incident['id'], 'def collect(api,c): return {}', validation)
    assert registry.active(c)['id'] == artifact
    assert health.incidents()[0]['state'] == 'resolved'
    assert registry.rollback(c['id'], '실행 후 오류')
    assert registry.active(c) is None


def test_health_state_does_not_modify_existing_application_history(store, job):
    key = store.upsert(job)
    before = store.rows()[0]['payload']
    HealthStore(store).record('x', snapshot())
    assert store.rows()[0]['id'] == key
    assert store.rows()[0]['payload'] == before


def test_zero_catalog_with_maintenance_message_is_rejected(config, source_evidence):
    c, ledger, output = source_evidence
    ledger['a'*32]['text'] = json.dumps(dict(count=0, items=[], message='under maintenance'))
    output['jobs'] = []
    with pytest.raises(RepairRejected, match='점검'):
        validate_output(output, c, ledger, config[0])


def test_genuine_json_zero_requires_catalog_array(config, source_evidence):
    c, ledger, output = source_evidence
    ledger['a'*32]['text'] = json.dumps(dict(count=0, items=[]))
    output['jobs'] = []
    result = validate_output(output, c, ledger, config[0])
    assert result.proof.empty_confirmed and result.status == 'ok'


def test_unrelated_job_body_cannot_supply_jd_evidence(config, source_evidence):
    c, ledger, output = source_evidence
    value = json.loads(ledger['a'*32]['text'])
    value['unrelated'] = value['items'][0]['body']
    ledger['a'*32]['text'] = json.dumps(value)
    output['jobs'][0]['body']['pointer'] = '/unrelated'
    with pytest.raises(RepairRejected):
        validate_output(output, c, ledger, config[0])


def test_metadata_from_another_posting_is_rejected(config, source_evidence):
    c, ledger, output = source_evidence
    value = json.loads(ledger['a'*32]['text'])
    value['other'] = dict(employment_type='정규직')
    ledger['a'*32]['text'] = json.dumps(value)
    output['jobs'][0]['employment_type'] = dict(evidence_id='a'*32, pointer='/other/employment_type')
    with pytest.raises(RepairRejected, match='필드'):
        validate_output(output, c, ledger, config[0])


def test_public_evidence_does_not_freeze_response_encoding(config, monkeypatch):
    import httpx
    import career_scout.collectors as collectors
    async def run():
        tools = PublicTools(config[0])
        response = httpx.Response(200, headers={'content-type':'text/html'},
                                  content=b'<meta charset="euc-kr">'+bytes([0xb0,0xa1]),
                                  request=httpx.Request('GET','https://example.com/jobs'))
        async def request(*a, **k):
            return response
        monkeypatch.setattr(tools.http, 'request', request)
        monkeypatch.setattr(collectors, 'public_url', lambda url: None)
        try:
            actual = await tools._raw('GET','https://example.com/jobs')
            actual.encoding = 'cp949'
            assert '가' in actual.text
        finally:
            await tools.close()
    asyncio.run(run())


def test_sending_or_sent_mail_after_crash_is_never_resent(monkeypatch):
    at = now().replace(year=2026, month=11, day=12, hour=15)
    config = dict(anchor='2026-11-09', interval=3, installed_at=at.replace(day=10).isoformat())
    monkeypatch.setattr(watchdog, 'timestamp', lambda: at)
    slot, _ = watchdog.due_slot('report', config)
    path = watchdog.directory()/'operations'/(slot+'.json')
    monkeypatch.setattr(watchdog.subprocess, 'Popen', lambda *a,**k: (_ for _ in ()).throw(AssertionError('must not send')))
    for status in ('sending', 'sent', 'uncertain'):
        watchdog.atomic(path, dict(status='running', mail_id='a'*16))
        monkeypatch.setattr(watchdog, 'delivery_status', lambda ident: status)
        assert watchdog.operation(config, 'report', retry=True)['state'] == 'already_handled'


def test_corrupted_active_plugin_is_quarantined_before_execution(store, config, tmp_path):
    c = next(c for c in config[2] if c['id'] == 'kakao')
    class Runner:
        image_id = 'sha256:test'
        async def ready(self):
            return True
    registry = Registry(store, config[0], tmp_path, runner=Runner())
    registry.activate(c, 'test', 'def collect(api,c): return {}', verified_report(c))
    from pathlib import Path
    Path(registry.active(c)['path']).write_text('not valid python!!!', encoding='utf-8')
    assert asyncio.run(registry.execute(NS(), c)) is None
    assert registry.active(c) is None


def test_wrong_company_jobs_are_removed_from_ingestion(config):
    c = next(c for c in config[2] if c['id'] == 'kakao')
    r = Collection('kakao', [Job(company_id='other', company='other', tier=3,
                               title='AI Agent Engineer', url='https://careers.kakao.com/jobs/1')], 'ok')
    assert observe_result(r, c, config[0]).code == 'employer_mismatch'
    assert not r.jobs


def test_url_discovery_requires_an_official_link_and_same_tenant(config):
    c = next(c for c in config[2] if c['id'] == 'kakao')
    broker = PublicBroker(NS(), c)
    broker.ledger['root'] = dict(url=c['url'], text='<a href="https://job-boards.greenhouse.io/kakao">Careers</a>')
    assert broker.linked('https://job-boards.greenhouse.io/kakao')
    assert not broker.linked('https://random.example/careers')
    broker.ledger['new'] = dict(url='https://job-boards.greenhouse.io/kakao', official_link=True,
        text='<a href="/kakao/jobs/1">AI Engineer</a><a href="/other/jobs/2">AI Engineer</a>')
    assert broker.linked('https://job-boards.greenhouse.io/kakao/jobs/1')
    assert not broker.linked('https://job-boards.greenhouse.io/other/jobs/2')


def test_response_replay_matches_post_payload_and_never_fetches_live():
    records = [dict(id='old', url='https://example.com/jobs', kind='http',
                    request=dict(method='POST', json=dict(page=1)))]
    broker = ReplayBroker(records)
    assert asyncio.run(broker.call('fetch', dict(url=records[0]['url'], method='POST', json=dict(page=1))))['id'] == 'old'
    with pytest.raises(RepairRejected):
        asyncio.run(broker.call('fetch', dict(url=records[0]['url'], method='POST', json=dict(page=2))))


def test_docker_launch_has_no_network_secrets_or_writable_host_mount(config, source_evidence, tmp_path, monkeypatch):
    c, ledger, output = source_evidence
    calls = []
    class Input:
        def write(self, data):
            assert b'openai' not in data.lower() and b'password' not in data.lower()
        async def drain(self):
            pass
        def close(self):
            pass
    class Output:
        async def readline(self):
            return (json.dumps(dict(op='result', result=output))+'\n').encode()
    class Process:
        stdin, stdout, returncode = Input(), Output(), 0
        async def wait(self):
            return 0
    async def create(*args, **kwargs):
        calls.append(args)
        return Process()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create)
    runner = DockerRunner(tmp_path)
    runner.settings, runner.image_id = config[0], 'sha256:pinned'
    monkeypatch.setattr(runner, 'executable', lambda: 'docker')
    broker = ReplayBroker(list(ledger.values()))
    broker.ledger = ledger
    result = asyncio.run(runner.run('def collect(api,c): return {}', c, broker))
    argv = calls[0]
    assert result.jobs[0].verified
    assert argv[argv.index('--network')+1] == 'none'
    assert argv[argv.index('--user')+1] == '65534:65534'
    assert '--read-only' in argv and '--env' not in argv and '-v' not in argv
    mounts = [argv[i+1] for i, v in enumerate(argv) if v == '--mount']
    assert len(mounts) == 2 and all(m.endswith(',readonly') for m in mounts)
    assert all('scout.sqlite3' not in m and 'docker.sock' not in m for m in mounts)
    assert 'sha256:pinned' in argv and runner.image not in argv


def test_watchdog_retries_at_most_once_and_survives_import_failure(monkeypatch):
    at = now().replace(year=2026, month=11, day=12, hour=15)
    config = dict(anchor='2026-11-09', interval=3, installed_at=at.replace(day=10).isoformat(),
                  python='missing-python.exe', root='missing-root')
    monkeypatch.setattr(watchdog, 'timestamp', lambda: at)
    slot, _ = watchdog.due_slot('report', config)
    monkeypatch.setattr(watchdog.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert watchdog.operation(config, 'report')['status'] == 'failed'
    assert watchdog.operation(config, 'report', retry=True)['retries'] == 1
    assert watchdog.operation(config, 'report', retry=True)['state'] == 'retry_limit'
    assert watchdog.read(watchdog.directory()/'operations'/(slot+'.json'))['phase'] == 'bootstrap_failed'


def test_watchdog_stalled_owned_process_is_reported_without_touching_other_pids(monkeypatch):
    at = now().replace(year=2026, month=11, day=12, hour=15)
    config = dict(anchor='2026-11-09', interval=3, installed_at=at.replace(day=10).isoformat())
    monkeypatch.setattr(watchdog, 'timestamp', lambda: at)
    slot, _ = watchdog.due_slot('report', config)
    watchdog.atomic(watchdog.directory()/'operations'/(slot+'.json'),
                    dict(pid=123, status='running', started=(at-timedelta(minutes=25)).isoformat(),
                         heartbeat=(at-timedelta(minutes=20)).isoformat()))
    monkeypatch.setattr(watchdog, 'alive', lambda pid: pid == 123)
    monkeypatch.setattr(watchdog, 'owned_process', lambda s,c: False)
    assert not watchdog.inspect_operations(config, dry_run=True) or all(
        r['state'] != 'would_stop_owned_stalled_process' for r in watchdog.inspect_operations(config, dry_run=True))
    monkeypatch.setattr(watchdog, 'owned_process', lambda s,c: True)
    assert watchdog.inspect_operations(config, dry_run=True)[0]['state'] == 'would_stop_owned_stalled_process'


@pytest.mark.parametrize('initial_budget_failure', [False, True])
def test_auto_repair_graph_verifies_fixtures_twice_then_promotes(store, config, source_evidence, tmp_path, monkeypatch, initial_budget_failure):
    """Real graph/host reference checks; Docker and model transport are fakes."""
    import career_scout.maintenance as module
    c, ledger, output = source_evidence
    settings = dict(config[0])
    class Runner:
        image_id = 'sha256:test'
        calls = 0
        async def ready(self):
            return True
        async def run(self, code, company, broker):
            self.calls += 1
            return validate_output(output, company, ledger, settings)
    runner = Runner()
    registry = Registry(store, settings, tmp_path, runner=runner)
    positive = validate_output(output, c, ledger, settings)
    registry.fixture(c, positive, list(ledger.values()))
    class Tools:
        vault = NS(for_company=lambda ident: list(ledger.values()))
        health_store = None
        def __init__(self, settings):
            self.settings = settings
        async def collect(self, company):
            return Collection(company['id'], status='failed', detail='JSON schema changed')
        collect_primary = collect
        def evidence_for(self, company):
            return list(ledger.values())
        async def close(self):
            pass
    monkeypatch.setattr('career_scout.collectors.PublicTools', Tools)
    monkeypatch.setattr(module, 'RepairAgent', lambda *a: NS(draft=lambda *a: dict(diagnosis='field rename', source_code='def collect(api,c): return {}')))
    clock = [now()]
    monkeypatch.setattr(module, 'now', lambda: clock[0])
    async def sleep(seconds):
        assert seconds >= 30
        clock[0] += timedelta(seconds=seconds)
    monkeypatch.setattr(module.asyncio, 'sleep', sleep)
    regressions = []
    async def regression():
        regressions.append(True)
    monkeypatch.setattr(registry, 'regression', regression)
    if initial_budget_failure:
        def exhausted(*args):
            raise BudgetExceeded()
        monkeypatch.setattr(module, 'RepairAgent', lambda *a: NS(draft=exhausted))
        first = asyncio.run(Maintenance(settings, [c], store, registry=registry).run(repair=True))
        assert first['repairs'][0]['state'] == 'deferred' and runner.calls == 0
        assert store.db.execute('SELECT outcome FROM repair_attempts').fetchone()[0] == 'budget_deferred'
        monkeypatch.setattr(module, 'RepairAgent', lambda *a: NS(draft=lambda *a: dict(diagnosis='field rename', source_code='def collect(api,c): return {}')))
    result = asyncio.run(Maintenance(settings, [c], store, registry=registry).run(repair=True))
    assert result['repairs'][0]['state'] == 'applied'
    assert runner.calls == 3 and regressions == [True]
    assert registry.active(c) and registry.health.incidents()[0]['state'] == 'resolved'
    assert store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM usage').fetchone()[0] == 0
