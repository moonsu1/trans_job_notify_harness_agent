from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from .core import ROOT, Job, assessment_view, digest, now, prefilter
from .digest import render_digest
from .policy import ROLE_LABELS, finance_employment


def safe_link(url):
    p = urlsplit(url)
    return url if p.scheme in ('http', 'https') and not p.username and not p.password else ''


def report_rows(store, settings):
    rows = []
    for row in store.rows():
        job = Job.model_validate_json(row['payload'])
        current = row['assessed_hash'] == row['content_hash'] and row['fingerprint'] == settings['fingerprint']
        assessment = json.loads(row['assessment']) if row['assessment'] and current else None
        view = assessment_view(assessment, job.verified)
        employment, employment_reason = finance_employment(job, settings, assessment)
        row['exclusion'] = ('세부 직무별 공고로 분리된 통합 공고' if row.get('superseded_group') else
                            prefilter(job, settings) or (employment_reason if employment == 'excluded' else row['exclusion']))
        if row['exclusion']:
            view.update(grade='추천 제외', recommended=False, score=None)
        if employment == 'unknown':
            view.update(grade='판단 보류', recommended=False)
        try:
            stale = now()-datetime.fromisoformat(job.checked_at) > timedelta(hours=30)
        except ValueError:
            stale = True
        if stale:
            view.update(grade='판단 보류', recommended=False)
        row.update(job=job, analysis=assessment, view=view,
                   stale=stale,
                   employment_gate=employment, employment_reason=employment_reason,
                   notify_hash=digest([row['content_hash'], settings['fingerprint']]))
        rows.append(row)
    rows.sort(key=lambda r: (r['job'].tier, -(r['view']['score'] or 0),
                             r['job'].deadline or '9999', r['job'].company, r['job'].title))
    return rows


def selection(store, rows, include_weekly=True, include_all=False, repeat=False):
    selected = []
    uncertain = set() if repeat else store.uncertain_notifications()
    today = now().date()
    weekly = f'weekly-{today.isocalendar().year}-{today.isocalendar().week}'
    for r in rows:
        if r.get('employment_gate') in ('unknown', 'excluded'):
            continue
        if (r['id'], r['notify_hash']) in uncertain:
            continue
        review = (r['analysis'] and not r['stale'] and r['view']['grade'] == '판단 보류'
                  and (r['view']['score'] or 0) >= 60
                  and not r['analysis']['hard_failures'] and not r['analysis']['scope_failure'])
        if ('skcareers.com' in r['job'].url or 'samsungcareers.com' in r['job'].url) and not r['job'].employer_verified:
            review = False
        if r['exclusion'] or r['feedback'] in ('applied', 'ignored') or not (r['view']['recommended'] or review):
            continue
        kind = None
        if repeat and store.has_notification(r['id']):
            if not store.has_notification(r['id'], r['notify_hash']):
                kind = 'updated'
        elif not store.notified(r['id'], r['notify_hash'], 'new'):
            kind = 'new'
        if kind is None and r['job'].deadline:
            try:
                due = date.fromisoformat(r['job'].deadline[:10])
                if 0 <= (due-today).days <= 3 and (repeat or not store.notified(r['id'], r['notify_hash'], 'deadline')):
                    kind = 'deadline'
            except ValueError:
                pass
        if kind is None and repeat:
            kind = 'repeat'
        if kind is None and include_weekly and today.weekday() == 0:
            if not store.notified(r['id'], r['notify_hash'], weekly):
                kind = weekly
        if kind is None and include_all:
            kind = 'full-test'
        if kind:
            selected.append((r, kind))
    return selected


def application_stats(store):
    rows = list(store.db.execute('SELECT * FROM applications'))
    decided = [r for r in rows if r['screening'] in ('pass', 'fail')]
    passed = sum(r['screening'] == 'pass' for r in decided)
    rate = f'{passed/len(decided):.1%}' if decided else '산정 불가'
    return f'지원 기록 {len(rows)}건 / 서류 결과 확인 {len(decided)}건 / 실제 서류 통과율 {rate} (예측 확률 아님)'


