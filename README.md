# Market AI

`market-ai-dev`가 개발·빌드·운영 문서의 Source of Truth다. 형제 `../market-ai`는 Git 저장소가 아니라 **실행 Runtime 폴더**로만 사용하며 `README.md`가 없어도 된다.

## 폴더 역할

```text
parent/
├─ investment-dashboard/
├─ market-ai-dev/   # 소스, 테스트, 빌드/배포 도구
└─ market-ai/       # EXE와 runtime state만 보관
```

운영 포트는 Dashboard `8000`, Market AI local API `8001`, remote GET-only proxy `8002`다. Web Monitor는 `/monitor/`이며 Dashboard의 **보유종목 실시간 시세**에서 embedded로 연다. 화면 조회는 응답 완료 후 5초 간격으로 실행하며, 숨김 상태에서는 중단한다. 모니터 polling 실행 검증: `node --test tests/monitor-polling.test.cjs`.

## Source 테스트

```powershell
python -m pip install -r .\requirements-test.txt
python .\tools\run-tests.py -q
```

`requirements-test.txt`는 플랫폼 공통 `requirements.txt`에 pytest만 추가한다. Windows frozen runtime의 exact dependency는 `requirements-lock.txt`와 `requirements-build-lock.txt`가 별도로 소유한다.

## 빌드

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-market-ai.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-investment-local-suite.ps1
.\build-kis-bridge-release.bat
```

각 빌드는 검증된 staging 결과만 sibling `../market-ai`에 배포한다. `market-ai-dev`와 `market-ai`는 같은 부모 아래에 있어야 하며 이름도 그대로 유지한다. **Runtime `market-ai/README.md`는 build marker가 아니다.**

- `app.py`/backend Python 변경 → `build-market-ai.ps1`
- `start-local-server.pyw` 변경 → `build-investment-local-suite.ps1`
- `KisKospi200Bridge` C# 변경 → `build-kis-bridge-release.bat`
- `monitor/index.html`, `monitor.css`, `monitor.js`만 변경 → 정적 파일 hotfix 가능; backend/packaging 변경이 함께 있으면 Market AI를 재빌드한다.

빌드 후 runtime은 자동 재시작하지 않는다. 필요한 빌드를 모두 끝낸 뒤 평소 방식으로 `InvestmentLocalSuite.exe`를 한 번 실행한다.

## 문서

- `README.md`: 실행·테스트·빌드 진입점
- `market_ai_project_handover.md`: architecture, ownership, runtime/session, build/deploy 장기 contract
- `market_ai_evaluation_guide.md`: 평가 범위, A/B/C, 반례 및 종료 기준

세부 운영·보안·Tailscale·KIS Bridge 계약은 `market_ai_project_handover.md`를 따른다.
