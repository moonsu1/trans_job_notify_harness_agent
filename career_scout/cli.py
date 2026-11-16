from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import logging.handlers
import sys
from datetime import date

import keyring
from filelock import FileLock, Timeout

from .agent import SERVICE, credential
from .core import ROOT, data_dir, load_config, now
from .harness import Harness
from .mailer import retry_pending, send_mail
from .reporting import application_stats
from .storage import Store


def setup_logging(directory):
    logger = logging.getLogger('career_scout')
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(directory/'scout.log', maxBytes=2_000_000,
                                                   backupCount=4, encoding='utf-8')
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)


def diagnostic(settings, directory, store):
    import subprocess
    result = {
        'python': sys.version.split()[0], 'root': str(ROOT), 'data_dir': str(directory),
        'time_kst': now().isoformat(), 'model': settings['model'],
        'openai_registered': bool(credential('openai_api_key')),
        'gmail_registered': bool(credential('gmail_app_password')),
        'keyring_backend': type(keyring.get_keyring()).__name__,
        'recipient': settings['recipient'],
        'spent_today_usd': store.spend(now().isoformat()[:10]),
        'spent_month_usd': store.spend(now().isoformat()[:7]),
        'applications': application_stats(store),
    }
    if sys.platform == 'win32':
        result['windows_timezone'] = subprocess.run(['tzutil', '/g'], capture_output=True,
                                                     encoding='utf-8', errors='replace').stdout.strip()
        task = subprocess.run(['schtasks', '/Query', '/TN', 'CareerScout-Daily-1300', '/FO', 'LIST'],
                              capture_output=True)
        result['scheduled_task_registered'] = task.returncode == 0
    return result


