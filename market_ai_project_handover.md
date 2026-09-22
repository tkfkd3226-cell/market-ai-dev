# Market AI 프로젝트 인수인계

> 기준: 현재 `market-ai` 실행 폴더 + `market-ai-dev` 개발 폴더  
> 목적: 변경 이력이 아니라 **현재 유효한 장기 유지보수 contract**를 보존한다.

> **문서 성격 / 대상 환경**  
> 이 문서는 `market-ai-dev`의 **개발 · 재빌드 · 배포 · 장기 유지보수 contract**를 보존하는 개발자용 문서다. 운영 실행·상태 확인·빌드 진입점은 `market-ai-dev/README.md`를 우선한다.  
> 개발/빌드 PC는 Python과 필요한 빌드 도구를 사용할 수 있지만, 최종 `market-ai` runtime은 Python-free를 목표로 한다. KIS Bridge는 Windows/x86/eFriend ActiveX 환경을 전제로 한다.
>
> **Source of Truth 구분**  
> 실제 구현값·상수·endpoint는 최신 소스와 build script가 우선하고, 이 문서는 그 구현이 지켜야 할 장기 책임 경계와 배포 contract를 소유한다. 문서와 실제 소스가 충돌하면 의도된 변경인지 회귀인지 판단한 뒤 contract가 바뀐 경우 같은 작업에서 문서를 갱신한다.

---


# 1. 문서 운영 원칙

이 문서는 changelog가 아니다.

평가·점수·A/B/C·반례 생성·수정 종료 기준은 `market_ai_evaluation_guide.md`가 소유한다. 이 handover는 실제 architecture, build/deploy, runtime/session, API와 장기 유지보수 contract를 설명하며 평가 기준을 중복 누적하지 않는다.

코드가 바뀌었다는 이유만으로 자동 수정하지 않고, 다음과 같은 **장기 contract가 실제로 바뀐 경우** 기존 항목을 현재 상태로 수정한다.

- runtime / 배포 architecture
- 외부 접근 / CORS / 네트워크 boundary
- Signal 입력·phase·engine 의미
- provider / canonical symbol 의미
- KIS Bridge API / route / rollover
- DB 보존·migration contract
- Backtest / Calibration 호환성
- 향후 유지보수자가 잘못 변경하기 쉬운 운영 제약

단발성 로그, 특정 QA 횟수, 당시 월물 코드, **일회성·미세 px/UI 보정값**은 누적하지 않는다. 반복 회귀를 막는 안정적인 responsive/interaction contract는 장기 유지보수 규칙으로 남길 수 있다.

---

# 2. 현재 운영 아키텍처

## 2.1 Python-free Investment Local Suite

대상 Windows PC의 일반 실행은 외부 Python 설치에 의존하지 않는다.

```text
Desktop shortcut
        ↓
Windows Scheduled Task (Highest)
        ↓
InvestmentLocalSuite.exe + _suite_internal/
        ↓
eFriend Expert
        ↓
자동 로그인 / 인증서
        ↓
KisKospi200Bridge.exe
(KIS eFriend Market Bridge · x86 ActiveX)
        ↓
MarketAI.exe + _internal/
        ↓
FastAPI 127.0.0.1:8001
        ↓
Local Suite remote GET-only proxy 127.0.0.1:8002
        ↓
Tailscale 상태 / Serve 자가복구 (optional, :8002만 공개)
        ↓
Investment Dashboard embedded HTTP :8000
```

현재 불변조건:

- 대상 PC 외부 Python/pip/venv 불필요
- runtime에서 `python -m uvicorn`, `python -m http.server`, pip install 호출 금지
- `MarketAI.exe`는 PyInstaller **onedir**
- `InvestmentLocalSuite.exe`도 PyInstaller **onedir**
- launcher support directory는 `_suite_internal/`
- Market AI support directory는 `_internal/`
- 두 support directory를 서로 혼동하지 않는다
- 특별한 이유 없이 launcher를 onefile로 되돌리지 않는다
- Dashboard HTTP는 launcher 내부 `ThreadingHTTPServer`
- Local Suite는 Market AI가 준비된 뒤 Tailscale backend 상태와 Serve 설정을 확인한다.
- Tailscale service가 멈춘 경우 `Tailscale` Windows service 시작을 best-effort로 시도한다.
- canonical Serve가 없거나 `127.0.0.1:8002` GET-only proxy를 가리키지 않으면 `tailscale serve --bg 8002`로 복구한 뒤 remote `/api/health`를 확인한다.
- FastAPI의 전체 로컬 API는 **코드에서 `127.0.0.1:8001`로 고정**한다. `MARKET_AI_HOST`/`MARKET_AI_PORT` 같은 환경변수로 full backend를 다른 NIC/포트에 재바인딩하지 않는다. Bridge와 로컬 유지보수 write endpoint는 8001에 직접 접근하고 Tailscale Serve에는 노출하지 않는다.
- Local Suite의 `127.0.0.1:8002` proxy는 `GET / HEAD / OPTIONS`만 backend로 전달하고 `POST / PUT / PATCH / DELETE`는 405로 차단한다. CORS preflight를 위해 `OPTIONS`는 허용하되 실제 write method는 전달하지 않는다.
- 8002 GET-only proxy가 기동하지 못하면 원격 기능은 **fail-closed**로 처리한다. canonical Serve root를 `tailscale serve off`로 해제하여 과거 `Serve → 8001` direct mapping이 write API를 다시 원격 노출하지 못하게 한다. Serve status에 정상 `proxy http://127.0.0.1:8002`가 있더라도 **어느 handler에서든 `proxy http://127.0.0.1:8001`이 동시에 발견되면 unsafe mixed mapping**으로 보고 Serve root를 내린 뒤 8002 canonical mapping만 재구성한다. GET-only Serve 복구 자체가 실패하거나 복구 후 status를 다시 확인할 수 없으면 안전을 증명할 수 없으므로 `serve off`를 best-effort로 시도하고 remote를 fail-closed로 둔다.
- Tailscale 미설치·로그인 필요·연결/Serve/remote health 실패는 **원격 기능 경고**이며 Local Suite 로컬 기동 실패 사유가 아니다.
- 정상 Serve 설정은 매 실행마다 재작성하지 않는다.
- Dashboard 소스는 형제 `../investment-dashboard`
- 시스템 트레이 아이콘은 `InvestmentLocalSuite.exe` 하나만 소유
- `KisKospi200Bridge.exe`는 별도 x86 프로세스로 유지하되 자체 `NotifyIcon`은 사용하지 않음
- Local Suite 트레이 최상단의 `로컬 브라우저 열기`는 로컬 Investment Dashboard(`http://localhost:8000/`)를 연다. `Local Suite 상태/로그` 창은 상태·로그 확인에만 집중하며 브라우저 열기/자동 로그인 설정 버튼을 두지 않는다.
- Local Suite 트레이의 `보유종목 실시간 시세` 메뉴가 private window message로 숨겨진 Bridge 모니터 창을 같은 프로세스에서 다시 표시한다. 내부 프로세스/로그 명칭인 KIS eFriend Market Bridge는 유지한다.
- Bridge 창의 `X`/`Alt+F4`는 `WM_SYSCOMMAND / SC_CLOSE` 단계에서 실제 Close transaction 전에 즉시 Hide하고, 최소화도 Hide 처리한다. 숨긴 뒤에도 Bridge 프로세스는 유지한다
- Local Suite 트레이 종료 메뉴는 실제 종료 순서와 동일하게 `서버·Bridge 종료`, `서버·Bridge·eFriend 종료`로 유지
- `서버·Bridge 종료`는 eFriend Expert를 유지하고, `서버·Bridge·eFriend 종료`만 eFriend를 마지막에 종료
- eFriend 자동 로그인 자격 증명은 Windows Credential Manager에 보관하며, 트레이의 `eFriend 자동 로그인 설정`에서 관리
- FastAPI는 `monitor/index.html + monitor.css + monitor.js` 3파일을 `/monitor/`에 제공한다. Web Monitor의 브라우저 탭/화면 제목은 모든 viewport에서 `보유종목 실시간 시세`로 통일한다. Web Monitor는 read-only 운영 관찰면이며 Dashboard lease를 소유하지 않는다. 접근·responsive·embedded 계약은 §12.5를 따른다.

현재 일반 실행은 Highest Scheduled Task를 등록한 바탕화면 바로가기를 사용하여 관리자 권한 runtime을 매 실행마다 UAC 승인하지 않는 형태를 기준으로 한다.

코드서명은 현재 PC의 신뢰된 publisher 식별과 실행 신뢰도를 높이기 위한 배포 절차이며, `requireAdministrator` 자체를 제거하는 수단으로 취급하지 않는다.

---

## 2.2 실행 폴더와 개발 폴더

`market-ai`와 `market-ai-dev`는 **같은 부모 폴더의 형제 디렉터리**여야 한다. 공식 build/deploy helper는 이 이름과 위치를 contract로 검증하며 임의의 다른 운영 target을 받지 않는다.

```text
parent\
├─ investment-dashboard\
├─ market-ai-dev\       # 개발 / 빌드 Source of Truth
└─ market-ai\           # 실행 / 운영 Runtime
```

### `market-ai` — 실행 전용

