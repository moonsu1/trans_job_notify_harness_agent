"""Compact email content with an inline, single-column layout for mobile clients."""
from __future__ import annotations

import html
from collections import Counter
from datetime import date
from urllib.parse import urlsplit

from .core import now, plain


def concise(value, limit=85):
    value = plain(str(value or ''))
    return value if len(value) <= limit else value[:limit-1].rstrip()+'…'


def small_list(values, fallback, limit=100):
    values = list(dict.fromkeys(plain(v) for v in values if plain(v)))
    return concise(' · '.join(values[:2]) or fallback, limit)


def card_summary(row):
    job, a, view = row['job'], row['analysis'] or {}, row['view']
    scope = a.get('role_scope', {})
    reason = scope.get('reason', '')
    # The classifier already returns a short deliverable; do not paste its source paragraphs.
    if reason.startswith('주 산출물:'):
        duty = reason.removeprefix('주 산출물:').split('. ', 1)[0].rstrip('.')
    else:
        duty = a.get('rationale', '') or reason or '공고 원문에서 주요 업무를 확인해 주세요.'
    location = concise(a.get('location') or job.location or '근무지 확인 필요', 35)
    deadline = job.deadline[:10] if job.deadline else '마감 미공개'
    grade = '조건 확인 필요' if view['grade'] == '판단 보류' else view['grade']
    return {
        'title': f'{job.company} · {job.title}',
        'meta': f'{job.tier}순위 · 적합성 {view["score"]}점 · 서류 {grade}',
        'location': f'{location} · {deadline}',
        'duty': concise(duty, 85),
        'strength': small_list(a.get('strengths', []), '상세 근거 확인 필요'),
        'check': small_list(a.get('important_unknowns', []) or a.get('gaps', []), '명확한 필수조건 불충족 없음'),
        'url': job.url if urlsplit(job.url).scheme in ('https', 'http')
        and not urlsplit(job.url).username and not urlsplit(job.url).password else '',
    }


def deadline_soon(job):
    try:
        return 0 <= (date.fromisoformat(job.deadline[:10])-now().date()).days <= 3
    except ValueError:
        return False


