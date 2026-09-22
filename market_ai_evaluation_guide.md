# market_ai_evaluation_guide · Market AI 평가 기준

> **문서 성격 / 평가 환경**  
> 이 문서는 `market-ai-dev`의 **Market AI / KIS eFriend Market Bridge / Investment Local Suite / Web Monitor / Dashboard 연동**을 평가할 때 사용하는 전용 기준서다.  
> 수정 방법이나 운영자의 실행 순서 자체를 설명하는 문서는 아니며, 실제 구조·빌드·배포 contract의 Source of Truth는 `market_ai_project_handover.md`를 따른다.  
> 평가는 현실적인 결함을 찾고 안정성을 확인하는 것이 목적이며, 이론적으로 가능한 모든 조합을 무한히 생성해 정상 구조를 계속 수정하는 것이 목적이 아니다.

이 문서는 사용자가 `점수`, `평가`, `평가해줘`, `수정할 거 찾아줘`, `수정해`라고 요청했을 때 다음을 일관되게 정의한다.

- 무엇을 평가하는가
- 어떤 근거로 A / B / C를 구분하는가
- 어떤 반례를 새로 만들어 검증하는가
- 무엇은 감점하지 않는가
- `수정해` 이후 어디까지 반복하고 언제 종료하는가
- source 평가와 Windows 재빌드·실기 QA를 어떻게 분리하는가

평가의 기본 원칙은 다음과 같다.

> **최신 실제 구현을 필요한 범위에서 독립적으로 확인하고, 실제로 재현 가능한 A/B급 문제는 숨기지 않는다.**

> **반대로 C급·취향성 개선·극저확률 다중 장애·이론적 가능성만 남았다면 수정 루프를 계속 열지 않는다.**

> **미해결 A/B가 0이고, 변경 영향 범위 QA가 통과하며, 마지막 bounded Counterexample Pass에서 새로운 A/B가 나오지 않으면 평가·수정을 종료하고 100점을 허용한다.**

---

# 1. 문서 역할 분리

| 문서/소스 | 역할 |
|---|---|
| `market_ai_project_handover.md` | 현재 운영 구조, 포트, API, 세션, build/deploy, 장기 contract |
| `market_ai_evaluation_guide.md` | 평가 범위, 점수, A/B/C, 반례, 감점·비감점, 종료 기준 |
| `monitor/` | Web Monitor 실제 UI/JS/CSS Source of Truth |
| `bridges/` | Dashboard holdings, KIS eFriend, KRX quote/session 계약 |
| `KisKospi200Bridge/` | x86 ActiveX Bridge 실제 C# 구현 |
| `start-local-server.pyw` | Investment Local Suite, 8000/8002, Tailscale Serve 경계 |
| `app.py` + 서비스 모듈 | FastAPI 8001 backend |
| `requirements*.txt`, `tools/run-tests.py`, build scripts | clean build / dependency / toolchain contract |
| `tests/` | 현재 명시된 contract의 회귀 안전망 |
| `investment-dashboard` 최신 snapshot | Dashboard ↔ Market AI 연동 계약 확인용 |
| 운영 `market-ai` | 재빌드 이후 실제 runtime 검증용, 초기 source 평가에 필수 아님 |

실제 코드와 문서가 다르면 문서를 근거로 코드를 자동으로 되돌리지 않는다.

```text
실제 구현 확인
→ handover의 장기 contract 확인
→ 코드 결함인지 문서 semantic drift인지 구분
→ 실제 영향에 따라 A/B/C 또는 비감점 판정
```

---

# 2. 평가 명령과 기본 출력

## 2.1 `점수`

`점수`, `점수만`은 최신 실제 소스를 필요한 범위에서 확인한 뒤 결과를 간결하게 제시한다.

기본 형식:

```text
Market AI 종합점수: XX/100

A급: N건
B급: N건
C급: N건 또는 비감점 관찰사항 N건
```

필요하면 영역 점수만 짧게 함께 제시한다.

- 과거 점수를 복사하지 않는다.
- 테스트 PASS 개수만으로 100점을 주지 않는다.
- 반대로 실제 A/B 감점 근거가 없는데 “완벽한 소프트웨어는 없으니 99”도 금지한다.

## 2.2 `평가` / `평가해줘`

두 표현은 같은 상세 평가 명령으로 처리한다.