```text
market-ai\
├─ _internal\
├─ _suite_internal\
├─ db\
│  └─ market_signal.db
├─ tools\
│  └─ close-efriend-tray.ps1
├─ AxInterop.ITGExpertCtlLib.dll
├─ Interop.ITGExpertCtlLib.dll
├─ InvestmentLocalSuite.exe
├─ InvestmentLocalSuite.ico
├─ KisKospi200Bridge.exe
├─ KisKospi200Bridge.exe.config
└─ MarketAI.exe
```

`.env`는 존재할 수 있으나 **기본 runtime의 필수 파일이 아니다.**

`start-local-server.log`는 `InvestmentLocalSuite.exe` 실행 시 실행폴더 root에 생성될 수 있는 **로컬 runtime 로그**다.

- canonical runtime 배포 파일이 아님
- GitHub 추적 대상이 아님
- 필요하면 삭제 가능
- 다음 Local Suite 실행 시 다시 생성
- 실행 시 기존 내용을 비우고 현재 실행 로그를 기록할 수 있음
- PC 로컬 경로 등 환경별 정보가 포함될 수 있으므로 공유/배포 산출물로 보지 않음

### `market-ai-dev` — 개발 / 재빌드 Source of Truth

`market-ai-dev`는 **소스 + 빌드/서명/배포 도구 + 테스트 + 참고자료**를 보관한다. 운영 EXE/DLL/support directory는 상시 보관하지 않는다. `db/` 아래에는 개발 중 생성된 mutable data가 존재할 수 있지만 build prerequisite나 운영 배포 산출물로 취급하지 않는다.

현재 clean dev 기준 주요 구조:

```text
market-ai-dev\
├─ ai\
├─ backtest\
├─ bridges\
├─ calibration\
├─ collectors\
├─ db\
│  ├─ market_signal.db              # 존재 가능: 개발용 mutable data
│  └─ *.py
├─ eFriendQA\
├─ KisKospi200Bridge\               # C# source only; bin/obj는 clean 상태에 없음
├─ market\
├─ news\
├─ signals\
├─ monitor\
│  ├─ index.html
│  ├─ monitor.css
│  └─ monitor.js
├─ tests\
├─ tools\
│  ├─ clean-dev-artifacts.ps1
│  ├─ close-efriend-tray.ps1
│  ├─ runtime-deploy.ps1
│  ├─ runtime-stop.ps1
│  └─ run-tests.py
├─ .env.example
├─ .gitattributes
├─ .gitignore
├─ README.md
├─ app.py
├─ build-investment-local-suite.ps1
├─ build-kis-bridge-release.bat
├─ build-market-ai.ps1
├─ config.py
├─ InvestmentLocalSuite.ico
├─ KisKospi200Bridge.sln
├─ market_ai_project_handover.md
├─ market_ai_evaluation_guide.md
├─ requirements.txt
├─ requirements-test.txt
├─ requirements-lock.txt
├─ requirements-build-lock.txt
├─ requirements-openai.txt
├─ run_market_ai.py
├─ Initialize-InvestmentLocalSuiteSigning.ps1
├─ Sign-InvestmentLocalSuite.ps1
└─ start-local-server.pyw
```

다음은 **clean dev에 남아 있으면 안 되는 generated/runtime artifact**다.

```text
_internal/
_suite_internal/
MarketAI.exe
InvestmentLocalSuite.exe
KisKospi200Bridge.exe
KisKospi200Bridge.exe.config
AxInterop.ITGExpertCtlLib.dll
Interop.ITGExpertCtlLib.dll
KisKospi200Bridge/bin/
KisKospi200Bridge/obj/
_runtime-backup/
.pytest_cache/
__pycache__/
*.pyc
*.pyo
```

`tools/clean-dev-artifacts.ps1`가 위 항목을 제거하고 `-VerifyOnly`로 clean 상태를 검증한다. 세 공식 build script는 build 전 cleanup과 성공 직전 clean verification을 수행하므로 **정상 SUCCESS 후 dev root에 운영 runtime이 남지 않는 것이 contract**다.

`.gitignore`도 위 runtime/build/cache의 재유입을 차단한다. 단, frozen runtime의 native extension까지 잘못 제외하지 않도록 `*.pyd` 같은 광범위 ignore는 사용하지 않는다.

`.gitattributes`는 `* text=auto`로 Git index의 텍스트 줄바꿈만 정규화해 Windows의 CRLF/LF 차이만으로 dirty 상태가 생기는 것을 줄인다. `*.ico`·`*.zip`·`*.pdf`는 binary로 지정해 text normalization 대상에서 제외한다.

`db/market_signal.db`와 `db/dashboard_quote_universe.json`은 generated executable이 아니라 개발 중 생길 수 있는 mutable state다. build/test prerequisite가 아니며 `clean-dev-artifacts.ps1`가 삭제하지 않는다. 특히 `db/market_signal.db`는 누적 개발 데이터일 수 있으므로 자동 cleanup 대상으로 만들지 않는다. `dashboard_quote_universe.json`은 local transient state로 `.gitignore` 대상이다.

`eFriendQA/`는 runtime dependency가 아니라 KIS eFriend 수정·검증용 참고자료다. 용량 정리가 필요하면 별도 보관할 수 있으나 runtime cleanup과 혼동하지 않는다.

소스와 build/deploy script를 Source of Truth로 유지하고, 재생성 가능한 EXE/support directory나 build residue를 개발 contract로 의존하지 않는다.

Source-test 환경은 운영 frozen lock과 분리한다. `requirements-test.txt`는 플랫폼 공통 `requirements.txt`에 pytest만 더하며, 정식 source QA는 다음 경로를 사용한다.

```text
python -m pip install -r requirements-test.txt
python tools/run-tests.py -q
```

`tools/run-tests.py`는 필수 distribution 누락을 먼저 보고하고, fake module/stub으로 import 실패를 숨기지 않은 채 같은 interpreter의 `python -m pytest`를 실행한다.

# 3. 빌드 / 배포 contract

공식 build script는 이제 **빌드 산출물을 dev root에 두지 않고**, 검증된 staging set을 같은 부모의 sibling `../market-ai`에 자동 반영한다. 정상적인 정식 빌드에서 운영 파일을 사람이 dev에서 `market-ai`로 수동 복사하는 절차는 없다.

공통 경계:

```text
market-ai-dev source
→ TEMP / C# Release staging
→ build별 검증
→ tools/runtime-stop.ps1
→ tools/runtime-deploy.ps1
→ sibling ../market-ai
→ SHA-256 manifest verify
→ clean-dev verify
→ BUILD + DEPLOY : SUCCESS
```

`market-ai-dev`와 `market-ai`는 같은 부모의 형제 폴더여야 하고, dev 폴더명은 `market-ai-dev`, runtime 폴더명은 `market-ai`여야 한다. runtime 식별은 **정확한 두 폴더명 + 동일 부모 경로 + dev/runtime 분리**로 검증하며, 운영 폴더의 `README.md` 존재 여부에는 의존하지 않는다.

## 3.1 공통 Runtime stop contract

세 build는 운영 세트를 교체하기 전에 `tools/runtime-stop.ps1`을 사용한다.

- 실행 중인 `InvestmentLocalSuite.exe`가 있으면 tray의 **`서버·Bridge 종료`**와 같은 `WM_COMMAND / 1004` 경로를 요청한다.
- 이 경로는 `_request_exit(stop_efriend=False)`로 연결되므로 Dashboard embedded server, MarketAI, Bridge, Local Suite를 종료하고 **eFriend Expert는 유지**한다.
- 관리자 권한으로 실행 중인 Local Suite에 일반 PowerShell이 메시지를 전달할 수 없는 Windows UIPI 경계를 고려해, 필요한 경우 stop helper만 UAC elevation으로 재실행한다.
- tray shutdown이 완료되지 않을 때만 sibling `market-ai` 아래에서 실행된 예상 프로세스 PID를 대상으로 fallback한다. 이름이 같은 다른 경로의 프로세스를 광범위하게 종료하지 않는다.
- Bridge fallback 전에는 기존 graceful exit message를 먼저 요청한다.
- 다른 Runtime 경로의 `KisKospi200Bridge.exe`가 이미 실행 중이면 그 프로세스는 종료하지 않되, 현재 Runtime의 두 번째 Bridge도 시작하지 않고 fail-closed한다. shutdown은 owned Bridge만 종료하고 external Bridge는 보호한다.
- 8000/8001/8002를 무관한 프로세스가 점유한 경우 그 프로세스를 죽이지 않고 fail-closed한다.
- stop 성공 조건은 `InvestmentLocalSuite.exe`, `MarketAI.exe`, `KisKospi200Bridge.exe`의 owned runtime이 없고 8000/8001/8002가 모두 free인 것이다.

빌드/배포는 runtime을 자동으로 다시 시작하지 않는다. 여러 component를 연속 재빌드할 때는 모두 끝난 뒤 `InvestmentLocalSuite.exe`를 평소 방식으로 한 번 실행한다.

## 3.2 공통 Runtime deploy / rollback contract

`tools/runtime-deploy.ps1`은 component별 whitelist만 sibling `market-ai`에 반영한다.

```text
MarketAI
  MarketAI.exe + _internal/

InvestmentLocalSuite
  InvestmentLocalSuite.exe + _suite_internal/

KisBridge
  KisKospi200Bridge.exe
  KisKospi200Bridge.exe.config
  AxInterop.ITGExpertCtlLib.dll
  Interop.ITGExpertCtlLib.dll
```

주요 불변조건:

