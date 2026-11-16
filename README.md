# AI 채용 탐색 하네스

공식 채용 사이트를 수집하고 AI 직무 적합성을 평가해 Gmail과 로컬 HTML·Markdown으로 전달하는 Windows용 Python Agent입니다. LangGraph가 수집 → 검색 보완 → 직무 판정 → 이력 적합성 평가 → 리포트 흐름을 관리합니다. 기본 예약은 **한국 시간 기준 3일 간격 13:00**이며 ChatGPT를 열어둘 필요가 없습니다.

## 설치

Windows, Python 3.12, Gmail 계정과 OpenAI API 키를 사용합니다. 개인 경력 프로필·수신 주소·인증 정보·실행 DB·실제 리포트는 저장소에 포함되지 않습니다.

```powershell
git clone https://github.com/moonsu1/trans_job_notify_harness_agent.git
cd trans_job_notify_harness_agent
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

설치 스크립트는 가상환경·의존성·Chromium을 설치하고, 없는 설정 파일만 예제에서 복사합니다. 기존 설정은 보존합니다. Python 설치 명령을 사용할 수 없다면 Python 3.12를 먼저 설치하고 다시 실행합니다.

1. `config/settings.yaml`의 `recipient`를 본인 Gmail 주소로 바꿉니다. 같은 주소를 SMTP 발신에도 사용합니다.
2. `config/profile.yaml`을 **실제 경력으로 교체**합니다. 제공된 내용은 가상의 예시입니다. 실무 시작일과 학위 기간을 구분하고 확인한 성과만 기입합니다.
3. `인증등록.cmd`를 열어 OpenAI API 키와 Gmail 앱 비밀번호를 각각 입력합니다. Windows 자격 증명 저장소에 저장하며 **Google 로그인 비밀번호는 사용하지 않습니다.** [Gmail 앱 비밀번호 안내](https://support.google.com/accounts/answer/185833?hl=ko)
4. 발송 없는 실행으로 결과를 확인한 뒤 `지금실행.cmd`로 수집·분석·메일을 실행합니다.
5. 자동 실행을 원하면 `배치켜기.cmd`를 실행합니다. 설치만으로 예약을 등록하지 않습니다.

설정을 수동으로 준비하려면 다음 파일을 복사합니다. 이미 설정이 있으면 덮어쓰지 마세요.

```powershell
Copy-Item config/settings.example.yaml config/settings.yaml
Copy-Item config/profile.example.yaml config/profile.yaml
```

## 탐색·평가 기준

| 순위 | 기업 | 지역 |
|---|---|---|
| 1 | 삼성전자 DS, SK하이닉스 | 국내 전 지역 |
| 2 | 현대자동차, 기아, 삼성리서치 | 서울·경기 |
| 3 | SKT, 현대모비스, 네이버, 카카오, 크래프톤, 국내 외국계 기술기업 및 금융사 | 서울·경기 |

기본 구성은 기술·제조 기업과 증권·카드·은행·보험 42개사를 합쳐 56곳입니다. 등록된 회사와 그날 정상 수집한 회사는 구분합니다. 소스 목록은 `config/sources.yaml`, `config/financial_companies.yaml`에서 확인합니다.

- AI Agent·RAG·Text2SQL·Ontology 제품 개발, 해당 제품의 기획·PM·PL에 집중합니다. MLOps/LLMOps, 데이터 분석·엔지니어링, 일반 ML 모델, 콘텐츠 제작은 제외합니다.
- JD 주업무로 직무 범위를 먼저 판정하고 대상 직무만 경력 적합성을 평가합니다. SQL·Python 키워드가 겹친다는 이유만으로 추천하지 않습니다.
- SI·컨설팅과 LG·한화·포스코·CJ·HD현대 계열, 토스·당근·쿠팡은 기본 제외입니다. 금융은 정규직이 명시된 공고만 추천합니다.
- 총보상 1억 3천만 원은 협의 가능한 목표입니다. 연봉 미공개 자체는 제외 조건이 아닙니다.
- 점수에는 JD 문단과 경력 근거를 연결합니다. **적합성 점수는 합격 확률이 아닙니다.** 수치 합격 확률을 생성하지 않으며 필수 조건·원문이 불확실하면 판단을 보류합니다.

기업·지역·제외 기준은 설정과 정책 코드로 관리합니다. 공고·첨부 문서의 지시가 도구 권한이나 메일 수신처를 바꿀 수 없습니다.

## 리포트와 실행

메일은 모바일용 한 열 카드로 업무 요약·적합성·강점·확인할 점·링크를 표시합니다. 매번 현재 후보 전체를 **새로 추가된 공고 / 이전에 알려드린 공고**로 나눠 보냅니다. 마감·지원 완료·관심 없음은 제외하고 원문·평가 대기는 별도 표시합니다. 상세 결과는 `reports/latest.html`, 메일 미리보기는 `reports/latest-email.html`입니다.

```powershell
# 설정·인증 여부·예약·예산 진단 (비밀 값 출력 없음)
.\.venv\Scripts\python.exe -m career_scout.cli doctor