```text
최신 실제 소스 확인
→ 범위/Source of Truth 확정
→ 현재 architecture / ownership inventory
→ 실제 contract와 테스트 확인
→ 위험도에 맞는 상태/경계/실패 가설 생성
→ 영역별 평가
→ A/B/C 분류
→ bounded Counterexample Pass
→ 점수와 최종 결론
```

평가에서는 원칙적으로 파일을 수정하지 않는다.

전체 평가의 기본 영역:

1. Backend / API / 서비스 wiring
2. KRX Quote / ticker admission / lease / bootstrap
3. KIS eFriend Market Bridge / market session
4. Investment Local Suite / Tailscale / 8002 GET-only boundary
5. Web Monitor
6. Signal / market data / DB / persistence
7. Build / dependency / PyInstaller / deployment
8. Dashboard ↔ Market AI contract
9. README / handover / source comment 의미 정합성

사용자가 특정 범위를 지정하면 해당 영역을 중심으로 평가한다.

## 2.3 `수정할 거 찾아줘`

점수보다 **실제 A/B 후보 탐색**이 우선이다.

- 이미 안정화된 영역을 매번 처음부터 전수평가하지 않는다.
- 최근 변경 파일과 직접 dependency를 먼저 본다.
- 실제 사용자·운영 영향이 없는 미세 정리는 C 또는 비감점으로 둔다.

## 2.4 `수정해`

기본 의미:

```text
현재 미해결 A/B 수정
→ 변경 영향 범위 회귀 QA
→ 위험도에 맞는 대표 반례 재검토
→ 새로운 A/B가 있으면 추가 수정
→ A/B = 0
→ 마지막 bounded Counterexample Pass 1회
→ 새 A/B 없음
→ 종료
```

- **A는 반드시 수정**
- **B는 원칙적으로 수정**
- **C는 기본 수정 대상 아님**
- 사용자가 `C까지 수정해`, `이 UI 정리도 같이 해`처럼 명시한 경우에만 C를 수정한다.
- C를 수정하다 회귀 위험이 커지면 유지가 더 나은 선택일 수 있다.
- 마지막 반증 평가에서 C나 이론적 가능성만 새로 나오면 수정 루프를 다시 시작하지 않는다.

---

# 3. 평가 Source of Truth

평가 시 우선순위는 다음과 같다.

| 확인 대상 | Source of Truth |
|---|---|
| 현재 Python backend | 최신 `market-ai-dev` 실제 `.py/.pyw` |
| KIS Bridge | `KisKospi200Bridge/*.cs`, 실제 API schema |
| KRX quote / lease | `bridges/krx_quotes.py`, `dashboard_holdings.py`, `app.py` |
| Signal | `signals/engine.py`, `signals/service.py` |
| DB/migration | `db/*.py`, 현재 schema/migration |
| Web Monitor | `monitor/index.html`, `monitor.css`, `monitor.js` |
| Tailscale/8002 | `start-local-server.pyw` |
| build | `build-*.ps1`, `.bat`, `requirements*lock.txt`, `.csproj` |
| 장기 설계 의도 | `market_ai_project_handover.md` |
| Dashboard consumer | 사용자가 제공한 최신 `investment-dashboard` 관련 adapter/client |
| 실제 EXE 동작 | 재빌드 후 운영 PC runtime / 새 `market-ai` |

초기 source 평가 단계에서는 `market-ai.zip` 운영본이 없어도 된다.

실제 EXE binary, eFriend ActiveX, Tailscale Serve, Windows service 상태는 **재빌드 이후 실기 QA 범위**다.

---

# 4. 평가 철학과 보호 규칙

## 4.1 현실적인 A/B만 찾는다

평가자는 다음 질문을 우선한다.

> **“실제 Windows 운영 환경 또는 일반적인 사용자 행동에서 이 문제가 현실적으로 도달 가능한가?”**

> **“발생하면 데이터·시장상태·보안경계·사용자 판단·서비스 가용성에 실질 영향이 있는가?”**

실제 근거 예:

- 재현 가능한 bug
- 반복 가능한 market-session 오판
- remote/local contract 파손
- 실제 stale/race
- 정상 재시작에서 복구 실패
- local Dashboard가 remote client 때문에 밀려남
- write endpoint가 remote에 노출됨
- Web Monitor가 실제로 열리지 않음
- 유효하지 않은 KIS 시간이 사용자 화면에 실제 시간처럼 노출됨
- build가 clean machine에서 재현되지 않음
- build smoke가 운영 DB를 실제로 변경함
- 문서가 운영자를 잘못된 배포/재빌드로 유도함

