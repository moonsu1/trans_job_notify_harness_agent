"""Stdlib-only bootstrap/watchdog: works even if the application cannot import."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))


def timestamp():
    return datetime.now(KST)


def directory():
    root = Path(os.environ.get('CAREER_SCOUT_DATA_DIR') or Path(os.environ.get('LOCALAPPDATA', Path.home()))/'CareerScout')
    (root/'operations').mkdir(parents=True, exist_ok=True)
    return root


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def heartbeat(phase, **extra):
    slot = os.environ.get('CAREER_SCOUT_OPERATION')
    if not slot:
        return
    path = directory()/'operations'/(slot+'.json')
    state = read(path, {})
    state.update(phase=phase, heartbeat=timestamp().isoformat(), **extra)
    atomic(path, state)


def alive(pid):
    if not pid:
        return False
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        code = wintypes.DWORD()
        valid = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(valid) and code.value == 259
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def due_slot(kind, config, at=None):
    at = at or timestamp()
    if kind == 'report':
        anchor = datetime.fromisoformat(config['anchor']).replace(hour=13, tzinfo=KST)
        interval = int(config.get('interval', 3))
        days = (at.date()-anchor.date()).days
        result = anchor+timedelta(days=(days//interval)*interval)
        if result > at:
            result -= timedelta(days=interval)
    else:
        result = at.replace(hour=14, minute=0, second=0, microsecond=0)
        if result > at:
            result -= timedelta(days=1)
    return kind+'-'+result.strftime('%Y%m%dT%H%M'), result


def owned_process(state, config):
    """Do not terminate a reused PID or another Python application."""
    if not alive(state.get('pid')) or sys.platform != 'win32':
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x1000, False, int(state['pid']))
    if not handle:
        return False
    try:
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return False
        if os.path.normcase(buffer.value) != os.path.normcase(config['python']):
            return False
        class FileTime(ctypes.Structure):
            _fields_ = [('low', wintypes.DWORD), ('high', wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE]+[ctypes.POINTER(FileTime)]*4
        created, exited, system, user = FileTime(), FileTime(), FileTime(), FileTime()
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            return False
        seconds = ((created.high << 32)+created.low)/10_000_000-11644473600
        return abs(seconds-datetime.fromisoformat(state['started']).timestamp()) < 30
    finally:
        kernel.CloseHandle(handle)


def delivery_status(mail_id):
    import sqlite3
    if not re.fullmatch(r'[a-f0-9]{16}', mail_id):
        return None
    path = directory()/'scout.sqlite3'
    try:
        with sqlite3.connect('file:'+path.as_posix()+'?mode=ro', uri=True, timeout=3) as connection:
            row = connection.execute('SELECT status FROM outbox WHERE id=?', (mail_id,)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def cleanup_containers(slot):
    """Clean only workers labelled for this exact interrupted operation."""
    known = [Path(os.environ.get('LOCALAPPDATA', ''))/'Programs/DockerDesktop/resources/bin/docker.exe',
             Path(os.environ.get('PROGRAMFILES', ''))/'Docker/Docker/resources/bin/docker.exe']
    docker = shutil.which('docker') or next((str(p) for p in known if p.is_file()), None)
    if not docker:
        return
    try:
        result = subprocess.run([docker, 'ps', '-aq', '--filter', 'label=career-scout.operation='+slot,
                                 '--filter', 'label=career-scout=parser'], capture_output=True, timeout=12)
        ids = [v for v in result.stdout.decode().splitlines() if re.fullmatch(r'[a-f0-9]{12,64}', v)]
        if ids:
            subprocess.run([docker, 'rm', '-f', *ids], capture_output=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        pass


def scheduling_paused(config):
    # Re-read the flag so an already-started watchdog respects a user's pause.
    latest = read(directory()/'schedule.json', config)
    return bool(latest.get('paused', config.get('paused', False)))


def operation(config, kind, *, retry=False, dry_run=False):
    if scheduling_paused(config):
        return {'state': 'paused'}
    root = directory()
    slot, due = due_slot(kind, config)
    if due < datetime.fromisoformat(config['installed_at']):
        return {'state': 'before_installation', 'slot': slot}
    path = root/'operations'/(slot+'.json')
    previous = read(path, {})
    mail_state = delivery_status(previous['mail_id']) if kind == 'report' and previous.get('mail_id') else previous.get('mail_status')
    if mail_state in ('sent', 'sending', 'uncertain') or (previous.get('mail_id') and mail_state is None):
        if not dry_run:
            previous.update(status='complete' if mail_state == 'sent' else 'uncertain', mail_status=mail_state or 'uncertain')
            atomic(path, previous)
        return {'state': 'already_handled', 'slot': slot}
    if previous.get('status') in ('complete', 'uncertain') or (alive(previous.get('pid')) and previous.get('status') == 'running'):
        return {'state': 'already_handled', 'slot': slot}
    if previous.get('retries', 0) >= 1 and retry:
        return {'state': 'retry_limit', 'slot': slot}
    if kind == 'maintenance':
        report_slot, report_due = due_slot('report', config)
        report = read(root/'operations'/(report_slot+'.json'), {})
        exhausted = report.get('status') in ('failed', 'interrupted') and report.get('retries', 0) >= 1
        if report_due >= datetime.fromisoformat(config['installed_at']) and report.get('status') not in ('complete', 'uncertain') and not exhausted:
            return {'state': 'report_has_priority', 'slot': slot}
    if dry_run:
        return {'state': 'would_launch', 'slot': slot}
    if kind == 'report':
        atomic(root/'operations'/'main.requested.json', {'at': timestamp().isoformat()})
        for _ in range(30):
            active = [read(p, {}) for p in (root/'operations').glob('maintenance-*.json')]
            if not any(s.get('status') == 'running' and alive(s.get('pid')) for s in active):
                break
            time.sleep(1)
    args = [config['python'], '-m', 'career_scout.cli']
    mail_only = kind == 'report' and previous.get('mail_id') and mail_state in ('pending', 'failed')
    args += ['send-report', previous['mail_id']] if mail_only else ['run', '--send'] if kind == 'report' else ['maintain']
    if kind == 'report' and previous.get('run_id') and not mail_only:
        args += ['--resume-run', previous['run_id']]
    env = dict(os.environ, PYTHONUTF8='1', CAREER_SCOUT_OPERATION=slot)
    state = dict(slot=slot, expected=due.isoformat(), started=timestamp().isoformat(),
                 heartbeat=timestamp().isoformat(), phase='bootstrap', status='running',
                 retries=previous.get('retries', 0)+int(retry), pid=0)
    if mail_only:
        state.update(mail_id=previous['mail_id'], run_id=previous.get('run_id'), mail_status=mail_state)
    atomic(path, state)
    with (root/'operations'/'bootstrap.log').open('ab') as output:
        try:
            process = subprocess.Popen(args, cwd=config['root'], env=env, stdout=output, stderr=output,
                                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        except OSError as exc:
            state.update(status='failed', phase='bootstrap_failed', exit_code=-1,
                         error=type(exc).__name__, finished=timestamp().isoformat())
            atomic(path, state)
            return state
        state['pid'] = process.pid
        atomic(path, state)
        started = time.monotonic()
        while process.poll() is None:
            request = read(root/'operations'/'main.requested.json', {})
            cancel = (kind == 'maintenance' and request.get('at') and
                      datetime.fromisoformat(request['at']) > datetime.fromisoformat(state['started']))
            if cancel or time.monotonic()-started > 1190:
                if sys.platform == 'win32':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                else:
                    process.kill()
                process.wait()
                cleanup_containers(slot)
                state.update(read(path, {}))
                state.update(status='interrupted', phase='report_priority' if cancel else 'timeout', finished=timestamp().isoformat())
                atomic(path, state)
                return state
            time.sleep(2)
    state.update(read(path, {}))
    state.update(status='uncertain' if state.get('mail_status') == 'uncertain' else
                 'complete' if process.returncode == 0 else 'failed', exit_code=process.returncode,
                 finished=timestamp().isoformat())
    atomic(path, state)
    return state


def inspect_operations(config, *, dry_run=False):
    if scheduling_paused(config):
        return [{'state': 'paused'}]
    results = []
    for kind in ('report', 'maintenance'):
        slot, due = due_slot(kind, config)
        if due < datetime.fromisoformat(config['installed_at']) or timestamp()-due < timedelta(minutes=25):
            continue
        state = read(directory()/'operations'/(slot+'.json'), {})
        if state.get('status') in ('complete', 'uncertain'):
            continue
        if alive(state.get('pid')):
            stale = timestamp()-datetime.fromisoformat(state.get('heartbeat', state['started'])) > timedelta(minutes=5)
            overdue = timestamp()-datetime.fromisoformat(state['started']) > timedelta(minutes=20)
            if not (stale or overdue) or not owned_process(state, config):
                continue
            if not dry_run:
                subprocess.run(['taskkill', '/PID', str(state['pid']), '/T', '/F'], capture_output=True)
                cleanup_containers(slot)
                state.update(status='interrupted', phase='watchdog_timeout')
                atomic(directory()/'operations'/(slot+'.json'), state)
            else:
                results.append({'state': 'would_stop_owned_stalled_process', 'slot': slot})
                continue
        result = operation(config, kind, retry=True, dry_run=dry_run)
        results.append(result)
        if not dry_run and result.get('pid'):
            break  # One bounded operation per tick; do not exceed the watchdog's own deadline.
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('tick', 'launch'))
    parser.add_argument('--kind', choices=('report', 'maintenance'), default='report')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    config = read(directory()/'schedule.json')
    if not config:
        print('Schedule configuration unavailable')
        return 1
    result = inspect_operations(config, dry_run=args.dry_run) if args.command == 'tick' else operation(config, args.kind, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))
    return int(isinstance(result, dict) and result.get('status') in ('failed', 'interrupted'))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