- staging root가 운영 `market-ai` 내부이면 거부한다.
- staging manifest가 비어 있거나 필수 파일/폴더가 없으면 거부한다.
- 기존 component runtime은 `%TEMP%\InvestmentLocalSuite\RuntimeDeployRollback\` 아래에 백업하고 **SHA-256 manifest 검증**을 통과한 뒤에만 제거한다.
- MarketAI의 `_internal/`, Local Suite의 `_suite_internal/`은 기존 폴더에 덮어쓰지 않고 **폴더 전체를 먼저 삭제한 뒤** 새 support directory를 복사한다.
- executable은 support directory보다 뒤에 반영한다.
- 배포 후 staging ↔ operating runtime의 파일 수, 상대경로, SHA-256을 비교한다.
- 배포 중 실패하면 partial 신규 세트를 제거하고 검증된 이전 세트를 복원한다. 안전한 정리가 불가능하면 old/new 혼합 복원을 강행하지 않고 fail-closed하며 검증된 rollback backup 위치를 남긴다.
- 정상 성공 후 rollback temp는 제거한다. 제거 실패는 운영 배포 성공을 되돌리지는 않지만 경고를 남긴다.
- `-PlanOnly`는 target/staging/manifest를 검증하되 운영 파일을 변경하지 않는다.

공통 deploy whitelist에 없는 다음 운영 mutable/static 자원은 일반 build가 덮어쓰지 않는다.

```text
db/market_signal.db
.env                 # 실제 사용하는 경우
InvestmentLocalSuite.ico
tools/close-efriend-tray.ps1
start-local-server.log
```

특히 dev의 DB를 운영 DB 위에 build artifact처럼 복사하지 않는다.

## 3.3 Market AI 백엔드

```text
Python source
→ build-market-ai.ps1
→ clean-dev preflight
→ TEMP onedir staging: MarketAI.exe + _internal/
→ 운영 Runtime stop
→ 격리된 staged frozen-runtime Smoke QA
→ runtime-deploy.ps1 -Component MarketAI
→ sibling market-ai clean replacement + SHA-256 verify
→ clean-dev verify
→ MARKET AI BUILD + DEPLOY : SUCCESS
```

`MarketAI.exe`와 `_internal/`은 반드시 동일 빌드의 한 세트다.

build는 Python 3.13 x64를 강제하고 `%TEMP%\InvestmentLocalSuite\MarketAI\` 아래 새 venv/staging을 사용한다. `requirements-build-lock.txt`의 build closure와 `requirements-lock.txt`의 runtime closure를 exact pin 기반으로 설치하고 `pip check`와 exact-version audit를 통과해야 한다. 전역 Python package에 의존하지 않는다.

staged smoke는 격리된 `MARKET_AI_HOME`과 임시 SQLite DB를 사용하고 collector/news/AI/signal을 비활성화한다. 운영/개발 DB와 외부 수집을 smoke fixture로 사용하지 않는다. 현재 backend가 8001 고정이므로 staged smoke 전에 shared runtime-stop contract로 기존 운영 runtime을 정지한다. smoke 실패 시 **운영 component는 배포하지 않는다.**

`app.py`, CORS, endpoint, service wiring 등 backend Python을 수정했다면 이 build가 필요하다.

`monitor/index.html`, `monitor.css`, `monitor.js` **정적 3파일만** 수정한 운영 hotfix는 §12.5 direct replacement fast-path를 사용할 수 있다. backend Python이나 packaging/build contract가 함께 바뀌면 예외를 사용하지 않는다.

## 3.4 Local Suite launcher

```text
start-local-server.pyw
→ build-investment-local-suite.ps1
→ clean-dev preflight
→ TEMP onedir staging: InvestmentLocalSuite.exe + _suite_internal/
→ staged EXE Authenticode signing + VALID 확인
→ 운영 Runtime stop
→ runtime-deploy.ps1 -Component InvestmentLocalSuite
→ sibling market-ai clean replacement + SHA-256 verify
→ deployed EXE signature VALID 재확인
→ clean-dev verify
→ INVESTMENT LOCAL SUITE BUILD + DEPLOY : SUCCESS
```

launcher는 onedir 구조를 유지한다. build는 Python 3.13 x64와 `%TEMP%\InvestmentLocalSuite\LauncherOnedir\`의 clean venv를 사용하며 build-tool exact lock을 검증한다.

**dev root의 `MarketAI.exe`, `_internal/`, Bridge EXE는 Local Suite build prerequisite가 아니다.** Market AI/Bridge runtime은 sibling `market-ai`의 별도 component이며 Local Suite staging에 합쳐 넣지 않는다.

Local Suite source gate는 8002 GET-only allowlist, remote `client_id` namespace, ticker/capacity 경계와 proxy/backend ownership contract를 확인한다.

해당 Windows 사용자 계정에서 처음 서명할 때는 `Initialize-InvestmentLocalSuiteSigning.ps1`를 1회 실행한다. 이후 `Sign-InvestmentLocalSuite.ps1`가 CurrentUser 범위의 인증서를 사용한다. **완전한 staging set 생성과 서명 검증이 끝난 뒤에만 운영 runtime을 정지**한다.

## 3.5 KIS Bridge

실행 파일명은 호환성을 위해 `KisKospi200Bridge.exe`를 유지하지만 현재 역할은 **KIS eFriend Market Bridge**다.

```text
KisKospi200Bridge source
→ build-kis-bridge-release.bat
→ Release|x86 staging
→ 4개 runtime file 존재 확인
→ 운영 Runtime stop
→ runtime-deploy.ps1 -Component KisBridge
→ sibling market-ai 4파일 set 배포 + SHA-256 verify
→ local bin/obj cleanup
→ clean-dev verify
→ KIS BRIDGE BUILD + DEPLOY : SUCCESS
```

Bridge build PC에는 Visual Studio 2022 또는 Build Tools 2022의 MSBuild/.NET desktop development 환경이 필요하다. `.csproj`는 더 이상 `AfterTargets=Build`로 dev root에 runtime 파일을 자동 복사하지 않는다. C# `bin/obj`는 staging/build residue일 뿐 운영 Source of Truth가 아니며 성공 후 clean helper가 제거한다.

배포 세트:

```text
KisKospi200Bridge.exe
KisKospi200Bridge.exe.config
AxInterop.ITGExpertCtlLib.dll
Interop.ITGExpertCtlLib.dll
```

Bridge는 KOSPI/KOSPI200 선물과 Dashboard가 요청한 동적 KRX 보유종목 `SC_R` universe를 수신한다. `005930`, `000660`은 Signal Engine baseline quote ticker로 유지한다.

## 3.6 Dashboard

Dashboard HTML/CSS/JS 수정은 MarketAI.exe 또는 InvestmentLocalSuite.exe 재빌드를 요구하지 않는다.

Local Suite build도 sibling `investment-dashboard/index.html`을 build prerequisite로 요구하지 않는다. Dashboard는 별도 consumer/runtime ownership으로 유지한다.

## 3.7 전체 재빌드 권장 순서

세 component는 dev-root runtime dependency를 공유하지 않으므로 각각 독립적으로 빌드 가능하다. 전체 runtime을 다시 만들 때의 **운영상 권장 순서**는 다음과 같다.

```text
1. build-market-ai.ps1
2. build-kis-bridge-release.bat
3. build-investment-local-suite.ps1
4. 세 build가 모두 SUCCESS인지 확인
5. InvestmentLocalSuite.exe를 평소 방식으로 실행
6. Windows runtime 실기 QA
```

첫 build에서 runtime이 실행 중이면 shared stop helper가 `서버·Bridge 종료` lifecycle을 수행한다. 이후 build에서는 이미 stopped 상태를 확인하고 진행한다. 각 build는 성공한 자기 component만 sibling `market-ai`에 자동 반영하므로 중간에 사람이 파일을 옮기지 않는다.

`build-kis-bridge-release.bat --ensure`는 sibling 운영 Bridge가 C# source보다 최신이고 4파일 세트가 존재하면 재빌드/재배포를 생략할 수 있다.

## 3.8 Clean-dev contract

세 build 시작 전:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\clean-dev-artifacts.ps1
```

성공 직전에는 cleanup 후 다음 검증을 수행한다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\clean-dev-artifacts.ps1 -VerifyOnly
```

정상 SUCCESS 시 dev에 남지 않아야 하는 대표 항목:

```text
MarketAI.exe
_internal/
InvestmentLocalSuite.exe
_suite_internal/
KisKospi200Bridge.exe
KisKospi200Bridge.exe.config
AxInterop.ITGExpertCtlLib.dll
Interop.ITGExpertCtlLib.dll
KisKospi200Bridge/bin/
KisKospi200Bridge/obj/
_runtime-backup/
.pytest_cache/
__pycache__/
*.pyc
*.pyo
```

`db/` 개발 mutable data는 clean helper가 삭제하지 않는다. build/test는 이를 prerequisite나 운영 배포 source로 사용하지 않는다.

## 3.9 SUCCESS / 실패 해석

공식 build의 최종 `... BUILD + DEPLOY : SUCCESS`는 **build만 성공했다는 뜻이 아니라 해당 component의 sibling 운영 반영과 검증, clean-dev 확인까지 완료**됐다는 뜻이다.

대표 성공 단계:

```text
Build         : PASS
Runtime stop  : PASS
Smoke/Sign    : PASS   # component에 따라 해당
Deploy verify : PASS   # SHA-256 manifest
Dev cleanup   : PASS
```

반대로 중간 `[ERROR]`/예외가 발생하고 최종 SUCCESS가 없으면 운영 반영 완료로 간주하지 않는다. deploy helper가 rollback을 수행했다면 로그의 restore 결과를 확인한다. build는 runtime을 자동 재시작하지 않으므로 성공 후에도 서버가 stopped인 것은 정상이다.

운영 `market-ai`의 `start-local-server.log`는 dev build artifact와 별개인 실행 시 생성 runtime log이며 GitHub/배포 세트에 포함하지 않는다.

# 4. 외부 Dashboard / Tailscale Serve contract

Market AI 계산과 증권 프로그램은 로컬 PC에 유지하고 **조회 API만 Tailscale Serve를 통해 외부 Dashboard에 제공**한다.

현재 구조:

```text
GitHub Pages Dashboard
https://tkfkd3226-cell.github.io/investment-dashboard
        │
        │ HTTPS fetch
        ▼