## 4.2 다음은 그 자체로 감점하지 않는다

아래는 **실제 문제가 확인되지 않는 한** A/B를 만들지 않는다.

- Python / C# / JS 파일 길이 자체
- 함수가 크다는 이유만의 리팩토링
- framework 미사용
- 테스트 개수 자체
- 테스트가 없다는 사실 자체
- CSS/JS 파일 개수 자체
- dependency artifact의 모든 wheel SHA-256 미고정 자체
- exact version lock이 있는데도 “이론적으로 PyPI artifact가 바뀔 수 있음”만으로 B 부여
- 모든 HTTP endpoint에 rate limit이 없다는 이유만으로 B 부여
- 모든 가능한 TCP idle/flood를 방어하지 않는다는 이유만으로 B 부여
- 모든 로그에 TR/service code가 남아 있다는 이유
- 운영 DB의 개발용 복사본이 source tree에 있다는 사실 자체
- `market-ai.zip`이 초기 source 평가에 없다는 사실
- Visual Studio/MSBuild patch 버전까지 완전 동일하지 않다는 이유만의 감점
- 모든 다중 장애 조합을 atomic하게 복구하지 않는다는 이론적 가능성
- 현재 지원 환경에서 발생 경로가 거의 없고 영향도 미미한 edge case
- C급 관찰사항이 남아 있다는 사실

## 4.3 빌드 재현성의 현실적 기준

Build는 다음이 실제로 충족되면 높은 점수를 줄 수 있다.

- 지원 Python major/minor/x64 기준 명확
- clean temporary venv 사용
- runtime/build dependency exact version lock
- global site-packages 비의존
- `--no-deps` / 필요 시 `--no-build-isolation`
- `pip check`
- 설치 버전 audit
- PyInstaller 버전 고정
- Windows PowerShell 5.1에서 실제 script 문법/quoting이 안전
- KIS Bridge의 지원 VS/MSBuild baseline 명확
- clean build smoke가 성공
- source와 build output 사이 prerequisite가 문서와 일치

모든 artifact hash까지 고정하는 것은 **선택적 강화**다.

실제 공급망 요구사항이나 반복적인 artifact drift가 확인되지 않았다면:
- C 또는 비감점 관찰사항 가능
- A/B로 승격하지 않는다
- 100점을 막지 않는다

## 4.4 테스트와 평가를 분리한다

```text
테스트 PASS
→ 현재 테스트가 아는 contract는 만족
→ 평가 종료 아님

테스트 FAIL
→ 실제 production defect인지 확인
→ 낡은 테스트인지 확인
→ 실제 결함만 A/B에 반영
```

테스트를 늘렸다는 이유로 점수를 올리지 않는다.

---

# 5. A / B / C 판정

## 5.1 A — 즉시 수정해야 하는 중대한 문제

A는 데이터·보안·핵심 기능·시장판정·운영 결과에 중대한 영향을 주는 구체적인 문제다.

대표 예:

- 8001 full API가 Tailscale/LAN에 직접 노출
- remote에서 write endpoint 실행 가능
- 잘못된 quote가 역사 데이터/장부 DB를 오염
- Signal 계산이 잘못되어 반대 방향 판단을 생성
- ticker admission 실패가 기존 local lease/state를 손상
- durable state 손상 또는 복구 불능
- 주요 backend 전체 startup 불능
- KIS route 오류로 잘못된 월물/세션을 실제 값처럼 제공
- build/deploy 절차가 운영 DB를 손상
- frontend-backend 핵심 schema 파손으로 주요 기능 사용 불가

가능하면 다음을 함께 기록한다.

```text
파일/함수/API
재현 조건
현재 상태
실제 영향
왜 A인지
수정 방향
회귀 테스트 후보
```

## 5.2 B — 현실적으로 발생 가능한 수정 권장 문제

B는 대부분 정상이어도 현실적인 특정 순서·시간·경계·복원에서 실질 오류 또는 운영 혼란이 생기는 문제다.

대표 예:

- 15:30 이후 개별주식을 실제 애프터마켓 중인데 `장마감` 표시
- `/monitor/`가 8002 allowlist 누락으로 폰에서 실제 사용 불가
- KIS `888888`을 `88:88:88` 실제 시각처럼 표시
- remote lease가 local Dashboard capacity를 밀어냄
- bootstrap이 remote request 때문에 오염
- subscription 장애 후 새 tick 없이 old quote가 usable로 부활
- heartbeat 공백 후 재연결에서 새 SC_R tick 없이 장애 전 process-memory quote가 live/closed로 부활
- heartbeat stale 시간보다 빠른 Bridge/stream 재시작에서 subscription counter가 reset됐는데도 old quote가 계속 usable
- 서로 독립 전송되는 SC_R tick과 heartbeat의 도착 순서가 뒤집혔을 때, 과거 unhealthy heartbeat가 서버가 이미 수락한 최신 tick을 다시 stale/fresh-required로 덮음
- 동일 client/ticker 변경에서 old quote가 남음
- restart 직후 현실적인 bootstrap 순서에서 stale state
- source/build script 차이 때문에 일반 clean build가 실패
- 문서와 실제 배포 세트가 달라 운영자가 잘못 복붙할 가능성이 큼
- local/remote mixed path에서 실제 CORS 또는 endpoint 실패
- 8002 GET-only에서 필요한 read endpoint/monitor asset 누락
- 실제 UI에서 내부 debug code가 사용자를 혼란시키는 수준으로 노출

B는 다음 조건을 만족할 때만 부여한다.

- 지원 환경에서 현실적으로 도달 가능
- 코드 흐름상 발생 경로가 명확
- 사용자/데이터/운영 영향이 실질적

## 5.3 C — 비감점 관찰사항 / 선택 개선

C는 수정 이득이 작거나 trade-off인 항목이다.

예:

- 모든 dependency artifact hash 추가 고정
- 진단 로그의 내부 TR code 유지
- build script의 추가 미세 정리
- 지금도 정상인 UI 텍스트를 더 예쁘게 다듬는 것
- 극저확률 다중 장애
- 모든 가능한 flood/resource exhaustion까지 방어
- 현재 운영 리스크가 매우 작은 유지보수 개선
- 구조상 정상인데 “더 현대적”인 대안이 있는 경우

C는:
- 감점하지 않음
- 미해결 결함으로 계산하지 않음
- 100점 판정을 막지 않음
- 사용자가 명시하지 않으면 수정하지 않음

---

# 6. Market AI 종합점수 /100

전체 평가 시 아래 영역을 기준으로 점수를 산정한다.

| 영역 | 기본 비중 | 핵심 질문 |
|---|---:|---|
| Backend / API / service wiring | 14 | startup, endpoint, validation, failure isolation이 안정적인가 |
| KRX Quote / admission / lease / bootstrap | 18 | local priority, capacity, stale quote, restart가 안전한가 |
| KIS Bridge / market session | 16 | 실제 KIS route/session/time/stream 의미가 맞는가 |
| Local Suite / Tailscale / security boundary | 14 | 8000/8001/8002와 remote GET-only 경계가 정확한가 |
| Signal / market data / DB | 10 | signal 의미, persistence, history와 realtime 경계가 맞는가 |
| Web Monitor / Native Monitor UI | 8 | 사용자에게 실제 상태를 정확하고 안정적으로 표시하는가 |
| Build / dependency / deployment | 12 | clean build와 올바른 재빌드/배포가 재현 가능한가 |
| Dashboard 연동 / docs / maintenance contract | 8 | consumer contract와 문서가 실제 구현과 맞는가 |

범위가 좁으면 N/A 영역은 합리적으로 재배분한다.

```text
구체적인 A/B 감점 근거 있음 → 감점
A/B 없음 → 100 가능
C만 있음 → 감점 없음
```

---

# 7. 평가 Workflow — 위험도·변경 영향 기반

평가 강도는 요청 범위와 위험도에 비례한다.

## 7.1 전체 6 Pass를 수행하는 경우

- 새 Source of Truth의 최초 전체 평가
- backend / lease / session / Tailscale 같은 shared contract 대규모 변경
- 데이터 손상·remote write·ticker admission 등 고위험 로직 변경
- 최종 릴리스 평가를 명시적으로 요청
- 기존 A급 수정 직후 관련 범위 전체 반증이 필요한 경우

## 7.2 제한된 수정의 재평가

방금 전체 평가를 마치고 특정 파일만 수정한 경우:

```text
변경 파일
→ 직접 dependency
→ 영향받는 contract
→ 핵심 regression
→ 대표 반례 1~3개
→ 문서/테스트 정합성
→ bounded Counterexample Pass 1회
→ 새 A/B 없으면 종료
```