def build_report(store, settings, profile, run_id, summary):
    rows = report_rows(store, settings)
    repeat = settings.get('report_repeat_candidates', True)
    all_selected = selection(store, rows, include_all=bool(settings.get('_full_scan')), repeat=repeat)
    # Each scheduled digest is a complete current list, including previously sent jobs.
    chosen = all_selected if repeat or now().weekday() == 0 or settings.get('_full_scan') else all_selected[:settings['max_report_jobs']]
    pending = [r for r in rows if not r['exclusion'] and r['feedback'] not in ('applied', 'ignored')
               and (not r['analysis'] or r['view']['grade'] == '판단 보류')]
    # A manually checked image role can be time-sensitive while the API is
    # budget-paused. Expose its original link separately, without inventing a score.
    discoveries = [r for r in pending if r['job'].verified and not r['analysis'] and not r['stale']
                  and r['job'].source_note.startswith('이미지 원문을 직접 대조')
                  and r.get('employment_gate') not in ('unknown', 'excluded')
                  and (repeat or not store.notified(r['id'], r['notify_hash'], 'discovery'))
                  and (repeat or (r['id'], r['notify_hash']) not in store.uncertain_notifications())]
    discovered = discoveries if repeat else discoveries[:3]
    sources = store.sources(run_id)
    monthly = store.spend(now().isoformat()[:7])
    new_count = sum(kind == 'new' for _, kind in chosen)
    subject = (f'[Agent 채용] 신규 {new_count}건 · 기존 {len(chosen)-new_count}건 | {now():%m/%d}' if repeat else
               f'[Agent 채용] 추천·검토 {len(chosen)}건 | {now():%m/%d}')
    if discovered:
        subject += f' · 원문 확인 후보 {len(discovered)}건'

    def job_card(r, kind='전체 목록'):
        job, a, view = r['job'], r['analysis'], r['view']
        fields = [
            ('우선순위 / 알림', f'{job.tier}순위 / {kind}'),
            ('조직', (a or {}).get('organization') or job.organization or '미기재'),
            ('근무지', (a or {}).get('location') or job.location or '미확인'),
            ('경력 요건', (a or {}).get('experience_requirement') or job.experience or '미기재'),
            ('고용형태', (a or {}).get('employment_type') or job.employment_type or '미기재'),
            ('마감 / 상태', f'{job.deadline or "미기재"} / {job.status}'),
            ('처우 원문', (a or {}).get('compensation_quote') or job.compensation or '미공개'),
            ('희망 직무 판정', ROLE_LABELS.get((a or {}).get('role_scope', {}).get('family', 'unclear'), '직무 확인 필요')),
            ('이력 적합성', f'{view["score"]}/100' if view['score'] is not None else
             '산정 제외 — 희망 직무 범위 밖' if (a or {}).get('role_scope', {}).get('decision') == 'out_of_scope' or r['exclusion'] else '평가 대기'),
            ('예상 서류 통과 가능성', view['grade']),
            ('예상 합격 확률', view['probability']),
            ('원문 / 확인 시각', f'{"확인" if job.verified else "미확인"} / {job.checked_at}'),
            ('공고 출처 종류', '채용 플랫폼·공공기관 재게시 원문' if job.source_kind == 'mirror' else '기업 공식 채용 원문'),
        ]
        if r['stale']:
            fields.append(('재확인 필요', '원문 확인 후 30시간 경과 — 현재 접수 상태 확인 필요'))
        if job.source_note:
            fields.append(('원문 확인 방식', job.source_note))
        if r['exclusion']:
            fields.append(('제외 사유', r['exclusion']))
        if r['analysis_error']:
            fields.append(('분석 상태', r['analysis_error']))
        if r.get('employment_gate') not in ('not_applicable', None):
            fields.append(('금융 정규직 확인', '확인' if r['employment_gate'] == 'confirmed'
                           else r.get('employment_reason') or '미확인'))
        if a:
            role = a.get('role_scope', {})
            fields.append(('주업무 판정 근거', role.get('reason', '직무 판정 전의 이전 평가')))
            fields.append(('주업무 원문', ' / '.join(role.get('duty_quotes', [])) or '미확인'))
            for name, key in [('주요 업무', 'duties'), ('필수 자격', 'required'), ('우대사항', 'preferred'),
                              ('맞는 경험', 'strengths'), ('부족·확인할 조건', 'gaps'),
                              ('중요 미확인 조건', 'important_unknowns'), ('필수 조건 불충족', 'hard_failures')]:
                fields.append((name, ' / '.join(a[key]) or '없음/미기재'))
            fields.append(('판단 근거', a['rationale']))
            fields.append(('기업·조직·지역 범위 확인', a['scope_failure'] or '명확한 불일치 미확인'))
            for e in a['evidence']:
                labels = {'role': 'AI 주업무 대응', 'technical': 'AI 전문 기술',
                          'production': 'AI 서비스 운영·평가', 'leadership': 'AI 과제 기획·리딩'}
                fields.append((f'점수 근거 {labels.get(e["category"], e["category"])} {e["points"]}점',
                               f'JD: “{e["jd_quote"]}” → 경력 {e["profile_id"]}: {e["reason"]}'))
            ids = set(a['emphasis_profile_ids'])
            fields.append(('지원 시 강조점', ' / '.join(e['description'] for e in profile['experience']
                                                    if e['id'] in ids) or '검토 필요'))
        url = safe_link(job.url)
        source = safe_link(job.source_url)
        title = f'{job.company} · {job.title}'
        m = '\n\n## '+title+'\n\n'+'\n'.join(f'- {k}: {v}' for k, v in fields)
        m += f'\n- 공고 ID: {r["id"]}\n- 원문: {url}\n- 수집 출처: {source}\n'
        card = f'<article style="padding:16px;overflow-wrap:anywhere;"><h2>{html.escape(title)}</h2>'
        card += ''.join(f'<p style="line-height:1.7;"><strong>{html.escape(k)}</strong><br>'
                        f'{html.escape(str(v))}</p>' for k, v in fields)
        card += f'<p><a href="{html.escape(url, quote=True)}">공고 원문</a> · '
        card += f'<a href="{html.escape(source, quote=True)}">수집 출처</a></p><small>ID: {r["id"]}</small></article>'
        return m, card

    email_md, email_html = render_digest(chosen, pending, sources, settings,
                                         dict(summary, monthly_spend=monthly,
                                              uncertain_delivery=bool(store.uncertain_notifications())),
                                         unseen=len(all_selected)-len(chosen), discovered=discovered)
    full_md = email_md+'\n\n# 저장된 전체 공고 및 제외 내역\n'
    full_h = [email_html.removesuffix('</body></html>'),
              '<main style="max-width:900px;margin:0 auto;padding:16px;">',
              '<details><summary style="padding:12px;">소스별 수집 상태 보기</summary>']
    full_h += ['<p style="overflow-wrap:anywhere;">'+html.escape(
        f'{s["company_id"]}: {s["status"]} — {s["detail"]}')+'</p>' for s in sources]
    full_h += ['</details><h2>공고별 상세 근거·제외 내역</h2>']
    for row in rows:
        m, card = job_card(row)
        full_md += m
        label = html.escape(row['job'].company+' · '+row['job'].title)
        full_h.append('<details style="margin:8px 0;border:1px solid #dbe2ee;border-radius:8px;">'
                      f'<summary style="padding:12px;line-height:1.6;overflow-wrap:anywhere;">{label}</summary>'
                      +card+'</details>')
    output = ROOT / 'reports'
    output.mkdir(exist_ok=True)
    name = f'{now():%Y%m%d-%H%M%S}-{run_id}'
    for filename, content in [(name+'.html', ''.join(full_h)+'</main></body></html>'),
                              (name+'.md', full_md), ('latest.html', ''.join(full_h)+'</main></body></html>'),
                              ('latest.md', full_md), ('latest-email.html', email_html), ('latest-email.md', email_md)]:
        (output / filename).write_text(content, encoding='utf-8')
    items = [{'id': r['id'], 'hash': r['notify_hash'], 'kind': kind} for r, kind in chosen]
    items.extend({'id': r['id'], 'hash': r['notify_hash'], 'kind': 'discovery'} for r in discovered)
    return {'subject': subject, 'html': email_html, 'markdown': email_md, 'items': items,
            'report': str(output / (name+'.html'))}