https://node.tail60a98e.ts.net
        │
        │ Tailscale Serve
        ▼
http://127.0.0.1:8002
        │  Local Suite GET-only reverse proxy
        │  GET / HEAD / OPTIONS only
        ▼
http://127.0.0.1:8001
        │
        ▼
MarketAI.exe
```

장기 경계:

- FastAPI는 계속 loopback `127.0.0.1:8001`에 바인딩한다. 이 포트에는 Bridge 수신과 유지보수용 write endpoint를 포함한 전체 로컬 API가 존재한다.
- Local Suite는 원격 전용 GET-only reverse proxy를 `127.0.0.1:8002`에 바인딩하고 Tailscale Serve는 **8002만** proxy한다.
- remote proxy는 모든 GET을 통과시키지 않는다. Dashboard 조회용 `/api/health`, `/api/signal/latest`, `/api/market-data/snapshot`, `/api/market-data/krx-quotes`, `/api/bridge/kis-efriend/status`와 Web Monitor read-only 조회용 `/api/bridge/kis-efriend/quote-universe`, 정적 UI `/monitor/`, `/monitor/index.html`, `/monitor/monitor.css`, `/monitor/monitor.js`만 allowlist로 둔다. 해당 경로의 `GET / HEAD / OPTIONS`만 backend로 전달하며 allowlist 밖 GET은 404, write method는 405로 proxy에서 종료한다. `/monitor`는 상대 asset 경로가 tailnet host를 유지하도록 proxy가 `/monitor/`로 canonical redirect한다.
- `/api/market-data/krx-quotes`는 Dashboard의 탭별 lease 의미를 유지하되 원격 `client_id`를 `remote-<sha256-prefix>` namespace로 정규화하여 로컬 8001 client identity와 충돌하지 않게 한다. **8002 proxy는 backend lease 상태를 복제하지 않는다.** proxy는 ticker/client 형식과 client당 최대 64 ticker만 검증하고, 실제 lease admission은 8001 `KrxQuoteService`의 단일 lock/table에서 처리한다. backend는 원격 active client를 최대 16개로 제한하며, 원격 요청은 현재 로컬 lease·Signal baseline뿐 아니라 **첫 local lease 전의 local restart bootstrap까지 예약 용량으로 보호한 뒤 남은 capacity만** 사용할 수 있다. 반대로 로컬 요청이 들어왔을 때 기존 원격 lease 때문에 64-ticker 물리 한도를 넘는다면 가장 오래된 원격 lease부터 필요한 만큼 회수하여 로컬 요청을 우선한다. 첫 **admission 성공 local request**는 explicit `[]`까지 포함해 bootstrap을 해제하는 authoritative lease이며, capacity 초과 등으로 거절된 local request는 bootstrap 상태를 바꾸지 않으며, 로컬 lease들 자체가 물리 한도를 넘는 경우에만 로컬 요청도 fail-closed한다. 따라서 원격 사용자가 먼저 62개를 점유해도 이후 정상 로컬 Dashboard가 422로 밀려나지 않고, 재시작 직후 원격이 먼저 붙어도 local bootstrap을 지울 수 없다.
- local authoritative lease가 성공한 뒤 `dashboard_quote_universe.json` 쓰기만 실패한 경우 **현재 API 성공을 500으로 뒤집지 않는다.** 이 파일은 restart warm-up용 recovery aid이며 in-memory admission의 transaction owner가 아니다. 파일 I/O 실패는 warning으로 남기고 다음 local request에서 다시 persistence를 시도한다.
- 따라서 tailnet 사용자가 FastAPI의 POST maintenance/ingest endpoint나 Dashboard가 사용하지 않는 조회면을 직접 실행할 수 없어야 한다.
- Local Suite가 Tailscale의 설치/연결/Serve 상태를 startup에서 점검하고 가능한 범위에서 자가복구한다.
- Tailscale backend가 `NeedsLogin`이면 자동 `tailscale up`이나 인증 변경을 시도하지 않고 경고만 남긴다.
- Serve 자가복구 성공 여부와 별개로 로컬 `127.0.0.1:8001` Market AI는 계속 사용할 수 있어야 한다.
- 공유기 port forwarding으로 8001을 인터넷에 직접 공개하지 않는다.
- Tailscale Serve가 tailnet 내부 HTTPS reverse proxy 역할을 하되 target은 FastAPI 8001이 아니라 Local Suite GET-only proxy 8002다.
- Market AI PC와 외부 client 모두 같은 tailnet 접근 권한이 있어야 한다.
- 로컬 PC 또는 Local Suite가 꺼지면 원격 Market AI도 사용할 수 없다.
- Dashboard의 일반 정적 기능과 Market AI runtime availability를 분리해서 생각한다.

현재 canonical Tailscale Serve endpoint:

```text
https://node.tail60a98e.ts.net
```

이 endpoint가 바뀌면 Dashboard frontend의 remote API base와 문서를 함께 맞춘다.

---

# 5. CORS contract

GitHub Pages Dashboard는 Tailscale Serve endpoint와 서로 다른 Origin이므로 FastAPI CORS 허용이 필요하다.

현재 허용해야 하는 Dashboard Origin:

```text
https://tkfkd3226-cell.github.io
```

불변조건:

- CORS에는 `/investment-dashboard` path를 포함하지 않고 Origin만 등록
- 단순 편의를 위해 `allow_origins=["*"]`로 넓히지 않음
- 실제 Dashboard host만 명시적으로 허용
- Dashboard host가 변경되면 `app.py` CORS도 검토
- `app.py` 변경 후 반드시 `build-market-ai.ps1` 재빌드
- `build-market-ai.ps1`가 `MarketAI.exe + _internal/` 동일 세트를 sibling `market-ai`에 자동 배포
- 직접 주소창에서 API가 열리는 것과 cross-origin JavaScript `fetch()` 성공은 별개이므로 원격 QA에서 둘을 구분

CORS는 frontend layout contract가 아니라 **Market AI HTTP boundary contract**이므로 이 handover에서 유지한다. 8002 GET-only proxy는 allowlist 경로에 한해 Dashboard의 `Origin`/preflight header와 backend의 CORS response header를 전달하며, CORS를 write authorization으로 사용하지 않는다.

---

# 6. 프로젝트 목표와 ownership

Market AI 책임:

- 국내/미국 시장 데이터 수집
- eFriend 실시간 KOSPI·KOSPI200 선물·동적 KRX 보유종목 SC_R 수신
- 실제 KOSPI200 선물 수신
- 반도체/선행시장 데이터
- FX / 유가 / 미국채 금리
- 뉴스 수집
- 선택적 OpenAI 뉴스 구조화
- Rule Signal Engine
- Prediction Outcome / Backtest
- Probability Calibration
- Dashboard용 조회 API

자동 주문 시스템이 아니다.

주문 API, 계좌번호, 계좌 비밀번호를 사용하지 않는다.

소유권:

- `app.py`: FastAPI entry, HTTP/CORS contract, service 조합
- `signals/`: Rule Signal 계산
- `bridges/`: KIS eFriend 실시간 입력, KOSPI200 route/session
- `market/`, `collectors/`: 일반 market provider / snapshot/history / eFriend 우선·Yahoo fallback 정책
- `backtest/`, `calibration/`: 사후 평가 / 확률 보정
- `db/`: persistence contract
- `KisKospi200Bridge/`: eFriend ActiveX real-time 수신 및 HTTP 전송. 파일명은 legacy이지만 현재는 K200·KOSPI·동적 KRX 보유종목 SC_R를 함께 처리하는 Market Bridge 역할
- Dashboard UI/responsive: investment-dashboard 프로젝트 소유

Market AI backend가 Dashboard DOM이나 UI state를 직접 알게 만들지 않는다.

---

# 7. 데이터 저장 및 무결성 contract

## 7.1 운영 DB

반드시 보존:

```text
db/market_signal.db
```

빌드, `_internal` 교체, runtime cleanup, 문서 작업에서 삭제·초기화하지 않는다.

DB schema migration이 필요하면 기존 누적 데이터를 최우선으로 보호한다.

## 7.2 Snapshot / History

- 최신 snapshot과 history 의미를 구분
- 오래된 observation이 더 최신 snapshot을 덮지 않음
- provider observed time과 server received time을 혼동하지 않음
- heartbeat 성공만으로 오래된 quote를 fresh하게 만들지 않음

## 7.3 `.env`

`.env`는 외부 mutable configuration이다.

- 기본 실행 필수 아님
- EXE에 포함하지 않음
- GitHub/공유 ZIP 포함 금지
- OpenAI Key, KRX override 등 실제 설정이 필요한 경우에만 사용

---

# 8. 실제 KOSPI200 선물과 proxy

canonical:

```text
FUTURES:KOSPI200
```

여기에는 실제 KIS eFriend 선물만 canonical 값으로 저장한다.

Yahoo `^KS200`은 KOSPI200 현물지수 proxy다.

불변조건:

- proxy를 실제 futures로 승격하지 않음
- 실제 KIS snapshot을 proxy가 덮지 않음
- Signal Engine에서 proxy를 실제 futures component로 사용하지 않음
- 실제값이 없으면 결측 허용

기본 정책:

```text
MARKET_AI_ALLOW_KOSPI200_INDEX_PROXY=false
```

## 8.1 국내 현물 eFriend 실시간 / Yahoo fallback

현재 canonical 실시간 입력:

```text
INDEX:KOSPI   ← JUC_R / 0001
KRX:005930    ← SC_R  / 005930   # 삼성전자
KRX:000660    ← SC_R  / 000660   # SK하이닉스
```

eFriend source 형식:

```text
kis-efriend:JUC_R:0001
kis-efriend:SC_R:005930
kis-efriend:SC_R:000660
```

현재 정책:

- KRX 정규장 중에는 eFriend snapshot이 우선이다.
- eFriend snapshot이 정규장 중 설정된 stale 기준(기본 90초, `MARKET_AI_KIS_FALLBACK_AFTER_SECONDS`)을 넘으면 Yahoo/yfinance가 장애 fallback으로 들어올 수 있다.
- 개별주식 `SC_R`은 KRX 정규장 종료 후에도 KRX 애프터마켓 운영시간(16:00~20:00) 동안 실시간 tick을 계속 수신한다. `KrxQuoteService`는 15:30 이후 개별주식을 `market_state=extended`로 구분하고, 당일 quote와 Bridge/해당 subscription이 정상인 경우 Dashboard 호환성을 위해 `state=live`, `usable=true`를 유지한다. ETF와 KOSPI 현물지수는 기존 15:30 정규장 마감 contract를 유지한다.
- 장마감 또는 해당 종목 session 종료 후에는 마지막 검증된 eFriend/KIS snapshot을 단순 경과시간만으로 Yahoo가 덮어쓰지 않는다. active `open/extended` session에서는 durable closed snapshot을 현재가로 승격하지 않고 실제 fresh `SC_R` tick을 우선한다.
- Yahoo는 국내 3종의 운영 fallback이며, 유효한 eFriend 실시간값보다 우선하지 않는다.
- history는 실시간 tick마다 무제한 적재하지 않고 backend sampling 정책을 따른다.
- 현재 설치된 eFriend Expert Viewer에는 NXT/ATS 현물 실시간 TR이 확인되지 않았다. `SC_R`은 현재 KRX 정규장 및 KRX 애프터마켓 실시간 입력으로 취급하며, NXT 실시간 통합은 별도 구현 전까지 가정하지 않는다.
- Dashboard KOSPI 툴팁의 데이터 소스 표시는 하드코딩하지 않고 snapshot `source`를 기준으로 `KIS eFriend KOSPI 실시간` 또는 Yahoo fallback을 표시한다.

---

# 9. SOX / Nasdaq-100 의미

## 9.1 SOX

```text
INDEX:SOX = PHLX Semiconductor Index 현물지수
```

현재 Signal Engine의 SOX component는 항상 `INDEX:SOX`를 사용한다.

현재 Dashboard Market AI의 SOX 시장 metric도 `INDEX:SOX`를 표시한다.

`FUTURES:SOX` 데이터가 수집되어 있더라도:

- 현재 Signal weight에 포함하지 않음
- 현재 Dashboard SOX 표시의 자동 fallback으로 사용하지 않음
- 현물과 선물의 의미를 섞지 않음

## 9.2 Nasdaq-100 선물

```text
FUTURES:NQ
```

현재 provider는 Yahoo `NQ=F`를 사용한다.

---

# 10. Signal Engine contract

현재 engine:

```text
stage6_rule_v8
```

## 10.1 Rule Score

주요 score 필드:

- `kospi_score`
- `semiconductor_score`
- `gap_up_probability`
- `up_close_probability`

calibration이 적용되지 않은 값은 통계 확률이 아니라 **0~100 Rule Score**다.

기존 `gap_up_probability`, `up_close_probability` 이름은 API/DB 호환을 위해 유지한다. `confidence`, `data_completeness`도 기존 DB/API 소비자 호환 때문에 필드는 유지하지만 v8의 사용자 설명 지표가 아니며 `details.deprecated_fields`에 명시한다. Dashboard는 이 두 값을 신뢰도/완성도로 표시하지 않는다.

## 10.2 KOSPI

```text
KOSPI 현물       35%
KOSPI200 선물    65%
```

다른 의미의 입력을 섞지 않는다.

## 10.3 반도체

```text
삼성전자          20%
SK하이닉스        20%
SOX 현물지수      20%
NVIDIA            15%
SK하이닉스 ADR    15%
Micron            10%
```

SOX는 `INDEX:SOX`.

## 10.4 갭상

장전/다음 거래일:

```text
KOSPI200 선물     50%
SOX 현물지수      25%
Nasdaq100 선물    20%
USD/KRW            5%
```

phase:

- 장전: 당일 gap 예측
- 장중: 09:00 직전 checkpoint 고정
- checkpoint 없음: 유효 signal 없음 가능
- 장마감 후/휴장: 다음 KRX 거래일 예측

## 10.5 상승마감

장전:

```text
KOSPI200 선물     50%
SOX 현물지수      30%
Nasdaq100 선물    20%
```

장중:

```text
KOSPI 현물        45%
KOSPI200 선물     35%
SOX 현물지수      12%
Nasdaq100 선물     8%
```

15:30 이후 KOSPI 종가 snapshot이 확인되면 실제 상승/하락/보합 결과로 종료한다.

## 10.6 입력 사용 정책 / 신호별 충족률 / 반영 비중

v8은 단순 wall-clock 경과에 따라 1.0/0.85/0.65/0.40으로 품질을 감점하는 정책을 사용하지 않는다. 각 원천은 거래 세션과 provider 특성에 따라 **사용 가능(1) / 제외(0)**를 판정하고, 정상 장마감값은 시간이 지났다는 이유만으로 감점하지 않는다.

- `details.signal_inputs[target].input_coverage` = 사용 가능한 configured weight 합계 / 해당 신호 configured weight 합계
- `basis[].configured_weight` = 기본 가중치
- `basis[].effective_weight` = 현재 사용할 수 있는 가중치. unavailable이면 0
- `basis[].normalized_weight` = 사용할 수 있는 입력끼리 다시 정규화한 **실제 반영 비중**
- `missing_inputs` = 제외된 구성요소와 `inputs[].status/reason` 근거
- 정상 입력이 모두 있으면 coverage 100%이며 normalized weight는 기본 가중치 의미를 그대로 유지
- 일부 입력이 빠지면 coverage는 낮아지지만 남은 입력의 normalized weight 합은 100%
- legacy checkpoint에 당시 coverage가 저장되지 않았다면 `input_coverage=null`을 유지하며 임의 복원하지 않음
- `SIGNAL_MINIMUM_DATA_WEIGHT`는 전체 평균 신뢰도가 아니라 **신호별 최소 입력 충족 조건**으로 사용
- 신호가 최소 조건을 충족하지 않으면 `signal_state[target].available=false`를 저장해 과거 정상 신호가 화면에 잔존하지 않게 함
- 실제 KIS `FUTURES:KOSPI200`은 주간 `FC_R` / 야간 `CMEC_R` source와 최근 완료 세션이 일치하고, source의 instrument code가 **그 완료 세션 종료시점의 canonical 근월물**과 일치할 때만 세션 마지막 구간 tick을 마감 후보로 인정한다. 정확한 종료시각 tick을 강제하지 않으며 현재 구현은 종료 전 30분 이내의 검증된 실제 KIS tick까지 `closed_latest`로 허용한다. 분기 만기일 주간장은 15:20 rollover를 반영하므로 15:20 이전 만기월 tick이 15:45 마감값으로 승격되지 않는다. `MARKET_AI_KIS_KOSPI200_CODE` explicit override가 설정된 경우에는 그 고정 code가 canonical code다. 그보다 오래된 tick, 주간/야간 route 불일치, instrument 불일치는 Bridge 중간 장애나 rollover drift를 가리기 위해 `missing_close`로 fail-closed한다.

정확한 session/lag 판정은 `signals/input_status.py`, score 산식과 target별 weight는 `signals/engine.py`가 Source of Truth다. consumer는 score를 재계산하지 않는다.

## 10.7 고정 예측 / phase metadata

Signal detail의 phase/mode/target session/checkpoint/calibration eligibility는 UI 장식 문자열이 아니라 **signal 값의 의미를 해석하는 backend contract**다.

장전/장중 고정 예측은 당시 score뿐 아니라 `input_coverage`, `basis`, `missing_inputs`, `basis_at`, engine version을 함께 보존한다. 전날 `locked_preopen`을 오늘 예측으로 재사용하지 않으며, 이전 v6/v7 checkpoint는 당시 저장되지 않은 coverage를 임의로 100% 처리하지 않는다. v8과 engine version이 다른 checkpoint는 새 v8 calibration 학습 대상으로 섞지 않는다.

---

# 11. Backtest / Calibration

No-lookahead 원칙을 유지한다.

- prediction 생성 이후의 실제 outcome으로 평가
- engine version 또는 target 의미가 바뀌면 과거 record를 현재 의미로 소급 변환하지 않음
- Calibration은 해당 engine/target 의미와 호환되는 기록만 사용
- 충분한 데이터가 없으면 억지 calibration 금지

현재 방식:

```text
quantile_beta_pava_v1
```

---

# 12. KIS eFriend / C# Market Bridge contract

환경:

```text
.NET Framework 4.8
x86
```

실행 파일명은 호환성을 위해 `KisKospi200Bridge.exe`를 유지하지만 UI/역할 명칭은 **KIS eFriend Market Bridge**다.

고정 realtime service:

```text
KOSPI          JUC_R   / 0001
KOSPI200 주간  FC_R
KOSPI200 야간  CMEC_R
```

보유종목 quote service:

```text
KRX 현물/ETF   SC_R    / 6자리 ticker
baseline       005930, 000660
동적 universe  Dashboard 활성 client들의 현재 보유 ticker 합집합
```

`SC_R` ticker는 숫자 6자리뿐 아니라 `0163Y0`처럼 영문을 포함한 KRX 6자리 코드도 문자열로 처리한다. 삼성전자·SK하이닉스는 기존 Signal/DB 경로의 baseline이지만, Bridge 구현은 종목별 하드코딩 control을 늘리지 않고 `ticker → ActiveX stream state` collection을 사용한다.

주요 endpoint:

```text
GET  /api/bridge/kis-efriend/route-code
GET  /api/bridge/kis-efriend/quote-universe
GET  /api/market-data/krx-quotes?tickers=...&client_id=...
POST /api/bridge/kis-efriend/tick
POST /api/bridge/kis-efriend/market-tick
POST /api/bridge/kis-efriend/heartbeat
GET  /api/bridge/kis-efriend/status
```

## 12.1 Quote universe / client lease

Dashboard의 live valuation request는 tab별 `client_id`와 현재 보유 ticker set을 Market AI에 전달한다.

- 물리 Bridge universe는 Signal baseline `005930`, `000660`과 **활성 Dashboard client ticker 합집합**을 합쳐 유지한다.
- Market AI/Bridge 시작 시 Dashboard bootstrap warm-up은 종목 목록을 Python/C#에 하드코딩하지 않는다. `MARKET_AI_HOME`의 형제 `investment-dashboard/data/portfolio.json`에서 현재 `qty > 0` 보유종목의 ticker·이름·instrument `type`을 읽어 초기 universe를 구성한다. `type`은 개별주식과 ETF 등의 종목별 시장 상태를 구분하는 metadata이며 ticker admission 자체와 분리한다. Dashboard 저장소를 읽을 수 없는 경우에만 `market-ai/db/dashboard_quote_universe.json`에 보존한 마지막 **local-only authoritative universe**를 resilience fallback으로 사용한다. 이 bootstrap은 첫 **admission 성공 local Dashboard request**가 올 때까지 로컬 예약 용량으로 유지되며 remote/Tailscale request는 해제하거나 durable state로 덮어쓸 수 없다. capacity 초과 등으로 거절된 local request도 bootstrap을 해제하지 않는다. 첫 admission 성공 local request부터 실제 local client universe가 authoritative이며 explicit `[]`도 정상 상태다.
- `dashboard_quote_universe.json`은 mutable runtime state이며 Git/배포 Source of Truth가 아니다. **원격/Tailscale lease는 이 파일에 절대 기록하지 않고 로컬 Dashboard lease 합집합만** restart warm-up fallback으로 저장한다. 로컬 route의 동시 worker가 오래된 응답 순서로 최신 state를 덮지 않도록 persistence lock을 잡은 뒤 `KrxQuoteService`에서 현재 local-only union을 다시 읽어 원자적으로 compare/replace한다. 유효한 현재 portfolio가 0종목이면 과거 state를 되살리지 않는다.
- `dashboard_tickers`는 구독/lease universe 계약을 그대로 유지하고, 모니터 표시 순서는 별도 `dashboard_display_tickers`로 제공한다. 표시 순서는 Dashboard 현황표와 같은 원칙으로 **증권계좌 평가금액 내림차순 → 증권계좌에 없는 퇴직연금 종목 평가금액 내림차순**이며, 양쪽 계좌에 같은 ticker가 있으면 한 번만 표시한다. Web Monitor와 Native Bridge는 이 배열을 우선 사용하고 구버전 backend에서는 `dashboard_tickers`로 fallback한다.
- Web Monitor가 Dashboard iframe으로 열리면 `postMessage` theme handshake를 사용해 Light/Dark 상태를 양방향 동기화한다. Monitor 단독 실행 시에는 기존 `market-ai-monitor-theme` 저장값을 사용하고, Dashboard에서 받은 테마도 로컬 저장값에 반영한다.
- 같은 client가 같은 set을 다시 요청했다고 universe version을 불필요하게 증가시키지 않는다.
- Dashboard의 Market AI와 live valuation 화면 조회 주기는 모두 **10초**이고 backend client lease는 현재 **120초**다. visible client의 단일 지연 polling 때문에 다른 client reconcile에서 오만료되지 않도록 여유를 둔다. 정확한 값은 backend source를 Source of Truth로 한다.

현재 주기/수명 contract는 다음처럼 서로 독립적이다.

```text
Web Monitor polling                10초
Dashboard Market AI polling        10초
Dashboard live valuation polling   10초
Market AI client lease            120초
Dynamic KRX DB snapshot throttle   30초
```

`30초` DB snapshot throttle을 Dashboard/Web Monitor 조회 주기로 해석하거나, `120초` client lease를 polling 주기로 바꾸지 않는다.
- lease expiry는 다른 client의 ticker 삭제 충돌을 막기 위한 lifecycle 정보다. **expiry 자체만으로 Bridge universe를 즉시 축소하지 않는다.** 모든 Dashboard가 background인 동안 당일 CLOSED quote를 보존하기 위해 다음 활성 client request가 authoritative reconcile을 수행한다.
- Dashboard universe에서 빠진 ticker는 valuation용 process-memory quote를 폐기한다. `005930`, `000660`이 Signal baseline으로 물리 구독을 계속하더라도 Dashboard quote cache를 특례로 유지하지 않으며, 다시 보유 universe에 편입되면 다른 ticker와 동일하게 새 `SC_R` tick 전까지 warming/unusable 상태를 유지한다.
- Bridge는 quote-universe API 실패/404/일시 오류 때문에 현재 정상 구독을 전부 지우지 않는다. Signal baseline stream은 유지하며 다음 resolve를 재시도한다.

## 12.2 Quote store / persistence boundary

Market AI `KrxQuoteService`는 Dashboard live valuation용 **process-memory latest quote store**다. Bridge monitor의 재시작 복원을 위한 durable latest snapshot은 별도 목적이며 Dashboard valuation cache와 같은 것으로 취급하지 않는다.

- Market AI는 ticker별 현재가/source/관측시각/subscription health만 소유한다.
- 수량·원가·원금·평가손익 계산은 Dashboard 책임이며 backend에 포트폴리오 장부를 저장하지 않는다.
- dynamic 보유종목은 Signal/Backtest history row를 늘리지 않으며, Bridge monitor와 Dashboard가 장마감 후 Market AI 재시작에도 당일 마지막 KIS 값을 복원할 수 있도록 SQLite `MarketSnapshot`의 latest value만 최대 30초 단위로 갱신한다. eFriend realtime snapshot에는 `business_time`을 함께 저장하되 `HHMMSS`가 실제 시각 범위(`00:00:00`~`23:59:59`)에 들어오는 경우만 유효값으로 인정한다. `888888`처럼 6자리이지만 유효하지 않은 값은 `null`로 정규화하고 DB에도 저장하지 않으며, UI는 유효 `business_time`이 없을 때 `observed_at`의 KST 시각으로 fallback한다. Dashboard `/api/market-data/krx-quotes`에서 durable snapshot을 `usable` quote로 승격하는 것은 **현재 시각 기준 최근 완료 KRX 거래일의 exact `kis-efriend:SC_R:<ticker>` snapshot + 해당 종목 `market_state=closed` + Bridge connected + subscription 정상 + fresh-tick 재확인 요구 없음**을 모두 만족할 때만 허용한다. 자정이 지나도 다음 KRX 정규장이 시작되기 전, 주말·휴장일에는 마지막 완료 거래일 snapshot을 계속 복원할 수 있다. 반대로 `open/extended`, 최근 완료 거래일보다 오래된 snapshot, yfinance/proxy source, subscription 오류/미구독, 장애 복구 후 새 tick 대기 상태에서는 durable snapshot을 Dashboard 현재가로 승격하지 않고 기존 JSON fallback을 유지한다.
- 기존 `KRX:005930`, `KRX:000660`은 Signal Engine이 사용하므로 기존 market snapshot/history persistence contract를 유지한다. 다만 Dashboard universe 밖에서는 해당 Signal tick이 Dashboard process-memory quote store를 다시 채우지 않는다.
- universe 밖 dynamic ticker의 임의 `market-tick`, symbol과 instrument_code 불일치, `SC_R`가 아닌 dynamic KRX service는 거부한다. Signal baseline tick은 Signal persistence를 위해 계속 허용될 수 있다.

Dashboard용 quote state 의미:

```text
live        → 정규장 또는 개별주식 시간외 + 당일 quote + Bridge/해당 subscription 정상
closed      → 해당 종목의 거래 가능 session 종료 + process-memory quote 또는 위 조건을 만족하는 최근 완료 KRX 거래일 KIS durable snapshot + Bridge/해당 subscription 정상
warming     → 구독 요청 중이거나 아직 유효 quote 없음
stale       → 기존 quote는 있으나 Bridge/해당 subscription/fresh-tick 신뢰 조건 실패
unavailable → quote/subscription을 사용할 수 없음
```

`usable=true`는 `live` 또는 신뢰 가능한 `closed`에만 사용한다. Monitor용 durable snapshot은 현재 session이 `open/extended`이면 명시적으로 `stale`, session이 종료된 경우에만 `closed`로 노출하며 Web/Native Monitor는 이 explicit state를 단순 timestamp freshness보다 우선한다. 따라서 재시작 직후 새 tick이 없는 active session의 저장값을 `정상`/`시간외`로 오인하지 않는다. `closed`는 process-memory quote가 우선이며, 그 값이 없거나 unusable한 경우에는 현재 시각 기준 **최근 완료 KRX 거래일**의 exact KIS durable snapshot을 제한적으로 복원한다. 따라서 장마감 후 자정을 넘겨 Market AI를 재시작해도 다음 정규장 시작 전에는 직전 완료 거래일의 closed quote를 복원할 수 있다. 종목별 `market_state`는 `open / extended / closed`를 별도로 제공하며, 개별주식은 정규장 15:30 이후부터 20:00까지 `extended`로 취급해 UI에서 `시간외`로 표시한다. 이 구간에는 durable closed fallback을 적용하지 않고 Dashboard 호환성을 위해 quote `state`는 계속 `live`로 유지한다. ETF와 KOSPI 현물지수는 기존 정규장 15:30 마감 contract를 유지하고, Signal Engine의 15:30 KOSPI 종가 판정도 변경하지 않는다. 저유동 ETF/종목은 단순히 마지막 tick age가 길다는 이유만으로 stale 처리하지 않는다.

## 12.3 종목별 subscription health

Bridge heartbeat는 전체 Bridge 상태와 별도로 각 `SC_R` stream의 health snapshot을 함께 전송한다.

각 ticker health에는 최소 다음 의미가 있다.

```text
subscribed
last_error
last_tick_at
tick_count
forward_success_count
last_forwarded_tick_count
```

불변조건:

- Bridge 전체 heartbeat가 살아 있어도 특정 ticker `subscribed=false` 또는 `last_error`가 있으면 **그 ticker만** stale/unusable로 만든다.
- Native Bridge 내부에서는 `SC_R` 구독/수신 오류와 Market AI HTTP 전송 오류를 별도 상태로 소유한다. `last_error`는 수신·구독 오류를 우선하고, 없을 때 전송 오류를 노출한다. **전송 성공은 전송 오류만 해제하며, 자신보다 뒤에 발생한 수신 오류를 지우거나 UI를 정상 상태로 덮어쓰지 않는다.** 수신 오류는 새 정상 `SC_R` 수신에서만 해제한다.
- 정상 ticker는 다른 ticker 장애 때문에 fallback하지 않는다.
- stream이 unhealthy로 전환되면 해당 ticker는 **새 실제 tick 필요** 상태가 된다.
- 이후 heartbeat에서 `subscribed=true`로 돌아왔다는 사실만으로 장애 전 same-day quote를 다시 usable로 만들지 않는다. 장애 lifecycle에서 `_fresh_tick_required`가 설정된 경우에는 장마감 durable snapshot도 승격하지 않고 새 `SC_R` tick을 실제 수신한 뒤에만 live/closed 복귀가 가능하다. 단순 Market AI 프로세스 재시작처럼 이전 stream 장애 상태가 없는 경우에는 위의 closed durable recovery contract를 적용할 수 있다.
- 반복된 동일 unhealthy heartbeat는 새 tick이 이미 fresh requirement를 해제한 뒤 이를 다시 무한 재무장하지 않는다.
- Bridge는 heartbeat HTTP 전송을 동시에 2개 이상 진행하지 않는다. backend는 `bridge_time` 기준 latest-wins를 적용하여 이미 반영한 subscription-health snapshot보다 오래된 지연 heartbeat를 무시한다. heartbeat ↔ SC_R tick처럼 서로 다른 HTTP 요청 사이의 causal ordering은 Bridge 생성시각(`sent_at`)·`tick_count`와 **마지막으로 HTTP 성공 응답까지 확인한 정확한 tick ID인 `last_forwarded_tick_count`**로 판정한다. `forward_success_count`는 throttling된 전송 성공 "횟수"이므로 `tick_count`와 직접 비교해 선후관계를 추론하지 않는다. 따라서 서버가 이미 더 최신 tick을 수락했지만 Bridge가 아직 그 POST 성공 응답을 받기 전 생성한 과거 unhealthy heartbeat는 무시하고, 반대로 해당 tick의 성공 응답까지 확인한 뒤 새 수신 오류가 발생하면 같은 `tick_count`라도 해당 ticker를 stale/fresh-required로 전환한다.
- Bridge heartbeat가 stale 임계치를 넘긴 뒤 다시 연결되면, 장애 전 process-memory quote를 heartbeat 복구만으로 live/closed로 부활시키지 않는다. 이전 heartbeat가 stale이 된 서버 시각 이후 실제 SC_R tick을 이미 수신했다면 그 tick 자체를 복구 증거로 인정하고, 그렇지 않으면 새 SC_R tick 전까지 `_fresh_tick_required`를 유지한다. 최초 프로세스 시작/최초 heartbeat는 재연결 장애로 간주하지 않아 closed durable recovery 계약을 불필요하게 막지 않는다.
- Bridge/stream이 heartbeat stale 임계치보다 빠르게 재시작되어 `connected=false`가 관측되지 않더라도, ticker별 `tick_count` 또는 `forward_success_count`가 이전 health보다 감소하면 새 stream epoch로 본다. 이 경우에도 새 epoch의 실제 SC_R tick이 이미 수락된 것이 확인되지 않는 한 장애 전 quote를 stale/fresh-required로 유지한다.
- heartbeat에 `quote_subscriptions`가 없는 구버전/전환 상태는 field 부재 자체만으로 전 종목을 stale시키지 않는다. `last_forwarded_tick_count`가 없는 구버전 Bridge와의 rolling transition도 parser 오류 없이 수용하며 이전 same-tick heuristic으로만 호환한다. 다만 B-01 인과관계 보정은 새 marker가 있는 Bridge에서 완전하게 보장되므로 backend와 native Bridge는 같은 릴리스로 함께 배포하는 것을 운영 기준으로 한다.

## 12.4 Futures / fixed market contracts

Bridge는 KOSPI200 선물의 월물/session route에 대해서는 Market AI 서버의 route 결과를 단일 기준으로 따른다.

- actual futures tick만 `FUTURES:KOSPI200` 갱신
- KOSPI는 `JUC_R/0001` contract 유지
- AUTO가 KOSPI200 canonical 운영
- fixed code는 emergency override
- CLOSED에서 futures tick 허용 금지
- 서버 예상 route와 다른 futures tick/heartbeat 거부
- heartbeat와 quote freshness 분리
- Bridge는 별도 x86 프로세스로 유지하고 자체 트레이 아이콘은 만들지 않는다
- Local Suite가 private window message로 Bridge 화면 열기/정상 종료를 요청한다

## 12.5 Web Monitor

`monitor/`는 `index.html + monitor.css + monitor.js` **3파일만** 유지한다.

- 로컬: `http://127.0.0.1:8001/monitor/`
- tailnet: `https://node.tail60a98e.ts.net/monitor/` → Tailscale Serve → 8002 GET-only proxy
- `/monitor`는 `/monitor/`로 정규화하여 상대 CSS/JS가 같은 origin에서 로드되게 한다.
- 10초 polling으로 read-only `/api/bridge/kis-efriend/quote-universe`를 조회하며 Dashboard `client_id` lease를 생성·연장하지 않는다.
- 표시값은 process-memory realtime 값을 우선하고, 장마감·재시작 복원에서만 durable snapshot을 fallback한다. active session에서 durable fallback만 남은 경우 backend의 explicit `stale`을 우선해 `지연`으로 표시하고, 새 realtime tick 전에는 `정상`/`시간외`로 승격하지 않는다.
- 보유종목 realtime `SC_R`는 현재가(`seq 2`), 전일대비 금액(`seq 4`), 등락률(`seq 5`)을 함께 전달한다. Web Monitor와 Native Bridge 보유종목 카드는 모두 등락률 오른쪽에 원본 `change_amount`를 같은 trend 색상으로 표시한다. `MarketSnapshot`에도 원본 `change_amount`를 함께 보존하고, `/api/bridge/kis-efriend/quote-universe`의 `monitor_snapshots`에도 그대로 전달하여 장마감 후 Market AI/Bridge 재시작에서도 최근 완료 KRX 거래일의 exact KIS durable snapshot으로 금액까지 복원한다. 과거 DB처럼 원본 `change_amount`가 실제로 없는 legacy snapshot은 퍼센트에서 역산하지 않고 금액 표시만 생략한다.
- `monitor.css`는 non-blocking으로 로드하고 `index.html`에는 첫 화면을 읽을 수 있는 최소 critical style만 둔다. `/monitor/`, `index.html`, `monitor.css`, `monitor.js` 응답은 `Cache-Control: no-store` 계열 헤더를 강제해 Monitor를 열 때마다 최신 정적 파일을 다시 읽게 한다.
- Desktop 보유종목 5열, 1100px 이하 3열, Phone 2열을 유지한다. Phone에서는 K200/KOSPI와 보유종목 모두 2열이며 420px 이하에서도 1열로 되돌리지 않는다.
- Phone 보유종목 카드는 종목코드를 숨기고, 상태 badge를 카드 우측 상단에 고정하며, 종목명은 최대 2줄까지 표시한다. Phone header의 시스템 상태는 상태 텍스트만 유지하고 실시간 clock과 status dot은 숨긴다.
- Phone shell의 화면 바깥 padding은 0으로 두고, 카드/grid 자식은 부모 폭을 밀어내지 않도록 축소 가능해야 한다. 긴 종목명·가격·상태는 카드 경계 밖으로 overflow하지 않는다.
- Web/Tablet은 viewport의 남는 높이 때문에 카드 row를 늘리지 않고 콘텐츠 자연 높이를 유지한다. iframe으로 열리면 부모 Dashboard가 compact modal 높이를 맞출 수 있도록 shell뿐 아니라 document 전체 높이를 전달한다. Print 전환에서도 높이를 다시 게시하고 `monitor.css`는 overflow/최소 page height를 해제·복원해 부모가 percentage height chain 없이 전체 내용을 인쇄할 수 있게 한다.
- embedded modal에서는 Dashboard의 외부 닫기 버튼과 Monitor theme toggle이 겹치지 않게 header tool 영역을 확보한다.