안정된 영역을 매번 처음부터 다시 공격하지 않는다.

## 7.3 6 Pass

### Pass 1 — 구조 / ownership

- 실제 entrypoint
- 8000 / 8001 / 8002
- service wiring
- KIS Bridge process
- monitor assets
- build scripts
- tests
- DB / config
- Dashboard consumer
- 문서와 실제 구조 drift

### Pass 2 — 상태 모델

주요 state owner:

- MarketAI backend lifecycle
- KIS heartbeat / tick
- quote store
- subscription health
- ticker universe
- local/remote client lease
- bootstrap
- Tailscale Serve
- Web Monitor polling
- Dashboard polling
- build/deploy set

### Pass 3 — 현실적 adversarial scenario

위험도에 따라 대표 반례만 만든다.

- 저위험: 1개 이상
- 중간위험: 2~3개
- 고위험: 3~5개

숫자를 채우기 위한 반복은 금지한다.

### Pass 4 — 경계 / async / restart

- 시간 경계
- lease 만료
- max ticker
- concurrent local/remote
- restart
- stale heartbeat
- subscription unhealthy→recovery
- response loss
- monitor polling
- build smoke

### Pass 5 — 코드 ↔ API ↔ UI ↔ 문서

- API schema
- status label
- session label
- README/handover
- build/deploy 설명
- Dashboard adapter
- test가 주장하는 contract

### Pass 6 — bounded Counterexample

마지막 질문:

> **“지금 100점을 주면 틀렸다고 반박할 수 있는 현실적인 A/B급 반례가 하나 더 있는가?”**

새 A/B가 없으면 종료한다.

C나 이론적 가능성만 나오면 더 확장하지 않는다.

---

# 8. 영역별 평가 체크포인트

이 절은 handover의 제품 contract를 다시 설명하지 않는다. **어떤 실패를 평가에서 찾아야 하는지**만 정리한다. 세부 상수·endpoint·세션·build 순서는 `market_ai_project_handover.md`와 최신 source를 따른다.

| 평가영역 | 우선 확인할 실패 | 대표 근거 |
|---|---|---|
| Backend / API | startup 불능, validation 누락, subsystem 실패 전파, schema 파손 | `app.py`, 서비스 모듈 |
| KRX Quote / lease | admission atomicity, local priority, capacity, lease expiry, stale/old quote 부활 | `bridges/krx_quotes.py`, `dashboard_holdings.py` |
| KIS Bridge / session | 잘못된 route/session/time, subscription 장애 전파, fresh tick 없이 usable 복귀 | C# Bridge + `bridges/` |
| Local Suite / Tailscale | direct `:8001` 노출, 8002 write 통과, unsafe Serve 복구, unrelated process 종료 | `start-local-server.pyw` |
| Signal / market data / DB | source 의미 오류, stale 오판, persistence 오염, no-lookahead 위반 | `signals/`, `market/`, `db/` |
| Monitor UI | 내부 code 노출, 상태/시간 오표시, read-only 경계 파손, 실제 viewport overflow | `monitor/`, Native Bridge UI |
| Build / deploy | clean build 실패, mutable state 덮어쓰기, mixed set, rollback/verify 오류 | build scripts + `tools/` |
| Dashboard 연동 | backend 의미 재해석, live quote 영속화, 과거 날짜 overlay, stale response 덮어쓰기 | 최신 Dashboard consumer |
| Docs | 현재 source와 다른 운영·재빌드 지시 | README + handover |

평가자는 구현 모양보다 **사용자·운영 결과**를 본다. 같은 결과와 경계를 유지하는 정상 리팩터링은 결함이 아니다.

---

# 9. 고위험 상태·경계 시나리오

전체 평가나 고위험 변경에서는 아래에서 관련 있는 대표 시나리오만 선택한다. 이미 같은 원인을 검증한 반례를 표현만 바꿔 반복하지 않는다.

## 9.1 Quote / admission / lease

- 신규 remote request가 capacity를 먼저 차지한 뒤 local Dashboard가 들어오는 경우
- client universe가 줄었다가 다시 늘어나는 경우
- failed admission이 기존 lease/universe를 바꾸는지
- lease expiry와 restart bootstrap이 겹치는 경우
- local 복수 탭과 remote client가 동시에 universe를 갱신하는 경우
- subscription unhealthy → healthy 전환에서 새 실제 tick 전 old/durable quote가 usable로 부활하는지