# 발송 없이 실사이트 수집·API 분석 (API 비용 발생 가능)
.\.venv\Scripts\python.exe -m career_scout.cli run

# API 없이 수집만 / 저장 자료로 리포트만
.\.venv\Scripts\python.exe -m career_scout.cli run --no-api
.\.venv\Scripts\python.exe -m career_scout.cli run --offline --no-api

# 수집·분석 후 본인 Gmail 발송
.\.venv\Scripts\python.exe -m career_scout.cli run --send

# 특정 회사 / 발송 대기열 / 지원 결과 기록
.\.venv\Scripts\python.exe -m career_scout.cli run --no-api --company skhynix
.\.venv\Scripts\python.exe -m career_scout.cli outbox
.\.venv\Scripts\python.exe -m career_scout.cli feedback JOB_ID ignored
.\.venv\Scripts\python.exe -m career_scout.cli application JOB_ID --screening pass
```

`리포트미리보기.cmd`는 API·메일 없이 저장된 후보로 미리보기를 만듭니다. SMTP 결과가 불확실한 발송은 자동 재전송하지 않습니다. 같은 발송 대기열의 재시도와 다음 예약일의 새 리포트는 구분합니다.

## 예약·유지보수

`배치켜기.cmd`는 채용 메일(3일 간격 13시), 유지보수(매일 14시), 독립 실행 감시(30분 간격)를 등록·재개합니다. `배치상태.cmd`로 다음 실행을 확인하고 `배치끄기.cmd`로 관련 예약을 중지합니다. 다시 켜면 중지 기간의 예약은 건너뛰고 기존 3일 주기의 다음 13시부터 실행합니다. 수동 실행은 중지 상태에서도 가능합니다.

PC 전원·인터넷·Windows 로그인 상태를 유지해야 합니다. 화면 잠금은 가능합니다. 이미 진행 중인 작업은 중지 명령 후에도 완료될 수 있습니다. 실행 DB·로그·예산 장부는 `%LOCALAPPDATA%\CareerScout`, 리포트는 프로젝트의 `reports/`에 저장합니다.

수집 근거·장애·버전·heartbeat를 기록하며 무료 점검과 기존 대체 경로를 먼저 사용합니다. 생성 수집기의 자동 적용에는 **WSL2·Docker 설치와 실제 격리 검증**이 필요합니다. 준비 전에는 활성화하지 않습니다. [유지보수 운영](docs/maintenance.md)

## 모델·비용·수집 제한

기본 모델은 `gpt-5.4-mini`, 예산은 월 $10·하루 $0.90입니다. 그 안에서 복구에 월 $1·하루 $0.10의 내부 상한을 둡니다. 고가 모델 자동 전환은 없습니다. 토큰·검색 비용을 호출 전에 예약하고 응답 후 정산하며 단가는 `config/settings.yaml`에서 관리합니다. 외부 앱 사용량은 이 장부에 포함되지 않습니다.

`max_search_calls: 12`는 한국 날짜 기준 **유료 추가 웹검색 요청** 한도입니다. 공식 사이트 직접 수집과는 별개이며 같은 날 수동·예약 실행을 합산합니다. 변경 없는 JD 평가는 캐시를 재사용하고 예산 소진 시 미분석 상태를 표시합니다.

공식 목록·실제 법인·페이지·JD를 대조하고 삼성의 여러 직무가 묶인 공고는 개별 역할로 분리합니다. 공개 재게시 자료는 실제 채용 법인을 검증한 뒤 사용합니다. 이미지 JD는 로컬 OCR을 사용할 수 있지만 원본 대조 전에는 점수를 생성하지 않습니다.

HTTP 오류·로그인·CAPTCHA·실제 차단은 실패 또는 부분 수집으로 남깁니다. **실패는 공고 없음이나 마감을 뜻하지 않습니다.** 모든 사이트 변경과 외부 접근 제한을 자동 해결한다고 가정하지 않습니다.

## 개발·검증

설정 예제를 복사한 뒤 실행합니다. 단위·회귀 테스트는 임시 실행 DB와 대역을 사용하며 실제 메일·유료 API를 호출하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

`career_scout/`는 수집·평가·운영 코드, `scripts/`는 설치·예약·진단 도구, `sandbox/`는 격리 실행기, `tests/fixtures/`는 공개 채용 응답의 고정 회귀 사례입니다. 실사이트 점검·모델 품질·메일 수신·Docker 격리 검증은 단위 테스트와 별도로 확인합니다.

로컬 설정·이력·인증 정보·수집 캡처·평가 산출물·리포트는 Git에서 제외합니다. 키를 설정 파일이나 코드에 넣지 마세요.