Monitor 정적 3파일만 수정한 운영 hotfix는 **MarketAI.exe를 재빌드하지 않고** 실행 중인 Market AI를 종료한 뒤 운영 `market-ai\_internal\monitor\`의 동일 3파일만 교체할 수 있다. 개발 Source of Truth인 `market-ai-dev/monitor/`에도 같은 변경을 남겨 다음 정식 build에 포함되게 한다. `app.py`, backend Python, build contract가 함께 바뀐 경우에는 이 fast-path를 쓰지 않고 `build-market-ai.ps1`로 전체 runtime 세트를 다시 만든다.

## 12.6 Native Bridge Monitor UI

Bridge 네이티브 모니터의 사용자 표시 제목은 `보유종목 실시간 시세`로 통일하고 운영 관찰용으로 단순화한다. 상단은 K200/KOSPI의 `상태·세션·시간·현재가·전일대비율`, 중간은 Dashboard 보유종목 lifecycle 집계, 하단은 기본 5열 동적 보유종목 카드와 `연결/마지막 수신`만 표시한다. 보유종목 카드의 등락 표시는 Web Monitor와 동일하게 `등락률 + 전일대비 금액`을 한 줄에 같은 색으로 표시한다.

- 보유종목 lifecycle은 `정상 / 시간외 / 장마감 / 대기 / 지연 / 오류`를 구분한다.
- 사용자 화면에는 `주간 / 야간 / 정규장 / 시간외 / 장마감` 같은 의미 라벨만 표시하고 `FC_R`, `CMEC_R`, `JUC_R`, `SC_R`, instrument code는 로그/API 진단 정보로만 유지한다.
- Signal baseline은 물리 stream에 남더라도 Dashboard가 실제 보유하지 않으면 카드/집계에 포함하지 않는다.
- 종목명은 Market AI의 `dashboard_names`를 사용하고 알 수 없는 ticker만 ticker 자체로 fallback한다.
- `business_time`은 실제 `HHMMSS` 범위만 유효하며, 장마감 재시작 복원은 최근 완료 KRX 거래일의 durable monitor snapshot만 사용한다.
- Native Bridge는 live `SC_R` 수신 시 `LastChangeAmountText`를 보존하고, 재시작 복원에서는 `monitor_snapshots.change_amount`를 같은 상태 필드로 seed한다. 원본 금액이 없는 legacy snapshot에서는 퍼센트만 표시하고 금액을 역산하지 않는다.

**UI Source of Truth는 Web Monitor의 현대식 카드 UI**다. Native WinForms는 같은 시각 언어를 따르되 렌더링 엔진과 responsive 책임은 분리한다.

Native UI 회귀 방지 contract:

- 보유종목 요약 Header와 `Holdings Card Grid`는 서로 다른 layout row를 사용하여 첫 카드 행을 덮지 않는다.
- rounded surface는 부모 배경을 정상 합성한 뒤 fill/border를 그려 검은 쐐기·클리핑을 만들지 않는다.
- `X`/`Alt+F4`는 `SC_CLOSE`를 가로채 즉시 Hide하고 실제 종료는 Local Suite 명령이 담당한다.
- Local Suite가 private window message로 기존 Bridge 창을 재표시하거나 정상 종료한다. Bridge 자체 `NotifyIcon`을 다시 만들지 않는다.

# 13. KOSPI200 AUTO 근월물 / KRX session

canonical 책임:

```text
bridges/kospi200_contract.py
```

책임:

- XKRX 거래일
- fallback calendar
- 일회성 open/closed/night override
- 분기월/근월물
- 실제 최종거래일 rollover
- day/night/CLOSED
- route code

C# Bridge에 별도 독립 월물 계산을 중복하지 않는다.

정확한 시간 경계는 최신 source를 Source of Truth로 한다.

---

# 14. Dashboard 연동 경계

Market AI backend가 제공하는 Dashboard용 정보:

- Market Snapshot. `/api/market-data/snapshot` 각 row는 `input_status`에 거래소 캘린더 기반 `status / available / session_open / session_close`를 함께 제공하며 Dashboard는 이를 브라우저의 요일·시간 추정보다 우선한다.
- Signal latest/details
- KIS eFriend Market Bridge status
- 현재 보유종목용 KRX quote universe / latest quote snapshot

현재 local/remote transport:

```text
Local Dashboard
→ http://127.0.0.1:8001