핵심 판정:

```text
요청 실패 → 기존 정상 state 보존
remote pressure → local Dashboard 우선
universe 제거 → old quote 폐기
재편입 → fresh lifecycle 재시작
```

## 9.2 Market session / business time

세부 시간표는 handover/source를 복제하지 않는다. 다음 **경계 의미**가 실제 구현과 맞는지만 본다.

- KRX 정규장 직전/종료 시점
- 개별주식 시간외 구간과 ETF/KOSPI 장마감의 차이
- K200 day/night/CLOSED route
- 휴장일·주말·override
- invalid `business_time`이 실제 시각처럼 표시되지 않는지
- 장마감 재시작에서 허용된 최근 완료 거래일 snapshot만 `closed` 의미로 복구되는지
- 다음 정규장 시작 뒤 전일 snapshot이 current quote로 부활하지 않는지

## 9.3 Remote boundary

반드시 보안·소유권 결과를 확인한다.

```text
localhost full API 정상
remote GET-only 정상
remote write 차단
allowlist 밖 GET 차단
unsafe direct full-API mapping fail-closed
Tailscale 실패가 local runtime 전체 실패로 확대되지 않음
```

숫자·URL·허용 endpoint 목록은 handover/source를 따른다.

## 9.4 Restart / recovery

대표 순서:

```text
정상 종료 → 재시작
MarketAI 먼저 / Bridge 늦게
Dashboard 미접속 상태 bootstrap
remote 먼저 접속
lease 만료 후 재접속
subscription 재구축
```

확인할 것은 **old state가 정상값으로 부활하는지**, **정상 durable closed state는 필요한 범위에서 복구되는지**, **remote가 local/bootstrap 소유권을 침범하는지**다.

---

# 10. Build / deploy 평가

Build는 파일 존재보다 **재현성과 운영 안전성**을 평가한다.

필수 질문:

- clean source에서 지원 toolchain으로 재현 가능한가
- exact dependency/tool version contract가 실제 build script와 맞는가
- staging 검증 뒤에만 sibling runtime을 교체하는가
- runtime stop이 owned process만 대상으로 하고 eFriend 유지 모드를 지키는가
- 무관한 port owner를 강제 종료하지 않는가
- runtime component가 clean replacement 되는가
- deploy verify와 rollback이 old/new 혼합 상태를 방지하는가
- 운영 DB·`.env`·README 등 mutable/non-component 자원을 덮어쓰지 않는가
- 최종 SUCCESS가 deploy/verify/clean-dev보다 먼저 나오지 않는가
- 정상 build 뒤 dev root에 재생성 가능한 runtime/bin/obj residue가 contract 위반으로 남지 않는가

변경 대상별 **어떤 build가 필요한지**는 handover의 build matrix를 따른다. 평가 문서에서 그 표를 다시 복제하지 않는다.

Monitor 정적 파일만의 fast-path처럼 handover가 명시적으로 허용한 예외는 정상 경로다. “항상 모든 component를 재빌드해야 더 안전하다”는 이유로 감점하지 않는다.

---

# 11. Dashboard ↔ Market AI 평가 경계

Dashboard는 Market AI 평가에서 **consumer contract 확인 범위**만 본다. Dashboard 자체 CSS·계산·GAS 품질을 Market AI 점수에 중복 합산하지 않는다.

확인:

- local/remote transport가 handover의 경계를 따르는가
- remote frontend가 write 경로를 갖지 않는가
- backend `usable/state/market_state/source/observed_at` 의미를 frontend가 임의 재계산하지 않는가
- KST 오늘 및 handover가 허용한 제한된 직전 완료 거래일 범위 밖에 Market AI quote를 덮지 않는가
- unusable ticker는 종목별 저장 JSON fallback을 유지하는가
- ticker universe 변경 중 이전 응답이 최신 state를 덮지 않는가
- Market AI quote가 `prices.json`, 성과 snapshot, Pension JSON, GAS write에 영속화되지 않는가
- Signal panel과 live valuation의 failure가 서로 불필요하게 전파되지 않는가

Dashboard 계약이 바뀌지 않은 Market AI 내부 수정이라면 Dashboard 전체 회귀 QA를 확대하지 않는다.

---

# 12. Monitor / 사용자 상태 표현 평가

