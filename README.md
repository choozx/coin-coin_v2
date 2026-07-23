# auto_trading

암호화폐 **레버리지(바이낸스 USDⓈ-M 선물)** 자동매매 시스템.
유저가 **매매기법을 프리셋(블록 조립)** 으로 만들어 백테스트 → 페이퍼 → 실거래로 굴린다.

## 현재 상태

| 레이어 | 상태 |
|--------|------|
| 프리셋 스키마 (블록 조립의 직렬화) | ✅ v1 (`schema/preset.schema.json`) |
| 백테스트 엔진 (청산·펀딩·수수료 반영) | ✅ 동작 (`engine/`) |
| 실데이터 수집 (바이낸스 공개 klines, 키 불필요) | ✅ `engine/binance_data.py` |
| 로컬 캔들 캐시 (SQLite, 증분 수집) | ✅ `engine/candle_store.py` |
| 독립 캔들 수집기 (1회/반복) | ✅ `engine/collector.py` |
| GUI 백테스트 스튜디오 (파라미터 튜닝) | ✅ `engine/server.py` + `gui.html` |
| 파라미터 최적화 (그리드+IS/OOS 검증) | ✅ `engine/optimize.py` |
| 페이퍼 트레이딩 (실시간 클럭 + 같은 엔진) | ✅ `engine/live.py` + `PaperExecutor` |
| 매매 원장 (청산거래 영구기록·잔고복원) | ✅ `engine/ledger.py` (`data/trades.db`) |
| 매매 대시보드 (모니터 + 멈춤/재개) | ✅ `engine/dashboard.py` + `control.py` |
| 데이터·수집기 관리 페이지 (`/collector`) | ✅ `collector.html` (심볼 핫리로드) |
| 리스크 가드레일 (일일손실·연속손실·킬스위치) | ✅ `engine/settings.py` + `live.py` |
| 배포 (도커 멀티서비스 + EC2 풀 배포) | ✅ `docker-compose.yml` + [`docs/deploy.md`](docs/deploy.md) |
| 실거래 어댑터 (ccxt) | ✅ 주문·체결·동기화 (`engine/binance_broker.py` + `LiveExecutor`) — 테스트넷부터 |
| 지정가 체결 모델 (미체결·슬리피지) | 🟡 진입은 passive-then-aggressive. **백테스트의 청산**은 아직 종가 체결 가정 — 아래 [다음 할 일](#다음-할-일) |
| 블록 빌더 UI | ⬜ 예정 |

## 문서

- [`DESIGN.md`](DESIGN.md) — 전체 시스템 설계
- [`docs/binance-formulas.md`](docs/binance-formulas.md) — 청산가·펀딩비·증거금·수수료 공식(바이낸스 공식)
- [`docs/data-source.md`](docs/data-source.md) — 캔들 데이터(candle-collector) 스키마 분석

## 빠른 시작

```bash
# ★ 지표 계산은 TA-Lib(검증된 C 라이브러리)에 위임 — C 라이브러리부터 설치
brew install ta-lib            # macOS (Ubuntu: apt-get install ta-lib)
pip install -r requirements.txt

# ★ GUI 백테스트 스튜디오 (브라우저에서 값 조절 → 실데이터 백테스트)
python3 -m engine.server         # → http://localhost:8765

# CLI 백테스트 (바이낸스 실데이터 14일, API 키 불필요)
python3 -m engine.run presets/examples/rsi-scalping-1m.json --real 14

# CLI 백테스트 (합성 데이터)
python3 -m engine.run presets/examples/rsi-oversold-long.json --minutes 86400

# 로컬 캔들 캐시 미리 채우기 (선택) — 이후 백테스트는 재수집 없이 즉시
python3 -m engine.candle_store BTCUSDT 60   # 60일치
python3 -m engine.candle_store --info       # 캐시 현황

# 독립 캔들 수집기 — 백테스트와 별개로 캐시를 최신 유지
python3 -m engine.collector BTCUSDT ETHUSDT              # 1회 수집
python3 -m engine.collector BTCUSDT --loop 60            # 60초마다 반복(Ctrl+C 종료)
python3 -m engine.collector --watchlist watchlist.example.txt --loop 60
# 백그라운드로: ! python3 -m engine.collector BTCUSDT --loop 60 &

# 테스트
python3 tests/test_engine.py
```

> **캔들 캐시**: 실데이터는 `data/candles.db`(SQLite)에 저장돼 재수집을 피함. 요청 범위 중
> 없는 구간만 바이낸스에서 받아 채움(증분). candle-collector의 MySQL `coin.candle`과 같은
> 스키마라 나중에 실 DB 어댑터로 재활용 가능. (캐시 히트 시 네트워크 수집 대비 ~수백 배 빠름)

### GUI 백테스트 스튜디오
`python3 -m engine.server` 실행 후 브라우저에서 `http://localhost:8765`.
좌측 폼에서 심볼·타임프레임·RSI 임계값·익절/손절·레버리지·필터를 조절하고
**백테스트 실행** → 수익률·MDD·승률·청산사유·자산곡선을 즉시 확인.
**백테스트는 수집된(캐시) 캔들만 사용** — 심볼 입력은 **자동완성 콤보박스**로, 타이핑하면 수집된
심볼만 드롭다운에 필터되어 표시(보유 범위·개수 포함). 선택 시 테스트 가능 기간을 안내.
미수집 심볼이면 "데이터 수집 탭에서 먼저 수집" 안내(서버 캐시 전용). (합성 데이터 옵션은 제거됨)

컨트롤은 **접이식 아코디언** — 기본은 시장·진입만 펼치고 손절/익절/청산/트레일링/사이징/필터/최적화는
접힘. **접힌 헤더에 현재 설정 요약**(예: 손절 `ATR×1.5`, 청산 `RSI>55·12봉`)이 표시돼 한눈에 파악.

**진입은 그룹 중심 조건 빌더 (방향 포함)** — **+ 진입 그룹 만들기**로 그룹을 만들고, 그룹 안에서 **+ 조건 추가(AND)**로 조건을 쌓음. **+ 그룹 추가(OR)**로 다른 그룹. 조건마다 지표 드롭다운으로 종류를 바꿈. **각 그룹은 방향(롱/숏)을 가짐**.
조건 종류는 **지표 하나당 한 항목**: RSI / EMA / SMA / MACD / VWAP / 볼린저 / 스토캐스틱 / StochRSI / CCI / MFI / 거래량(RVOL) / 반전 캔들 패턴(TA-Lib CDL). 비교 방식(가격 대비 vs 크로스 등)은 조건 안에서 선택 — EMA·StochRSI는 "방식" 드롭다운으로 가격대비/크로스 전환.
> 정석 3층 구조 예시: **방향 필터**(가격>EMA50) + **진입 구역**(RSI 과매도 & 가격<VWAP/BB하단) + **방아쇠**(StochRSI 골든크로스 or RVOL 급증) — 한 AND 그룹으로 조립. **청산 지표 조건도 지표 선택식**(RSI/Stoch%K/CCI/MFI) — 최적화의 청산 스윕 라벨·범위가 고른 지표에 맞춰 동적 생성.
→ **`((A and B)→롱) or ((C and D)→숏)`** 형태 = 롱·숏 동시 전략. (그룹=AND, 그룹 간=OR, 그룹별 방향)
서버가 `entryRules`(방향별 규칙)로 변환, 엔진은 순서대로 평가해 먼저 참인 규칙의 방향으로 진입.
(최적화의 `RSI 기준` 스윕은 첫 RSI 조건의 기준값을 자동으로 덮어씀.)
상단 프리셋 버튼(1m 스켈핑 / 15m RSI 반등 / RSI 역추세 / 전저점+리스크 / MACD)으로 시작점 로드. (`engine/gui.html`)

**📥 데이터 수집 탭** (상단 탭): 심볼 + 과거 시작일을 정하면 **지금까지**의 1분봉을 수집.
- **최신 → 과거 순** 청크 수집 → 중간에 **중지**해도 최신 구간은 빈틈없이 확보 (재개 가능)
- **프로그레스 바 + 중지 버튼** (청크 단위 진행, 브라우저가 루프 제어)
- **이미 있는 구간은 자동 스킵**, 빠진 구간(내부 구멍 포함)만 받음
- **레이트리밋 방어**: 페이지 간 대기(0.3s) + 429/418 백오프 재시도
- 하단에 심볼별 캐시 현황 표시

**🔍 파라미터 최적화** (백테스트 탭 좌측 하단): 백테스트 폼의 값 옆 **"범위" 체크박스**를 켜고 범위(최소/최대/단위)를 주면 **그리드 서치**로 최적값 탐색.
- **최적화 = 백테스트 폼 그대로** — 진입 지표·손절·레버리지 등 지금 구성한 그대로 쓰고, 각 파라미터 옆 "범위"를 켠 것만 동적으로 스윕. 진입 조건은 인라인 편집형(값 옆 체크박스). 서버는 `@entry:그룹:조건:경로` 오버라이드로 해당 조건만 패치.
- **멀티프로세싱 병렬** — 조합 평가를 코어 수(최대 8)만큼 동시 실행 (약 5배↑). **NDJSON 스트리밍**으로 조합이 끝나는 대로 **진행률 바 + 실시간 순위**를 즉시 표시. 동점은 결정적 정렬 → 워커 수와 무관하게 재현성 보장.
- **목적함수 Calmar**(수익÷MDD) — 총수익률보다 과최적화에 강함
- **IS/OOS 검증**: 앞 70% 최적화 → 뒤 30% 재검증. 둘 다 수익이면 ✅견고, OOS 실패는 ❌과최적화 의심
- 2개 탐색 시 **히트맵** — 넓은 밝은 구역(견고한 봉우리) vs 뾰족한 스파이크(운빨) 구분. **적용** 버튼은 진입 조건까지 되돌려 세팅
- ⚠️ "수익 최댓값"만 쫓으면 curve-fitting. IS 1위가 OOS에서 무너지는 걸 눈으로 확인 가능

## 프리셋 = 매매기법

프리셋은 4블록 조합: **진입 / 청산 / 사이징(레버리지·증거금) / 필터**.
UI 블록은 껍데기, 진실은 `schema/preset.schema.json` 의 JSON 트리.
예시: [`presets/examples/`](presets/examples/).

**청산 방식** (`exit`, 여러 개 동시 → 먼저 닿는 게 발동):
- `stopLoss`/`takeProfit`: `percent`(가격%) · `atrMultiple`(ATR배수) · `price`(절대가) · `swingLow`(전저점) · `swingHigh`(전고점)
- `trailing`: 고점 대비 콜백 % 되돌림
- `condition`: 지표 조건 청산 (RSI>70, MACD 데드크로스 등)
- `timeStop`: 최대 보유 봉수
- **청산(liquidation)**: 레버리지 강제청산 — 항상 자동

**사이징 방식** (`sizing.size.type`):
- `equityPercent`: 자본의 %를 증거금으로
- `riskPercent`: 손절까지 자본의 X%만 잃도록 수량 역산 (손절 필수) — 손절폭 가변일 때 리스크 일정
- `fixedQuote`/`fixedBase`: 고정 USDT / 코인 수량

> **가격% vs ROI**: 익절 `percent`는 **가격 기준**. 0.4% 익절 = 가격 0.4% 이동 = 10배 레버리지면 증거금의 약 4%.

## 엔진 구조 (`engine/`)

```
candles.py      1분봉 → 상위 TF 리샘플, 결측/중복 방어
indicators.py   지표 — TA-Lib 위임(RSI/MACD/BB/ATR/ADX·DMI/Stoch/StochRSI/CCI/MFI/SMA/EMA)
                + numpy(VWAP/RVOL/SuperTrend/QQE/Hawkeye/오더플로우 델타·CVD)
conditions.py   조건 트리 평가 (AND/OR/NOT, 비교, 교차)
binance_math.py 청산가·펀딩비·수수료 (바이낸스 공식)
backtest.py     코어 루프 — 1분봉 클럭, 이벤트순서 펀딩→청산→손절→신호
metrics.py      성과지표 (수익률/MDD/승률/PF/샤프/청산/펀딩)
optimize.py     그리드 서치 파라미터 최적화 (IS/OOS 분리 + 병렬)
synthetic.py    합성 1분봉 (실데이터 없을 때 검증용)

binance_data.py 바이낸스 공개 klines·펀딩 히스토리 수집 (키 불필요)
candle_store.py 로컬 SQLite 캔들/펀딩 캐시 — 없는 구간만 증분 수집
collector.py    독립 캔들 수집기 (1회/반복, 분 경계 정렬)

live.py         실시간 매매 루프 (페이퍼/실거래) + 리스크 가드레일 + 재시작 시 포지션 동기화
executor.py     주문 실행 어댑터 — PaperExecutor(시뮬) / LiveExecutor(실주문·실체결 반영)
binance_broker.py 실주문 배선(ccxt) — 정밀도·최소주문, BBO, post-only→시장가, 포지션/펀딩 조회
ledger.py       매매 원장 (data/trades.db, append-only. paper/live 분리)
settings.py     글로벌 설정 (동적 레버리지 티어·가드레일) — 백테스트/라이브 공유
control.py      멈춤/재개 신호를 파일(data/control.json)로 전달
env.py          .env 로더 (API 키는 파일에만, 기존 환경변수 우선)

run.py          CLI
server.py       웹 서버 (stdlib http.server) — / 대시보드 · /gui · /collector
dashboard.py    대시보드 단독 실행 (프로덕션용 read-only 모니터)
trade_chart.py  원장의 한 거래 → 진입~청산 구간 캔들+지표 차트 데이터
gui.html        백테스트 스튜디오 (프레임워크·빌드 없음)
dashboard.html  매매 대시보드 / collector.html  데이터·수집기 관리
vendor/         외부 JS. lightweight-charts.standalone.production.js
                = TradingView Lightweight Charts v5 (Apache 2.0) — 캔들 차트 줌/팬/크로스헤어.
                CDN 대신 벤더링 → 오프라인 동작. /vendor/lightweight-charts.js 로 서빙.
```

### 실거래 (테스트넷 → 실돈)

```bash
# 0) 키 준비 — .env 에. Enable Reading + Futures / 출금 OFF / IP 화이트리스트
#    BINANCE_API_KEY=... BINANCE_API_SECRET=... BINANCE_TESTNET=1

# 1) 테스트넷(가짜돈)으로 실주문 경로 검증 — 여기서 며칠 굴린다
python3 -m engine.live presets/saved/내전략.json --live --interval 60

# 2) 실돈. '두 번' 명시해야 돈다 — BINANCE_TESTNET=0 + --real-money
BINANCE_TESTNET=0 python3 -m engine.live presets/saved/내전략.json --live --real-money

# 배포(EC2)에선 .env 의 TRADE_MODE 로 전환: --paper / --live / "--live --real-money"
```

봇은 **항상 '멈춤' 상태로 뜬다** — 대시보드에서 '재개'를 눌러야 새 진입이 시작된다.

#### 대시보드에서 테스트넷 ↔ 실돈 전환

매매 봇 컨트롤바의 **거래소** 스위치로 재기동 없이 갈아탈 수 있다. 권장 흐름은
`TRADE_MODE="--live --real-money"` + `BINANCE_TESTNET=1` 로 **실돈 권한은 주되 테스트넷으로 시작**
→ 대시보드에서 확인 → 버튼으로 실돈 전환.

전환은 플래그를 뒤집는 게 아니라 **다른 계정으로 이사**다. 그래서 이렇게 막아 뒀다:

| 잠금 | 내용 |
|---|---|
| 권한 | `--real-money` 없이 뜬 프로세스는 버튼이 잠긴다(실돈 쪽으로 못 감) |
| 확인 | 실돈 전환은 `실돈` 이라고 직접 입력해야 진행 |
| 무포지션 | 포지션 보유 중이면 청산 후로 미룬다(전략 전환과 같은 규칙) |
| 롤백 | 새 계정의 preflight(원웨이·격리·잔고)가 실패하면 원래 네트워크로 되돌린다 |
| 원장 분리 | `mode` 버킷이 `paper` / `testnet` / `live` 로 갈린다 — 가짜돈 손익이 실돈 수익률에 섞이지 않는다 |

키는 네트워크별로: `BINANCE_TESTNET_API_KEY` / `BINANCE_MAINNET_API_KEY`(없으면 공용
`BINANCE_API_KEY` 로 폴백). 전환 후엔 그 계정의 이력·기준잔고를 다시 읽고, 남아 있는 포지션이
있으면 인계받는다.

시작 전 preflight 가 계정을 점검한다: **헤지 모드면 기동 거부**(reduceOnly 청산 의미가 달라
포지션이 안 닫힐 수 있음), 마진 모드는 **격리로 설정**(엔진 청산가 공식이 격리 기준), 잔고 확인.

실거래에서 백테스트와 달라지는 지점 — 이게 이 어댑터가 하는 일의 전부다:
- **체결가·수량·수수료는 거래소가 정한다.** 엔진이 계산한 `_Position` 을 실제 체결로 덮어쓴다.
  스텝사이즈 절사 후 최소주문(예: BTCUSDT 0.001 BTC / 50 USDT)에 못 미치면 **진입을 건너뛴다.**
- **청산가는 거래소 계산값을 채택** (우리 단일-tier 근사보다 정확).
- **강제청산은 거래소가 한다.** 엔진의 청산 판정은 추정이라, 실제로 털렸으면 그대로 기록하고
  아직 살아 있으면 시장가로 확실히 빠져나온다.
- **재시작하면 거래소의 포지션을 이어받는다**(로컬 상태 맹신 금지). 거래소가 모르는 손절/익절가는
  `data/live_position.json` 사이드카에서 복원 — 없거나 수량이 어긋나면 크게 경고한다.
- **펀딩은 거래소가 정산** → 청산 시 실제 펀딩 내역을 조회해 원장에 남긴다.
- 잔고의 진실은 거래소다. 수익률의 기준잔고는 '현재 실잔고 − 원장 누적손익'으로 역산한다
  (재시작해도 수익률이 리셋되지 않음).

### 핵심 설계 원칙
- **같은 전략 로직을 백테스트·페이퍼·실거래가 공유** (실행 어댑터만 교체)
- **청산을 손절보다 먼저 체크** — 레버리지 백테스트 뻥튀기 방지 (테스트로 보장)
- **1분봉 해상도 청산 판정** — 상위 TF 신호 + 1분봉 정밀 터치 검사

## 다음 할 일

- [x] **실거래 주문 구현** (`LiveExecutor.open/close` + `engine/binance_broker.py`) —
      ccxt `create_order`·`set_leverage`, 아래 BBO→N초→taker 정책, **실제 체결가/수량/수수료로
      포지션을 덮어씀**(청산가는 거래소 계산값 채택), 재시작 시 포지션은 거래소에서 읽어 동기화
      (손절/익절가는 `data/live_position.json` 사이드카에서 복원). 시작 전 preflight 로
      헤지모드 거부·격리마진 설정·잔고 확인. 실돈은 `BINANCE_TESTNET=0` + `--real-money` 이중 잠금.
      (남은 것: ⓐ 테스트넷 실전 검증, ⓑ 자동배포 승인 게이트 — 아래 두 항목)
- [ ] **테스트넷 실전 검증** — `TRADE_MODE=--live` + `BINANCE_TESTNET=1` 로 며칠 돌려
      백테스트 가정 vs 실제 체결(슬리피지·maker 비율·펀딩) 비교. 여기서 나온 차이를
      백테스트 가정에 반영한 뒤에야 실돈.
- [ ] **실돈 전환 안전장치** — GitHub Environment 승인 규칙/테스트 게이트로 나쁜 push 가
      실매매 봇을 갈아치우지 못하게. API 키는 출금권한 OFF + EIP 화이트리스트.
- [ ] **backtest/live 오케스트레이션 통합** — `backtest.run()`의 per-bar 로직을 `step()`으로
      추출해 `live.py`가 문자 그대로 공유 (지금은 같은 순서로 재구현 — `engine/live.py` 상단 주석)
- [ ] ADX/DMI 레짐 게이트를 실제 프리셋에 적용해 재백테스트 (지표·조건은 이미 있고 쓰는 프리셋이 없음)
- [ ] CLI 백테스트(`run.py`)도 실제 펀딩 히스토리 사용 — 지금은 상수 근사 (GUI/`backtest.py`는 실히스토리)
- [ ] direction "both" 롱·숏 동시 (스키마 v2: entryLong/entryShort 분리)
- [x] **maker 진입 passive-then-aggressive** (백테스트/페이퍼) — `execution.makerTimeoutSeconds`
      를 주면 post-only 지정가를 걸어두고(passive) 그 안에 가격이 지정가를 터치하면 maker 체결,
      아니면 시장가로 추격(aggressive). Stepper의 대기 지정가(pending) 상태머신, 룩어헤드 없이
      봉당 판정 → 백테스트·라이브 step() 공유 유지(1분봉이라 초→max(1,round(초/60))봉). 미설정이면
      옛 동작(신호봉 종가 즉시 maker). 프리셋 만들기·스튜디오 진입체결에 '폴백(초)' 노출.
      (남은 것: ⓐ SuperTrend/역추세 청산에도 같은 폴백, ⓑ maker 체결가를 종가 대신 BBO(bid/ask)로
      — 호가 데이터 필요, ⓒ 라이브에서 '여러 봉에 걸친 대기 지정가'는 아직 Stepper가 시뮬레이션한다:
      가격이 지정가를 터치한 봉에서 그때 실주문(BBO→N초→taker)을 낸다. 진짜로 N분간 호가에
      얹어두려면 거래소에 주문을 걸어두고 상태를 폴링하는 방식이 필요.)
- [x] **BBO → N초 → taker 체결 정책 (실거래 쪽)** — `binance_broker.limit_then_market`:
      ① post-only 지정가(GTX)를 BBO에 건다 → ② `MAKER_FILL_TIMEOUT_SEC`(기본 3초) 안에 안 채워지면
      취소 → ③ 남은 수량만 시장가. 진입과 maker 청산(SuperTrend 전환·가격 익절)이 이 경로를 타고,
      **손절·강제청산은 언제나 시장가**(확실히 빠져나가는 게 우선). 대부분 maker 로 빠져 수수료
      0(BTCUSDC), 급반전 때만 taker. 백테스트(낙관적 maker 가정)와의 차이는 "N초 내 미체결분의
      taker 수수료 + 슬리피지"로 한정된다.
- [ ] **지정가 청산 현실화 — 백테스트 쪽 (미체결·슬리피지)** — 실거래는 위 정책으로 정직해졌지만
      **백테스트는 여전히** SuperTrend 전환 청산·가격 익절을 '신호봉 종가에 maker 체결'로 가정한다
      (손절·강제청산은 taker). 청산도 진입처럼 fill-if-touched / passive-then-aggressive 로 바꿔야
      두 경로의 가정이 맞는다. 테스트넷 실측 슬리피지를 여기에 반영하는 게 다음 순서.