Remote GitHub Pages
→ https://node.tail60a98e.ts.net
→ Tailscale Serve
→ http://127.0.0.1:8002 (GET-only proxy)
→ http://127.0.0.1:8001
```

Market AI가 보장할 것:

- endpoint 데이터 의미와 HTTP/CORS contract
- Signal/market snapshot/Bridge status의 독립적 의미
- backend phase/effective weight/quality metadata
- KRX quote universe의 ticker identity, client lease, subscription health, quote `usable/state/source/observed_at`
- 특정 ticker 장애를 전체 quote 실패로 확대하지 않는 fail isolation

Dashboard 프로젝트가 소유할 것:

- Hero layout / Market AI 카드 / Mobile dialog
- `dashboard-view=web/tablet/mobile`
- CSS/responsive
- local/remote API base 선택과 frontend polling/latest-wins
- 기본적으로 KST 오늘에 live quote를 overlay하고, 다음 KRX 정규장 시작 전에는 최근 완료 거래일의 확정 `closed + usable` quote를 그 거래일 화면에만 이어서 적용하는 규칙
- 수량·원가·원금·매매흐름·실현손익과 전체 valuation math
- quote unusable 시 종목별 JSON fallback. 단 `market_state=closed`인 종목은 process-memory quote가 없어도 Bridge/subscription이 정상이고 fresh-tick 재확인 요구가 없으며 **최근 완료 KRX 거래일**의 exact KIS `MarketSnapshot`이 있으면 `closed + usable=true`로 복원한 뒤 JSON fallback을 생략
- Hero 제목행에는 raw `LIVE/CLOSED/STALE/WARMING/JSON` 상태 문자열을 노출하지 않는다. 실제 적용 가격에 따라 날짜·실시간·시간외·애프터 종가 기준을 표시하고, quote/fallback 상태는 내부 lifecycle 및 종목·상품 source tooltip에서 확인한다.
- live quote를 운영 JSON/history에 저장하지 않는 persistence boundary

외부 Dashboard에 예시 데이터를 제공하는 preview API/preview backend contract는 없다. Dashboard는 실제 Market AI API를 조회한다.

# 15. OpenAI 뉴스

선택 기능.

기본:

```text
MARKET_AI_AI_ENABLED=false
```

API Key가 없어도 Market AI 핵심 runtime 오류로 취급하지 않는다.

실제 Key는 `.env`에만 보관하고 source/ZIP/문서에 넣지 않는다.

현재 핵심 4개 Rule Signal에는 뉴스 score를 직접 섞지 않는다.

---

# 16. 운영 · 검증 문서 경계

현재 실행 가능 component, 원격 URL, 운영 프로세스와 사용자 확인 절차는 `market-ai-dev/README.md`가 소유하며, 이 handover에는 일시적인 상태표를 누적하지 않는다. 이 문서는 앞 절에서 정의한 architecture·runtime/session·build/deploy의 **장기 contract**를 소유하고, 실제 현재 동작 여부는 최신 source와 운영 Runtime으로 확인한다.

변경별 평가 강도·A/B/C·반례·100점 Gate와 자동/정적 QA·Windows 실기 선택 기준은 `market_ai_evaluation_guide.md`가 소유한다. 같은 회귀 체크리스트를 이 문서에 다시 복제하지 않는다.

# 17. 한 문장 원칙

> **Market AI는 eFriend/KIS/DB/Signal 계산과 write API를 로컬 Windows PC의 8001에 유지하고, Tailscale Serve에는 8002 GET-only proxy만 연결하여 외부 Dashboard가 조회 API만 사용하도록 경계를 분리한다.**