Web Monitor와 Native Bridge Monitor는 **운영 관찰면**이다. 내부 service/TR code가 아니라 사용자가 이해할 수 있는 상태를 정확히 보여주는지를 본다.

확인:

- Web Monitor가 read-only이며 Dashboard client lease를 만들거나 연장하지 않는가
- process-memory 최신값과 durable fallback의 의미가 화면에서 뒤섞이지 않는가
- 상태·세션·시간·가격·등락 정보가 실제 source 의미와 맞는가
- invalid time이나 stale 값이 정상 live 값처럼 보이지 않는가
- Phone/Web/Tablet의 핵심 responsive contract가 실제 viewport에서 깨지지 않는가
- embedded Monitor가 Dashboard modal과 충돌하지 않는가
- Native Bridge의 Hide/reopen lifecycle이 실제 process 종료와 혼동되지 않는가

CSS 속성명, Win32 상수값, DOM/C# 구현 순서처럼 사용자 결과와 무관한 세부는 **그 자체로 평가 contract가 아니다.** 실제 UI/수명주기 결과가 깨지는 경우에만 A/B를 부여한다.

---

# 13. 변경 영향 회귀 범위

테스트 개수를 채우는 것이 아니라 변경된 의미를 보호한다.

| 변경 | 기본 회귀 범위 |
|---|---|
| Backend/API | syntax/import, 관련 pytest, endpoint/schema consumer, health |
| KRX quote/lease | admission, priority/capacity, expiry/bootstrap, subscription, 대표 concurrency |
| Session/time | 관련 경계시각, 휴장일, instrument별 상태 차이 |
| Tailscale/proxy | local health, remote read, write 차단, allowlist, fail-closed |
| Web Monitor | JS syntax, polling/status/time mapping, responsive/embedded, remote asset |
| C# Bridge | build, route/stream, dynamic holdings, business time, Hide/reopen |
| Build/deploy | clean build, stop/deploy/verify/rollback, mutable state, clean-dev |
| Docs only | 링크·상호 참조·source semantic 정합성; 불필요한 runtime rebuild 강제 금지 |

이미 안정된 영역은 직접 dependency가 없는 한 매번 전체 재평가하지 않는다.

---

# 14. Clean-room / Windows 실기

## 14.1 Clean-room

다음 경우에 권장한다.

- shared backend/build contract 대규모 변경
- dependency/build script 변경
- final release
- source tree residue 의존 의심
- 여러 차수 수정 뒤 최종 패키징

원본 Source of Truth를 새 위치에 풀고 최종 수정 파일만 적용해 관련 QA를 다시 실행한다. 작은 문서·CSS 수정마다 전체 clean-room을 강제하지 않는다.

## 14.2 Source-test 환경

정식 source QA는 플랫폼 공통 test 환경을 먼저 준비한다.

```text
python -m pip install -r requirements-test.txt
python tools/run-tests.py -q
```

`tools/run-tests.py`가 dependency 누락을 보고하면 제품 코드 FAIL로 채점하지 않고 환경을 먼저 완성한다. `yfinance` 같은 runtime dependency를 fake module/stub으로 대체해 전체 PASS를 주장하지 않는다. Windows frozen runtime의 exact lock은 build contract가 별도로 소유한다.

## 14.3 Windows 실기

source 평가와 runtime 실기는 구분한다. 실제 EXE/eFriend/Tailscale/Windows 권한 동작은 필요한 변경에서만 운영 PC에서 확인한다.

대표 항목:

- 8000/8001/8002 ownership과 health
- remote GET-only / write 차단
- eFriend Bridge 실시간 tick과 session 전환
- Web Monitor local/remote
- Dashboard live valuation
- build/deploy 변경 시 runtime stop → deploy → SUCCESS → 재기동

실기를 못한 경우 source 기준 점수는 낼 수 있으나 **실제 runtime까지 확인했다고 표현하지 않는다.**

---

# 15. 문서 평가

문서 평가는 분량이 아니라 **역할과 잘못된 운영 유도 가능성**을 본다.

역할:

```text
market-ai-dev/README.md
→ 운영 실행 / 상태 확인 / 필요한 build 선택 / 데이터 보존

market_ai_project_handover.md
→ architecture / ownership / runtime·session / build·deploy 장기 contract

market_ai_evaluation_guide.md
→ 평가 범위 / 점수 / A·B·C / 반례 / 종료 기준
```

감점 후보:

- 실제 source와 다른 포트·접근 경계
- 잘못된 재빌드/배포 지시
- mutable DB를 build artifact처럼 취급
- session/quote 의미를 잘못 설명해 사용자 판단을 왜곡
- 서로 다른 문서가 같은 contract를 각각 소유해 현재값이 충돌

단순한 표현 차이·길이·스타일은 감점하지 않는다.

---

# 16. 100점 Gate

현재 평가 범위와 위험도에 해당하는 항목만 확인한다.

```text
[ ] 최신 Source of Truth를 확인했는가
[ ] 미해결 A = 0인가
[ ] 미해결 B = 0인가
[ ] 변경 영향 범위 QA가 PASS했는가
[ ] 고위험 변경이면 관련 state/race/restart 반례를 확인했는가
[ ] local/remote 보안 경계가 현재 contract와 맞는가
[ ] session/time/quote 의미가 API와 사용자 화면에서 일치하는가
[ ] build/deploy 변경이면 clean build·mutable state·rollback 경계를 확인했는가
[ ] Dashboard consumer contract가 깨지지 않았는가
[ ] 문서가 실제 구조를 잘못 안내하지 않는가
[ ] 마지막 bounded Counterexample Pass에서 새 A/B가 없는가
```

C가 남아 있어도 100점 가능하다.

---

# 17. Bounded Counterexample Pass

점수 확정 직전 **현재 변경과 관련된 대표 질문만** 다시 본다.

예:

```text
remote가 capacity를 먼저 잡으면 local은?
failed admission이 기존 정상 state를 바꾸면?
restart 뒤 old quote가 usable로 부활하면?
subscription 복구 뒤 새 tick 없이 stale/durable 값이 정상화되면?
장마감 복원 범위를 벗어난 snapshot이 current quote가 되면?
instrument별 session 경계가 뒤바뀌면?
8002에서 write 또는 allowlist 밖 GET이 통과하면?
unsafe direct 8001 Serve가 남으면?
build/test가 운영 DB를 바꾸면?
runtime stop이 unrelated process/eFriend를 종료하면?
deploy 실패인데 mixed set 또는 SUCCESS가 남으면?
Dashboard가 허용 범위 밖 과거 날짜에 realtime을 덮으면?
```

새 현실적 A/B가 없으면 종료한다. 같은 위험 패턴을 표현만 바꿔 반복하지 않는다.

---

# 18. 평가 결과 작성 형식

기본:

```text
Market AI 종합점수: XX/100
A급: N건
B급: N건
C급/비감점 관찰사항: N건
```

영역별 표를 사용할 경우 점수·상태·핵심 근거·감점 여부를 함께 적는다.

A/B에는 최소 다음을 포함한다.

- 정확한 문제
- 발생 조건
- 실제 영향
- 왜 A/B인지
- 수정 방향

100점이라면 마지막 bounded Counterexample Pass에서 새 A/B가 없었다는 근거를 적는다. C는 왜 감점하지 않는지만 짧게 설명한다.

---

# 19. 평가·수정 종료 조건

```text
[ ] A = 0
[ ] B = 0
[ ] 영향 범위 QA PASS
[ ] 고위험 변경이면 관련 async/restart/persistence 반례 확인
[ ] 마지막 bounded Counterexample Pass 1회에서 새 A/B 없음
```

이후 C·취향성 refactor·극저확률 다중 장애·이론적 가능성만으로 수정 루프를 다시 열지 않는다.

재평가를 여는 근거는 새로운 사용자 보고, 로그/실패 증거, 코드 또는 외부 contract 변경, runtime 환경 변경, 사용자의 명시적 전체 재평가 요청이다.

---

# 20. 최종 원칙

평가 우선순위는 다음과 같다.

1. 보안 경계
2. 시장/session/quote 의미 정확성
3. local priority / admission / lease
4. realtime / stale / subscription health
5. restart / bootstrap
6. persistence boundary
7. Dashboard consumer
8. build/deploy 재현성과 운영 안전성
9. Monitor UI 정확성
10. 문서·유지보수성
11. 성능

점수는 결과를 표현하는 보조 지표다. 점수를 올리기 위해 정상 구조를 계속 뜯거나 테스트 개수를 늘리지 않는다.

> **실제 Windows 운영에서 현실적으로 발생 가능한 A/B급 결함을 찾고, A/B가 0이며 필요한 QA와 bounded Counterexample Pass가 끝났다면 정상 구조를 더 수정하지 않고 종료한다.**