def render_digest(chosen, pending, sources, settings, summary, *, unseen=0, discovered=()):
    escape = html.escape
    stamp = now().strftime('%m월 %d일 %H:%M')
    repeat = settings.get('report_repeat_candidates', True)
    heading = f'Agent 채용 {"전체 " if repeat else ""}추천·검토 · {len(chosen)}건'
    intro = f'{stamp} · AI Agent 개발 / AI 기획 / PM·PL'
    if repeat:
        intro += ' · 이전에 보낸 공고도 포함'
    md = [f'# {heading}', intro]
    h = ['<!doctype html><html lang="ko"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         '<title>Agent 채용 추천</title></head>',
         '<body style="margin:0;padding:0;font-family:Arial,sans-serif;color:#172338;background:#f5f7fb;">',
         '<div style="width:100%;max-width:640px;box-sizing:border-box;margin:0 auto;padding:16px;">',
         f'<h1 style="font-size:23px;line-height:1.4;margin:8px 0;">{escape(heading)}</h1>',
         f'<p style="font-size:14px;line-height:1.7;margin:0 0 16px;">{escape(intro)}</p>']
    metadata = settings.get('_company_metadata', {})
    priority_gaps = [metadata[s['company_id']]['name'] for s in sources
                     if metadata.get(s['company_id'], {}).get('tier') == 1 and s['status'] != 'ok']
    if priority_gaps:
        message = '1순위 수집 미완료 · '+' / '.join(dict.fromkeys(priority_gaps))
        detail = '미확인 채널이 있어 해당 기업의 공고가 없다고 판단할 수 없습니다.'
        md.extend([message, detail])
        h.append('<div style="padding:12px;margin:0 0 16px;border:1px solid #d7aa53;border-radius:8px;'
                 'font-size:14px;line-height:1.7;"><strong>'+escape(message)+'</strong><br>'+detail+'</div>')
    if not chosen:
        message = '현재 조건에 맞는 추천·검토 공고가 없습니다.' if repeat else '이번에 알릴 신규·변경 추천 공고가 없습니다.'
        md.append(message)
        h.append(f'<p style="font-size:15px;line-height:1.7;">{message}</p>')
    sections = [('새로 추가된 공고', [(r, k) for r, k in chosen if k == 'new']),
                ('이전에 알려드린 공고', [(r, k) for r, k in chosen if k != 'new'])] if repeat else [('', chosen)]
    for section, entries in sections:
        if section:
            title = f'{section} · {len(entries)}건'
            md.append('\n## '+title)
            h.append('<h2 style="font-size:20px;line-height:1.5;margin:24px 0 12px;">'+escape(title)+'</h2>')
            if not entries:
                message = '새로 추가된 공고가 없습니다.' if section == '새로 추가된 공고' else '이전에 알려드린 공고가 없습니다.'
                md.append(message)
                h.append('<p style="font-size:14px;line-height:1.7;">'+message+'</p>')
        for row, kind in entries:
            card = card_summary(row)
            label = {'new': '신규', 'updated': '내용·평가 변경', 'repeat': '기존 검토',
                     'deadline': '마감 임박', 'full-test': '기존 검토'}.get(kind, '기존 검토')
            if repeat:
                if kind != 'deadline' and deadline_soon(row['job']):
                    label += ' · 마감 임박'
                card['meta'] = label+' · '+card['meta']
            md.extend([f'\n### {card["title"]}', card['meta'], card['location'],
                       f'- 업무: {card["duty"]}', f'- 맞는 점: {card["strength"]}',
                       f'- 확인할 점: {card["check"]}', f'[공고 보기]({card["url"]})'])
            h.extend(['<div style="display:block;box-sizing:border-box;max-width:100%;padding:16px;'
                      'margin:0 0 14px;border:1px solid #dbe2ee;border-radius:12px;background:#ffffff;">',
                      '<h3 style="font-size:18px;line-height:1.5;margin:0 0 8px;overflow-wrap:anywhere;">'
                      +escape(card['title'])+'</h3>',
                      '<p style="font-size:14px;line-height:1.7;margin:4px 0;"><strong>'
                      +escape(card['meta'])+'</strong><br>'+escape(card['location'])+'</p>'])
            for label, key in [('업무', 'duty'), ('맞는 점', 'strength'), ('확인할 점', 'check')]:
                h.append('<p style="font-size:14px;line-height:1.7;margin:8px 0;overflow-wrap:anywhere;">'
                         f'<strong>{label}</strong> · {escape(card[key])}</p>')
            h.append('<p style="margin:14px 0 0;"><a style="display:inline-block;padding:10px 16px;'
                     'font-size:15px;font-weight:bold;color:#ffffff;background:#2459ce;border-radius:8px;'
                     'text-decoration:none;" href="'+escape(card['url'], quote=True)+'">공고 보기 →</a></p></div>')
    if discovered:
        heading = '원문 확인 완료 · 적합성 평가 대기'
        md.extend([heading, '아래는 점수·서류 가능성을 아직 산정하지 않은 확인 후보입니다.'])
        h.append('<h2 style="font-size:18px;line-height:1.5;">'+heading+'</h2>'
                 '<p style="font-size:14px;line-height:1.7;">점수·서류 가능성은 아직 평가하지 않았습니다.</p>')
    for row in discovered:
        job = row['job']
        title = job.company+' · '+job.title
        meta = (job.location or '근무지 확인 필요')+' · '+job.employment_type+' · '+(job.deadline[:10] or '마감 미공개')
        lines = [plain(line) for line in job.body.splitlines() if len(plain(line)) > 40]
        duty = concise(lines[0] if lines else '공고 원문을 확인해 주세요.')
        md.extend(['## '+title, meta, duty, '[공고 보기]('+job.url+')'])
        h.append('<div style="padding:16px;margin:0 0 14px;background:#ffffff;border:1px solid #dbe2ee;'
                 'border-radius:12px;overflow-wrap:anywhere;"><h3 style="font-size:17px;line-height:1.5;margin:0;">'
                 +escape(title)+'</h3><p style="font-size:14px;line-height:1.7;">'+escape(meta)
                 +'</p><p style="font-size:14px;line-height:1.7;">'+escape(duty)
                 +'</p><a style="font-size:15px;" href="'+escape(job.url, quote=True)+'">공고 보기 →</a></div>')
    official = [s for s in sources if not s['company_id'].startswith('search-')]
    counts = Counter(s['status'] for s in official)
    searched = sum(s['company_id'].startswith('search-') for s in sources)
    footer = [
        '적합성 점수는 합격률이 아닙니다. 최종 합격 확률은 데이터 부족으로 산정하지 않습니다.',
        f'직접 수집 정상 {counts["ok"]} · 부분 {counts["partial"]} · 실패 {counts["failed"]} / 검색 보완 {searched}회',
        f'원문·조건 확인 및 분석 대기 {len(pending)}건'+(f' · 미표시 추천 {unseen}건' if unseen else ''),
    ]
    for category, label in [('securities', '증권'), ('card', '카드'), ('bank', '은행'), ('insurance', '보험')]:
        ids = {key for key, value in metadata.items() if value.get('finance_category') == category}
        if not ids:
            continue
        states = Counter(s['status'] for s in sources if s['company_id'] in ids)
        checked = sum(states.values())
        footer.append(f'금융 {label} {len(ids)}사: 정상 {states["ok"]} · 부분 {states["partial"]} · '
                      f'실패 {states["failed"]} · 미점검 {len(ids)-checked}')
    if settings.get('finance_permanent_only'):
        footer.append('금융은 정규직 확인 공고만 추천합니다. 전문계약직·계약직·전환형은 제외하고 고용형태 미확인은 보류합니다.')
    if summary.get('warnings'):
        footer.append('실행 참고: '+small_list(summary['warnings'], '', 160))
    if summary.get('uncertain_delivery') and not repeat:
        footer.append('이전 메일의 전송 결과가 불확실한 공고는 중복 방지를 위해 알림을 보류했습니다.')
    maintenance = summary.get('maintenance')
    if maintenance:
        footer.append(f'유지보수 · 복구 완료 {maintenance["recovered"]} · 복구 대기 {maintenance["pending"]}'
                      f' · 외부 차단·점검 {maintenance["external"]} · 실행 이상 {maintenance.get("execution", 0)}')
        if maintenance.get('reasons'):
            footer.append('주요 확인 사항: '+small_list(maintenance['reasons'], '', 200))
    if settings.get('_unlimited_budget'):
        footer.append(f'이번 테스트 비용 ${summary.get("run_cost_usd", 0):.4f} · 금액 상한 해제는 이번 테스트만 적용')
    elif summary.get('run_cost_usd') is not None:
        footer.append(f'실행 장부 비용 ${summary["run_cost_usd"]:.4f}')
    footer.append(f'이번 달 사용 ${summary.get("monthly_spend", 0):.2f} / ${settings["monthly_budget_usd"]:.2f}'
                  f' · 잔액 ${max(0, settings["monthly_budget_usd"]-summary.get("monthly_spend", 0)):.2f}'
                  ' (로컬 장부 기준)')
    footer.append('상세 근거·제외 내역·소스별 상태는 PC의 reports/latest.html에 저장했습니다.')
    h.append('<div style="font-size:12px;line-height:1.7;color:#53647b;padding:4px 0;">')
    h.extend('<p style="margin:6px 0;">'+escape(line)+'</p>' for line in footer)
    h.append('</div></div></body></html>')
    md.extend(['\n---', *footer])
    return '\n\n'.join(md), ''.join(h)
