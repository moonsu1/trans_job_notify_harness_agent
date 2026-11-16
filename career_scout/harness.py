from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agent import Agent, AgentError, MissingCredential
from .collectors import PublicTools
from .core import Job, cache_fingerprint, prefilter, role_relevant
from .mailer import send_mail
from .reporting import build_report
from .storage import BudgetExceeded
from .planning import search_plan
from .health import HealthStore, SourceSnapshot, observe_result

log = logging.getLogger('career_scout')


class RunState(TypedDict, total=False):
    run_id: str
    summary: dict
    report: str
    mail_id: str
    mail_status: str
    mail_error: str
    status: str


class Harness:
    def __init__(self, settings, profile, companies, store, *, offline=False, no_api=False,
                 send=False, browser=True, only=None):
        self.settings, self.profile, self.companies, self.store = settings, profile, companies, store
        self.offline, self.no_api, self.send = offline, no_api, send
        self.browser, self.only = browser, set(only or [])
        self.agent = Agent(settings, profile, store) if not no_api else None
        self.started = time.monotonic()
        self.run_limit = 7200 if settings.get('_full_scan') else settings['max_run_seconds']
        self.summary = {'collected': 0, 'analyzed': 0, 'warnings': []}
        self.health = HealthStore(store)

    def left(self):
        return self.run_limit-(time.monotonic()-self.started)

    def warning(self, message):
        if message not in self.summary['warnings']:
            self.summary['warnings'].append(message)
            log.warning(message)

    async def collect(self, state):
        from .watchdog import heartbeat
        heartbeat('collect', run_id=state['run_id'])
        run_id = state['run_id']
        if self.offline:
            self.warning('오프라인 실행: 이번 실행에서 접수 상태를 갱신하지 않음')
            return state
        tools = PublicTools(self.settings, browser=self.browser)
        tools.health_store = self.health
        semaphore = asyncio.Semaphore(3)

        async def one(company):
            async with semaphore:
                if self.settings.get('_resume_run') and any(s['company_id'] == company['id']
                                                           for s in self.store.sources(run_id)):
                    log.info('수집 캐시 재사용: %s', company['name'])
                    return
                if self.left() < 100:
                    self.store.source(run_id, company['id'], 'skipped', '실행 시간 한도')
                    return
                log.info('수집 시작: %s', company['name'])
                try:
                    result = await asyncio.wait_for(tools.collect(company),
                                                    timeout=min(company.get('timeout_seconds', self.settings.get('source_timeout_seconds', 90)), self.left()-70))
                    records = tools.evidence_for(company)
                    snapshot = observe_result(result, company, self.settings,
                                              schema_hash=records[-1].get('schema_hash', '') if records else '',
                                              evidence_ids=[r['id'] for r in records])
                    self.health.record(run_id, snapshot)
                    result.status = 'partial' if any(c['status'] != 'ok' for c in result.channels) and snapshot.status == 'ok' else snapshot.status
                    from .maintenance import Registry
                    Registry(self.store, self.settings).fixture(company, result, records)
                    for channel in result.channels:
                        extra = SourceSnapshot(company_id=company['id'], **channel)
                        from .health import fault_type
                        extra.code = fault_type(extra.detail) if extra.status != 'ok' else ''
                        if extra.code in ('access_blocked', 'login_required', 'maintenance', 'rate_limit', 'transient'):
                            extra.access = extra.code
                        self.health.record(run_id, extra)
                    self.store.source(run_id, company['id'], result.status, result.detail, len(result.jobs))
                    for job in result.jobs:
                        self.store.upsert(job)
                    self.summary['collected'] += len(result.jobs)
                    log.info('수집 완료: %s %s %d건%s', company['name'], result.status, len(result.jobs),
                             ' — '+result.detail if result.status != 'ok' else '')
                    heartbeat('collect', company_id=company['id'], run_id=run_id)
                except Exception as exc:
                    self.store.source(run_id, company['id'], 'failed', type(exc).__name__)
                    self.health.record(run_id, SourceSnapshot(company_id=company['id'], status='failed',
                                       code='transient' if isinstance(exc, TimeoutError) else 'parser_change', detail=str(exc)[:300]))
                    log.warning('수집 실패: %s %s', company['name'], type(exc).__name__)

        try:
            await asyncio.gather(*(one(c) for c in self.companies if not self.only or c['id'] in self.only))
        finally:
            await tools.close()
        return state

    async def discover(self, state):
        from .watchdog import heartbeat
        heartbeat('discover', run_id=state['run_id'])
        if self.offline or self.no_api:
            return state
        if not self.agent.client:
            self.warning('OpenAI 인증 미등록: 유료 검색·분석 대기')
            return state
        if self.left() < 180:
            self.warning('시간 한도로 추가 검색 생략')
            return state
        companies = [c for c in self.companies if not self.only or c['id'] in self.only]
        searches = search_plan(companies, bool(self.settings.get('_full_scan')), self.settings['max_search_calls'])
        visited = set()
        tools = PublicTools(self.settings, browser=self.browser)
        try:
            for group, track in searches:
                if not group or self.left() < 170:
                    continue
                search_id = 'search-'+group[0]['id']+'-'+track
                if self.settings.get('_resume_run') and any(s['company_id'] == search_id
                                                           for s in self.store.sources(state['run_id'])):
                    log.info('검색 캐시 재사용: %s / %s', group[0]['name'], track)
                    continue
                try:
                    results = await asyncio.to_thread(self.agent.search, group, track)
                    count = 0
                    candidates = results if self.settings.get('_full_scan') else results[:20]
                    for company, url in candidates:
                        if (company['id'], url) in visited:
                            continue
                        visited.add((company['id'], url))
                        if self.left() < 100:
                            break
                        try:
                            job = await asyncio.wait_for(tools.detail(url, company), 30)
                            if role_relevant(job, self.settings):
                                self.store.upsert(job)
                                count += 1
                        except Exception:
                            # Preserve a discovered link without treating a search snippet as a verified JD.
                            self.store.upsert(Job(company_id=company['id'], company=company['name'],
                                                  tier=company['tier'], url=url, source_url=url,
                                                  title='검색에서 발견 — 원문 확인 필요'))
                    log.info('검색 보완: %s / %s / %d건', ', '.join(c['name'] for c in group), track, count)
                    self.store.source(state['run_id'], 'search-'+group[0]['id']+'-'+track, 'partial',
                                      '공식 도메인 검색 보완; 검색 노출 범위만 확인', count)
                except BudgetExceeded:
                    self.warning('API 예산 또는 검색 횟수 한도로 추가 검색 중단')
                    break
                except AgentError as exc:
                    self.warning(str(exc))
                    break
        finally:
            await tools.close()
        return state

    def analyze(self, state):
        from .watchdog import heartbeat
        heartbeat('analyze', run_id=state['run_id'])
        def rank(row):
            job = json.loads(row['payload'])
            title = job['title']
            direct = bool(re.search(r'engineer|엔지니어|개발|architect|아키텍트', title, re.I)
                          and re.search(r'\bAI\b|\bML\b|LLM|Agent|RAG|Machine Learning|생성형|인공지능|플랫폼', title, re.I))
            return job['tier'], not direct, row['first_seen']
        rows = sorted(self.store.rows(), key=rank)
        for row in rows:
            job = Job.model_validate_json(row['payload'])
            reason = '세부 직무별 공고로 분리된 통합 공고' if row.get('superseded_group') else prefilter(job, self.settings)
            if not reason and not role_relevant(job, self.settings) and job.verified:
                reason = 'AI 개발 직무 관련 근거 부족'
            self.store.exclude(row['id'], reason)
            if reason or row['feedback'] in ('applied', 'ignored'):
                continue
            if row['assessed_hash'] == row['content_hash'] and row['fingerprint'] == self.settings['fingerprint']:
                continue
            if not job.verified or not job.body:
                continue
            if self.only and job.company_id not in self.only:
                continue
            if self.no_api or not self.agent or not self.agent.client:
                self.warning('이번 실행은 수집·필터 점검만 수행해 새 공고의 적합성 분석은 대기 중입니다.'
                             if self.no_api else 'OpenAI 인증 미등록: 적합성 분석 대기')
                continue
            if self.left() < 165:
                self.warning('실행 시간 한도: 미분석 공고는 다음 실행으로 이월')
                break
            # Failed analyses are retried next run, with the same per-call budget protections.
            try:
                prior = None
                if (row['assessment'] and row['assessed_hash'] == row['content_hash']
                    and row['fingerprint'] == cache_fingerprint(self.settings, self.profile, '2026-11-09-role-scope-v3')):
                    prior = json.loads(row['assessment'])
                result = self.agent.analyze(job, prior_assessment=prior)
                self.store.save_analysis(row['id'], result.model_dump(), self.settings['fingerprint'])
                self.summary['analyzed'] += 1
                log.info('분석 완료: %s / %s', job.company, job.title)
                heartbeat('analyze', run_id=state['run_id'])
            except BudgetExceeded:
                self.warning('API 예산 부족: 미분석 공고는 다음 실행으로 이월')
                break
            except (AgentError, MissingCredential) as exc:
                self.store.analysis_failed(row['id'], str(exc))
                self.warning(str(exc))
                if '요청 실패' in str(exc):
                    break
        return state

    def report(self, state):
        from .watchdog import heartbeat
        heartbeat('report', run_id=state['run_id'])
        state['summary'] = self.summary
        self.summary['budget_override'] = bool(self.settings.get('_unlimited_budget'))
        self.summary['maintenance'] = self.health.summary()
        usage = self.store.db.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage WHERE run_id=?',
                                      (state['run_id'],)).fetchone()[0]
        self.summary['run_cost_usd'] = usage
        report = build_report(self.store, self.settings, self.profile, state['run_id'], self.summary)
        state['report'] = report['report']
        if self.send:
            mail_id = self.store.create_mail(report['subject'], report['html'], report['markdown'], report['items'])
            state['mail_id'] = mail_id
            heartbeat('mail_prepared', run_id=state['run_id'], mail_id=mail_id, mail_status='pending')
            state['mail_status'] = send_mail(self.store, self.settings, mail_id)
            if state['mail_status'] != 'sent':
                error = self.store.db.execute('SELECT error FROM outbox WHERE id=?', (mail_id,)).fetchone()[0]
                log.warning('메일 %s: %s', state['mail_status'], error)
        else:
            state['mail_status'] = 'preview'
        source_failures = any(s['status'] != 'ok' for s in self.store.sources(state['run_id'])
                              if s['company_id'] in self.settings.get('_company_metadata', {}))
        state['status'] = ('degraded' if self.summary['warnings'] or source_failures
                           or state['mail_status'] in ('failed', 'uncertain', 'pending') else 'complete')
        if state.get('mail_id') and state['mail_status'] != 'sent':
            state['mail_error'] = self.store.db.execute('SELECT error FROM outbox WHERE id=?',
                                                       (state['mail_id'],)).fetchone()[0]
        state['summary'] = self.summary
        heartbeat('finished', run_id=state['run_id'], mail_status=state['mail_status'], mail_id=state.get('mail_id'))
        return state

    async def run(self):
        run_id = self.store.start_run(self.settings.get('_resume_run'))
        if self.settings.get('_resume_run'):
            self.summary['collected'] = sum(s['count'] for s in self.store.sources(run_id)
                                            if not s['company_id'].startswith('search-'))
        graph = StateGraph(RunState)
        for name, func in [('collect', self.collect), ('discover', self.discover),
                           ('analyze', self.analyze), ('report', self.report)]:
            graph.add_node(name, func)
        graph.add_edge(START, 'collect')
        graph.add_edge('collect', 'discover')
        graph.add_edge('discover', 'analyze')
        graph.add_edge('analyze', 'report')
        graph.add_edge('report', END)
        try:
            result = await graph.compile().ainvoke({'run_id': run_id})
            self.store.finish_run(run_id, {**self.summary, 'mail_status': result['mail_status']},
                                  result.get('status', 'complete'))
            return result
        except Exception as exc:
            self.warning('실행 오류: '+type(exc).__name__)
            try:
                result = self.report({'run_id': run_id})
            except Exception:
                self.store.finish_run(run_id, self.summary, 'failed')
                raise
            self.store.finish_run(run_id, self.summary, 'failed')
            return result