def parser():
    p = argparse.ArgumentParser(description='AI 경력 채용 탐색 하네스')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='인증·환경·비용 진단 (비밀 값 출력 없음)')
    for command in ('health', 'maintain'):
        check = sub.add_parser(command, help='수집 상태 점검' if command == 'health' else '매일 점검·격리 자동 복구')
        check.add_argument('--no-api', action='store_true')
        check.add_argument('--company', action='append')
    sub.add_parser('incidents', help='장애 이력 조회')
    repair = sub.add_parser('repair', help='단일 장애의 격리 복구')
    repair.add_argument('incident_id')
    repair.add_argument('--dry-run', action='store_true')
    rollback = sub.add_parser('rollback', help='회사의 이전 수집기 복원')
    rollback.add_argument('company_id')
    creds = sub.add_parser('credentials', help='Windows 자격 증명에 비밀 입력')
    creds.add_argument('--only', choices=['openai', 'gmail'])
    run = sub.add_parser('run', help='공고 수집·분석·리포트')
    run.add_argument('--send', action='store_true', help='본인 Gmail로 발송')
    run.add_argument('--offline', action='store_true', help='저장된 공고만 사용')
    run.add_argument('--no-api', action='store_true', help='유료 검색/분석 비활성')
    run.add_argument('--no-browser', action='store_true')
    run.add_argument('--company', action='append', help='config/sources.yaml의 회사 ID; 여러 번 지정 가능')
    run.add_argument('--full-scan', action='store_true', help='모든 기업을 개발·AI 기획 두 경로로 탐색하고 원문 전체 분석')
    run.add_argument('--unlimited-budget', action='store_true', help='명시적으로 허용한 일회 테스트용 금액 상한 해제; 설정 파일은 변경하지 않음')
    run.add_argument('--resume-run', help='완료된 검색·수집과 분석 캐시를 재사용해 해당 실행 이어가기')
    sub.add_parser('retry-mail', help='미발송·실패 메일 재시도 (불확실 발송 제외)')
    mail = sub.add_parser('send-report', help='저장된 발송 대기열 전송')
    mail.add_argument('mail_id')
    resolve = sub.add_parser('resolve-mail', help='Gmail에서 확인한 불확실 발송 결과 수동 확정')
    resolve.add_argument('mail_id')
    resolve.add_argument('result', choices=['delivered', 'not-delivered'])
    sub.add_parser('outbox', help='발송 대기열 상태')
    export = sub.add_parser('eval-export', help='실제 JD 20건의 평가용 스냅샷 내보내기')
    export.add_argument('path', type=str)
    evaluate = sub.add_parser('evaluate', help='검토 기준 라벨과 실제 모델 평가 비교; API 예산 적용')
    evaluate.add_argument('path', type=str)
    feedback = sub.add_parser('feedback', help='공고 알림 상태')
    feedback.add_argument('job_id')
    feedback.add_argument('status', choices=['applied', 'ignored', 'reset'])
    application = sub.add_parser('application', help='지원·전형 결과 기록')
    application.add_argument('job_id')
    application.add_argument('--applied', type=date.fromisoformat, help='YYYY-MM-DD')
    application.add_argument('--screening', choices=['pending', 'pass', 'fail', 'withdrawn'])
    application.add_argument('--interview', choices=['pending', 'pass', 'fail', 'withdrawn'])
    return p


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = parser().parse_args()
    settings, profile, companies = load_config()
    directory = data_dir()
    setup_logging(directory)
    store = Store(directory/'scout.sqlite3')
    try:
        with FileLock(directory/'run.lock', timeout=0):
            if args.command == 'credentials':
                if sys.platform != 'win32' or 'WinVault' not in type(keyring.get_keyring()).__name__:
                    raise ValueError('Windows 자격 증명 저장소에서 실행해 주세요')
                if args.only in (None, 'openai'):
                    secret = getpass.getpass('새 OpenAI API 키 (화면에 표시되지 않음): ').strip()
                    if not secret.startswith('sk-'):
                        raise ValueError('OpenAI 키 형식 확인 필요')
                    keyring.set_password(SERVICE, 'openai_api_key', secret)
                if args.only in (None, 'gmail'):
                    secret = getpass.getpass('Gmail 앱 비밀번호 (일반 로그인 비밀번호 아님): ').replace(' ', '')
                    if len(secret) != 16:
                        raise ValueError('Gmail 앱 비밀번호 16자리 확인 필요')
                    keyring.set_password(SERVICE, 'gmail_app_password', secret)
                print('인증 정보를 Windows 자격 증명에 저장했습니다. 값은 출력하지 않습니다.')
            elif args.command == 'doctor':
                from .maintenance import Registry
                from .watchdog import read
                registry = Registry(store, settings)
                result = diagnostic(settings, directory, store)
                result.update(maintenance=registry.health.summary(),
                              docker_ready=asyncio.run(registry.runner.ready()),
                              schedule=read(directory/'schedule.json', {}),
                              repair_spent_today_usd=store.db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage WHERE kind LIKE 'repair%' AND at LIKE ?", (now().date().isoformat()+'%',)).fetchone()[0])
                result['repair_spent_month_usd'] = store.db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM usage WHERE kind LIKE 'repair%' AND at LIKE ?", (now().strftime('%Y-%m')+'%',)).fetchone()[0]
                result['repair_budget'] = dict(daily=settings.get('repair_daily_budget_usd', .10), monthly=settings.get('repair_monthly_budget_usd', 1.0))
                result['isolation_setup'] = read(directory/'setup'/'isolation-state.json', {'stage': 'not_started'})
                result['operations'] = [read(p, {}) for p in sorted((directory/'operations').glob('*.json'))[-8:]]
                last = store.db.execute("SELECT id,started,finished,status FROM runs WHERE status='complete' ORDER BY finished DESC LIMIT 1").fetchone()
                result['last_successful_run'] = dict(last) if last else None
                if sys.platform == 'win32':
                    import subprocess
                    result['tasks'] = {name: subprocess.run(['schtasks', '/Query', '/TN', name], capture_output=True).returncode == 0
                                       for name in ('CareerScout-Daily-1300', 'CareerScout-Maintenance-1400', 'CareerScout-Watchdog-30m', 'CareerScout-SandboxResume')}
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command in ('health', 'maintain'):
                from .maintenance import Maintenance
                from .watchdog import heartbeat
                if args.company and set(args.company)-{c['id'] for c in companies}:
                    raise ValueError('알 수 없는 회사 ID')
                heartbeat('maintenance')
                result = asyncio.run(Maintenance(settings, companies, store, no_api=args.no_api,
                                                 only=args.company).run(repair=args.command == 'maintain'))
                heartbeat('finished', run_id=result['run_id'])
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == 'incidents':
                from .health import HealthStore
                print(json.dumps(HealthStore(store).incidents(), ensure_ascii=False, indent=2))
            elif args.command == 'rollback':
                from .maintenance import Registry
                if args.company_id not in {c['id'] for c in companies}:
                    raise ValueError('알 수 없는 회사 ID')
                print(json.dumps({'restored': Registry(store, settings).rollback(args.company_id)}, ensure_ascii=False))
            elif args.command == 'repair':
                from .maintenance import Maintenance
                from .collectors import PublicTools
                maintenance = Maintenance(settings, companies, store)
                incident = next((i for i in maintenance.health.incidents() if i['id'] == args.incident_id), None)
                if not incident:
                    raise ValueError('장애 ID 없음')
                async def repair_incident():
                    maintenance.tools = PublicTools(settings)
                    try:
                        return await maintenance.repair_one(incident, dry_run=args.dry_run)
                    finally:
                        await maintenance.tools.close()
                print(json.dumps(asyncio.run(repair_incident()), ensure_ascii=False, indent=2))
            elif args.command == 'run':
                settings['_full_scan'] = args.full_scan
                settings['_unlimited_budget'] = args.unlimited_budget
                settings['_resume_run'] = args.resume_run
                if args.unlimited_budget:
                    logging.getLogger('career_scout').warning('이번 실행에만 API 금액 상한 해제; 모든 사용량 기록')
                if args.company and any(c not in {x['id'] for x in companies} for c in args.company):
                    raise ValueError('알 수 없는 회사 ID')
                result = asyncio.run(Harness(settings, profile, companies, store, offline=args.offline,
                                             no_api=args.no_api, send=args.send, browser=not args.no_browser,
                                             only=args.company).run())
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if result.get('mail_status') in ('failed', 'uncertain'):
                    return 1
                if result.get('mail_status') == 'pending':
                    return 3
            elif args.command == 'retry-mail':
                print(json.dumps(retry_pending(store, settings), ensure_ascii=False, indent=2))
            elif args.command == 'send-report':
                from .watchdog import heartbeat
                status = send_mail(store, settings, args.mail_id)
                heartbeat('finished', mail_id=args.mail_id, mail_status=status)
                print(status)
                if status in ('failed', 'uncertain', 'sending', 'pending'):
                    return 1
            elif args.command == 'resolve-mail':
                mail = store.db.execute('SELECT * FROM outbox WHERE id=?', (args.mail_id,)).fetchone()
                if not mail or mail['status'] != 'uncertain':
                    raise ValueError('uncertain 상태의 발송 ID만 확정할 수 있습니다')
                if args.result == 'delivered':
                    store.mark_sent(mail)
                else:
                    store.mail_status(args.mail_id, 'failed', '사용자가 미전송 확인')
                print('확정했습니다. 미전송이면 retry-mail로 재시도할 수 있습니다.')
            elif args.command == 'outbox':
                print(json.dumps([dict(r) for r in store.db.execute(
                    'SELECT id,created,status,subject,error FROM outbox ORDER BY created DESC LIMIT 30')],
                    ensure_ascii=False, indent=2))
            elif args.command in ('eval-export', 'evaluate'):
                from pathlib import Path
                from .evaluation import evaluate, export_cases
                if args.command == 'eval-export':
                    print(f'실제 JD {export_cases(store, Path(args.path))}건 저장. 기준 라벨은 자동 생성하지 않습니다.')
                else:
                    print(json.dumps(evaluate(store, settings, profile, Path(args.path)), ensure_ascii=False, indent=2))
            elif args.command in ('feedback', 'application'):
                if not store.db.execute('SELECT 1 FROM jobs WHERE id=?', (args.job_id,)).fetchone():
                    raise ValueError('공고 ID 없음: 전체 리포트에서 ID 확인')
                if args.command == 'feedback':
                    store.db.execute('UPDATE jobs SET feedback=? WHERE id=?',
                                     ('' if args.status == 'reset' else args.status, args.job_id))
                else:
                    old = store.db.execute('SELECT * FROM applications WHERE job_id=?', (args.job_id,)).fetchone()
                    applied = str(args.applied) if args.applied else old['applied'] if old else now().date().isoformat()
                    screening = args.screening or (old['screening'] if old else 'pending')
                    interview = args.interview or (old['interview'] if old else 'pending')
                    store.db.execute('INSERT OR REPLACE INTO applications VALUES(?,?,?,?,?)',
                                     (args.job_id, applied, screening, interview, now().isoformat()))
                    store.db.execute("UPDATE jobs SET feedback='applied' WHERE id=?", (args.job_id,))
                store.db.commit()
                print('저장했습니다. '+application_stats(store))
    except Timeout:
        print('이미 실행 중입니다. 중복 실행을 건너뜁니다.')
        return 2
    except Exception as exc:
        # Secrets and raw API/server responses are intentionally not logged.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        logging.getLogger('career_scout').error('작업 실패: %s', message[:300])
        return 1
    finally:
        store.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
