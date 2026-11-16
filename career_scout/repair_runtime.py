"""Generated parsers execute only in Docker; the host owns I/O and validation."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import shutil
import os
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .core import ROOT, Job, canonical_url, digest, plain, prefilter
from .collectors import Collection, allowed_url, employer_matches, verified_jd_body, soup_text
from .health import CatalogProof


class RepairRejected(ValueError):
    pass


def validate_code(code):
    if len(code.encode()) > 48000:
        raise RepairRejected('수정 코드 크기 제한')
    tree = ast.parse(code)
    allowed = {'json', 're', 'html', 'datetime', 'urllib', 'bs4', 'collections', 'math'}
    banned = {'exec', 'eval', 'compile', 'open', '__import__', 'getattr', 'setattr', 'globals', 'locals', 'breakpoint'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(n.name.split('.')[0] not in allowed for n in node.names):
            raise RepairRejected('허용되지 않은 import')
        if isinstance(node, ast.ImportFrom) and (node.level or (node.module or '').split('.')[0] not in allowed):
            raise RepairRejected('허용되지 않은 import')
        if isinstance(node, ast.Name) and node.id in banned:
            raise RepairRejected('허용되지 않은 동작')
        if isinstance(node, ast.Attribute) and node.attr.startswith('_'):
            raise RepairRejected('비공개 속성 접근')
    if not any(isinstance(n, ast.FunctionDef) and n.name == 'collect' for n in tree.body):
        raise RepairRejected('collect(api, company) 함수 필요')
    return hashlib.sha256(code.encode()).hexdigest()


def resolve(ref, ledger):
    if not isinstance(ref, dict) or set(ref)-{'evidence_id', 'pointer', 'selector', 'attribute', 'many', 'index'}:
        raise RepairRejected('근거 참조 형식 오류')
    record = ledger.get(ref.get('evidence_id'))
    if not record or record['status'] != 200:
        raise RepairRejected('성공한 실제 응답 근거 필요')
    if 'pointer' in ref:
        value = json.loads(record['text'])
        parts = ref['pointer'].strip('/').split('/') if ref['pointer'] else []

        def walk(current, keys):
            if not keys:
                return current
            key = keys[0].replace('~1', '/').replace('~0', '~')
            if key == '*':
                if not isinstance(current, list):
                    raise RepairRejected('목록 경로 오류')
                return [walk(row, keys[1:]) for row in current]
            return walk(current[int(key)] if isinstance(current, list) else current[key], keys[1:])
        try:
            return walk(value, parts)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RepairRejected('실제 응답에 없는 필드') from exc
    if not isinstance(ref.get('selector'), str) or len(ref['selector']) > 300:
        raise RepairRejected('명시적 CSS 선택자 필요')
    elements = BeautifulSoup(record['text'], 'html.parser').select(ref['selector'])
    values = [e.get(ref['attribute']) if ref.get('attribute') else e.get_text(' ', strip=True) for e in elements]
    if ref.get('many'):
        return values
    try:
        return values[int(ref.get('index', 0))]
    except (IndexError, ValueError):
        raise RepairRejected('실제 화면에 없는 항목') from None


def number(value):
    if isinstance(value, bool) or isinstance(value, int) and value < 0:
        raise RepairRejected('음수·불리언 총건수 금지')
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    matches = re.findall(r'\d+', str(value).replace(',', ''))
    if len(matches) != 1:
        raise RepairRejected('독립적으로 확인할 수 없는 총건수')
    return int(matches[0])


def contract_hash(company):
    protected = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for folder in ('career_scout', 'tests', 'config', 'sandbox')
                 for p in (ROOT/folder).rglob('*') if p.is_file() and p.suffix in ('.py', '.yaml')}
    return digest([{k: v for k, v in company.items() if not k.startswith('_verified')},
                   protected,
                   hashlib.sha256((ROOT/'sandbox'/'worker.py').read_bytes()).hexdigest(),
                   hashlib.sha256((ROOT/'sandbox'/'Dockerfile').read_bytes()).hexdigest()])


def validate_output(output, company, ledger, settings):
    if not isinstance(output, dict) or set(output) != {'pages', 'jobs'} or not output['pages']:
        raise RepairRejected('pages와 jobs 결과 필요')
    seen, totals, targets = set(), [], set()
    shared = company.get('adapter') in ('samsungcareers', 'skcareers', 'korea_investment') or company.get('portal_employer_prefix')
    names = {plain(n).casefold() for n in [company['name'], company.get('portal_company_name', '')]
             + company.get('portal_aliases', []) if n}
    for index, page in enumerate(output['pages'], 1):
        if page.get('page') != index or not isinstance(page.get('ids'), dict):
            raise RepairRejected('페이지 누락 또는 ID 근거 없음')
        ids = resolve(page['ids'], ledger)
        if not isinstance(ids, list):
            raise RepairRejected('실제 ID 목록 필요')
        if any(v is None or isinstance(v, bool) or not str(v).strip() for v in ids):
            raise RepairRejected('비어 있거나 잘못된 공고 ID')
        ids = [str(v) for v in ids]
        if len(ids) != len(set(ids)) or seen.intersection(ids):
            raise RepairRejected('공고 ID 중복')
        seen.update(ids)
        totals.append(number(resolve(page['total'], ledger)))
        if not ids and totals[-1] == 0:
            evidence = ledger[page['ids']['evidence_id']]
            if re.search(r'access denied|captcha|under maintenance|시스템\s*(?:점검|개선)|로그인\s*(?:필요|후)', evidence['text'], re.I):
                raise RepairRejected('접근 제한·점검 응답을 빈 목록으로 처리할 수 없음')
            if 'pointer' in page['ids']:
                pointer = page['ids']['pointer'].casefold()
                if not re.search(r'job|posting|recruit|anno|notice|position|items|list|rows|content', pointer):
                    raise RepairRejected('공식 공고 배열의 빈 목록 근거 필요')
            elif not re.search(r'(?:공고|채용|모집|jobs?|positions?).{0,30}(?:0\s*건|없|no |0\b)|(?:no|0)\s+(?:open )?(?:jobs?|positions?)',
                               soup_text(evidence['text']), re.I):
                raise RepairRejected('공식 빈 목록 문구 확인 필요')
        if shared:
            employers = resolve(page.get('employers'), ledger)
            if not isinstance(employers, list) or len(employers) != len(ids):
                raise RepairRejected('공유 포털 실제 법인별 근거 없음')
            targets.update(i for i, name in zip(ids, employers) if plain(str(name)).casefold() in names
                           or str(name).startswith(company.get('portal_employer_prefix') or '\x00'))
        else:
            targets.update(ids)
    if len(set(totals)) != 1 or len(seen) != totals[0]:
        raise RepairRejected('총건수 또는 페이지 범위 불일치')
    if len(seen) > 2000 or len(output['jobs']) > 2000:
        raise RepairRejected('공고 수 상한')
    jobs, output_ids, output_urls = [], set(), set()
    for fields in output['jobs']:
        if set(fields)-{'id', 'title', 'url', 'body', 'organization', 'location', 'employment_type', 'experience', 'deadline'}:
            raise RepairRejected('수집기 권한 밖 출력 필드')
        ident = str(resolve(fields['id'], ledger))
        if ident not in targets or ident in output_ids:
            raise RepairRejected('다른 법인 또는 중복 공고')
        output_ids.add(ident)
        title = plain(str(resolve(fields['title'], ledger)))
        record = ledger[fields['url']['evidence_id']]
        url = urljoin(record['url'], str(resolve(fields['url'], ledger)))
        for field, ref in fields.items():
            if field == 'id':
                continue
            resolve(ref, ledger)
            field_record = ledger[ref['evidence_id']]
            id_parent = fields['id'].get('pointer', '\x00').rsplit('/', 1)[0]
            same_row = (ref['evidence_id'] == fields['id']['evidence_id'] and 'pointer' in ref
                        and (ref['pointer'].rsplit('/', 1)[0] == id_parent or ref['pointer'].startswith(id_parent+'/')))
            detail = (canonical_url(field_record['url']) == canonical_url(url)
                      or field_record['url'].rstrip('/').endswith('/'+ident))
            if not same_row and not detail:
                raise RepairRejected('다른 공고·공통 메뉴에서 가져온 필드 근거')
        # A broker-approved new URL is still bound to its verified employer path.
        if not allowed_url(url, company) and not any(url == r['url'] and r.get('official_link') for r in ledger.values()):
            raise RepairRejected('허용 기업·게시판 밖 URL')
        if canonical_url(url) in output_urls:
            raise RepairRejected('서로 다른 공고의 상세 URL 중복')
        output_urls.add(canonical_url(url))
        if not title or '로그인' == title:
            raise RepairRejected('JD 제목 확인 실패')
        raw_body = resolve(fields['body'], ledger) if fields.get('body') else ''
        if fields.get('body'):
            body_record = ledger[fields['body']['evidence_id']]
            title_record = ledger[fields['title']['evidence_id']]
            same_row = (fields['body']['evidence_id'] == fields['id']['evidence_id']
                        and fields['body'].get('pointer', '\x00').rsplit('/', 1)[0]
                        == fields['id'].get('pointer', '\x01').rsplit('/', 1)[0])
            detail_document = (canonical_url(body_record['url']) == canonical_url(url)
                               or (body_record['url'].rstrip('/').endswith('/'+ident)
                                   and fields['body']['evidence_id'] == fields['title']['evidence_id']))
            if not (same_row or detail_document) or title not in plain(str(title_record['text'])):
                raise RepairRejected('해당 공고와 JD 근거의 연결 확인 실패')
        body = soup_text(str(raw_body))
        job = Job(company_id=company['id'], company=company['name'], tier=company['tier'],
                  external_id=ident, title=title, url=url, source_url=company['url'], body=body,
                  employer_verified=True, verified=verified_jd_body(body),
                  source_note='격리 수집기 · 호스트의 원문 근거 검증 통과')
        for field in ('organization', 'location', 'employment_type', 'experience'):
            if fields.get(field):
                setattr(job, field, plain(str(resolve(fields[field], ledger))))
        if fields.get('deadline'):
            from .portals import portal_deadline
            raw = str(resolve(fields['deadline'], ledger))
            job.deadline = portal_deadline(raw) or (raw if re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T[^ ]+)?', raw) else '')
        if not prefilter(job, settings) and not job.verified:
            raise RepairRejected('현재 공고의 JD 본문 미확인')
        jobs.append(job)
    if output_ids != targets:
        raise RepairRejected('대상 기업 공고 누락')
    return Collection(company['id'], jobs, 'ok', '격리 수집기: 전체 목록·실제 법인·필드 원문 대조 통과',
                      proof=CatalogProof(expected=totals[0], observed=len(seen), company_count=len(jobs),
                                         pages_expected=len(totals), pages_observed=len(totals), complete=True,
                                         empty_confirmed=totals[0] == 0, basis='host_verified_references'))


class PublicBroker:
    def __init__(self, tools, company, max_calls=80):
        self.tools, self.company, self.max_calls = tools, company, max_calls
        self.calls, self.ledger = 0, {}
        self.verified_urls = set(company.get('_verified_urls', ()))

    def linked(self, url):
        # Only career navigation from a trusted entry page can establish a new host.
        for r in self.ledger.values():
            entry = canonical_url(r['url']) == canonical_url(self.company['url'])
            if not entry and not r.get('official_link'):
                continue
            for a in BeautifulSoup(r['text'], 'html.parser').select('a[href]'):
                target = urljoin(r['url'], a['href'])
                same_tenant = urlsplit(target).hostname == urlsplit(r['url']).hostname
                if (not entry and urlsplit(target).hostname in ('job-boards.greenhouse.io', 'boards.greenhouse.io')):
                    same_tenant = same_tenant and urlsplit(target).path.strip('/').split('/')[0] == urlsplit(r['url']).path.strip('/').split('/')[0]
                if target == url and ((entry and re.search(r'채용|careers?|recruit|jobs', a.get_text()+' '+a['href'], re.I))
                                      or (not entry and same_tenant)):
                    return True
        return False

    async def call(self, name, args):
        from .browser_read import public_page, PUBLIC_POSTS
        from .collectors import public_url
        self.calls += 1
        if self.calls > self.max_calls or name not in ('fetch', 'render', 'jobflex') or set(args)-{'url', 'method', 'json', 'frame', 'path', 'payload'}:
            raise RepairRejected('공개 수집 Tool 범위·횟수 제한')
        if name == 'jobflex':
            from .browser_read import jobflex_request
            if not (urlsplit(self.company['url']).hostname or '').endswith('.recruiter.co.kr'):
                raise RepairRejected('현재 기업의 플랫폼과 맞지 않는 Tool')
            async with public_page(self.tools, self.company['url']) as page:
                data = await jobflex_request(page, args['path'], args.get('payload'))
            record = self.tools.vault.capture('https://api-recruiter.recruiter.co.kr'+args['path'],
                                              json.dumps(data, ensure_ascii=False), company_id=self.company['id'],
                                              kind='jobflex', request={'payload': args.get('payload')})
            self.ledger[record['id']] = record
            return record
        url = args.get('url', '')
        await asyncio.to_thread(public_url, url)
        known, linked = allowed_url(url, self.company) or canonical_url(url) in self.verified_urls, self.linked(url)
        if not known and not linked:
            raise RepairRejected('공식 연결 근거 없는 호스트·게시판')
        method = args.get('method', 'GET')
        if method not in ('GET', 'POST') or (method == 'POST' and (not known or urlsplit(url).path not in PUBLIC_POSTS)):
            raise RepairRejected('읽기 전용 경로 밖 요청')
        if method == 'POST' and any(re.search(r'email|password|resume|applicant|token|secret', key, re.I)
                                    for key in (args.get('json') or {})):
            raise RepairRejected('지원서·인증 자료 요청 금지')
        if name == 'fetch':
            kwargs = {'json': args['json']} if 'json' in args else {}
            response = await self.tools.fetch(url, method, **kwargs)
            text, final = response.text, str(response.url)
            if not allowed_url(final, self.company) and final != url:
                raise RepairRejected('검증되지 않은 이동')
        else:
            async with public_page(self.tools, url) as page:
                await page.wait_for_timeout(700)
                if args.get('frame'):
                    if len(args['frame']) > 300:
                        raise RepairRejected('프레임 선택자 제한')
                    text = await page.frame_locator(args['frame']).locator('body').evaluate('(e)=>e.outerHTML')
                else:
                    text = await page.content()
                final = page.url
        if not known and (final != url or not employer_matches(soup_text(text), self.company)):
            raise RepairRejected('새 공식 연결 페이지의 실제 기업·이동 범위 미확인')
        record = self.tools.vault.capture(final, text, company_id=self.company['id'], kind=name,
                                          request={'method': method, 'json': args.get('json')})
        record['official_link'] = linked or canonical_url(url) in self.verified_urls
        if record['official_link']:
            self.verified_urls.add(canonical_url(final))
        self.ledger[record['id']] = record
        return record


class ReplayBroker:
    def __init__(self, records):
        self.records, self.ledger = records, {}

    async def call(self, name, args):
        url = 'https://api-recruiter.recruiter.co.kr'+args['path'] if name == 'jobflex' else args['url']
        record = next((r for r in self.records if canonical_url(r['url']) == canonical_url(url)
                       and r.get('kind', 'fetch') in (name, 'http')
                       and (name != 'jobflex' or r.get('request', {}).get('payload') == args.get('payload'))
                       and (name != 'fetch' or not r.get('request') or
                            (r['request'].get('method', 'GET') == args.get('method', 'GET')
                             and r['request'].get('json') == args.get('json')))), None)
        if not record:
            raise RepairRejected('보호된 과거 응답에 없는 요청')
        self.ledger[record['id']] = record
        return record


class DockerRunner:
    def __init__(self, root, image='career-scout-parser:1'):
        self.root, self.image = Path(root), image
        self.image_id = ''

    async def ready(self, *, require_attestation=True):
        self.image_id = ''
        if not self.executable():
            return False
        process = await asyncio.create_subprocess_exec(self.executable(), 'image', 'inspect', self.image, '--format', '{{.Id}}',
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 12)
            self.image_id = output.decode('utf-8', errors='replace').strip()
            installed = process.returncode == 0 and self.image_id.startswith('sha256:')
            if not installed or not require_attestation:
                return installed
            try:
                evidence = json.loads((self.root/'sandbox-validation.json').read_text(encoding='utf-8'))
                return bool(evidence.get('passed') and evidence.get('image_id') == self.image_id
                            and evidence.get('fingerprint') == sandbox_fingerprint())
            except (OSError, ValueError):
                return False
        except TimeoutError:
            process.kill()
            await process.wait()
            return False

    def executable(self):
        known = [Path(os.environ.get('LOCALAPPDATA', ''))/'Programs'/'DockerDesktop'/'resources'/'bin'/'docker.exe',
                 Path(os.environ.get('PROGRAMFILES', ''))/'Docker'/'Docker'/'resources'/'bin'/'docker.exe']
        return shutil.which('docker') or next((str(p) for p in known if p.is_file()), None)

    async def run(self, code, company, broker, timeout=180):
        code_hash = validate_code(code)
        artifact = self.root/'runtime'/code_hash
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact/'plugin.py').write_text(code, encoding='utf-8')
        runtime = ROOT/'sandbox'/'worker.py'
        if not self.executable() or not self.image_id:
            raise RepairRejected('Docker 실행 환경 미준비')
        container_name = 'career-scout-parser-'+uuid.uuid4().hex[:12]
        argv = [self.executable(), 'run', '--rm', '-i', '--name', container_name,
                '--label', 'career-scout=parser', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--user', '65534:65534', '--pids-limit', '64',
                '--memory', '512m', '--cpus', '1', '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m',
                '--mount', f'type=bind,src={artifact},dst=/plugin,readonly',
                '--mount', f'type=bind,src={runtime},dst=/worker.py,readonly',
                self.image_id, 'python', '-I', '-B', '-u', '/worker.py']
        if os.environ.get('CAREER_SCOUT_OPERATION'):
            argv[2:2] = ['--label', 'career-scout.operation='+os.environ['CAREER_SCOUT_OPERATION']]
        process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                     limit=3_000_000)
        safe_company = {k: company[k] for k in ('id', 'name', 'tier', 'url', 'board', 'portal_company_name', 'domains') if k in company}

        async def communicate():
            process.stdin.write((json.dumps({'company': safe_company})+'\n').encode())
            await process.stdin.drain()
            for _ in range(82):
                line = await process.stdout.readline()
                if not line:
                    raise RepairRejected('격리 수집기 비정상 종료')
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    raise RepairRejected('격리 수집기 JSON 통신 형식 오류') from None
                if not isinstance(message, dict):
                    raise RepairRejected('격리 통신 객체 형식 오류')
                if message.get('op') == 'result':
                    try:
                        result = validate_output(message.get('result'), company, broker.ledger, broker.tools.settings
                                                 if isinstance(broker, PublicBroker) else self.settings)
                    except RepairRejected:
                        raise
                    except Exception:
                        raise RepairRejected('격리 수집기 출력의 필수 필드·원문 파싱 오류') from None
                    process.stdin.close()
                    await process.wait()
                    if process.returncode:
                        raise RepairRejected('격리 프로세스 종료 오류')
                    return result
                if message.get('op') != 'tool':
                    raise RepairRejected('격리 통신 규약 오류')
                response = await broker.call(message.get('name'), message.get('args', {}))
                process.stdin.write((json.dumps(response, ensure_ascii=False)+'\n').encode())
                await process.stdin.drain()
            raise RepairRejected('격리 Tool 횟수 제한')
        try:
            return await asyncio.wait_for(communicate(), timeout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
                cleanup = await asyncio.create_subprocess_exec(self.executable(), 'rm', '-f', container_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    await asyncio.wait_for(cleanup.wait(), 10)
                except TimeoutError:
                    cleanup.kill()
                    await cleanup.wait()


def sandbox_fingerprint():
    return digest([hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in (ROOT/'sandbox/worker.py', ROOT/'sandbox/Dockerfile', Path(__file__))])
