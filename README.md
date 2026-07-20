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
| 페이퍼 트레이딩 | ⬜ 예정 (엔진 재사용) |
| 리스크 가드레일 (kill switch 등) | ⬜ 예정 |
| 실거래 어댑터 (ccxt) | ⬜ 예정 |
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
indicators.py   지표 — TA-Lib 위임(RSI/MACD/BB/ATR/Stoch/StochRSI/CCI/MFI/SMA/EMA)
                + numpy(VWAP/RVOL/SuperTrend/오더플로우 델타·CVD)
conditions.py   조건 트리 평가 (AND/OR/NOT, 비교, 교차)
binance_math.py 청산가·펀딩비·수수료 (바이낸스 공식)
backtest.py     코어 루프 — 1분봉 클럭, 이벤트순서 펀딩→청산→손절→신호
metrics.py      성과지표 (수익률/MDD/승률/PF/샤프/청산/펀딩)
synthetic.py    합성 1분봉 (실데이터 없을 때 검증용)
run.py          CLI
server.py       GUI 백테스트 스튜디오 (stdlib http.server)
gui.html        GUI 단일 파일 (프레임워크·빌드 없음)
vendor/         외부 JS. lightweight-charts.standalone.production.js
                = TradingView Lightweight Charts v5 (Apache 2.0) — 캔들 차트 줌/팬/크로스헤어.
                CDN 대신 벤더링 → 오프라인 동작. /vendor/lightweight-charts.js 로 서빙.
```

### 핵심 설계 원칙
- **같은 전략 로직을 백테스트·페이퍼·실거래가 공유** (실행 어댑터만 교체)
- **청산을 손절보다 먼저 체크** — 레버리지 백테스트 뻥튀기 방지 (테스트로 보장)
- **1분봉 해상도 청산 판정** — 상위 TF 신호 + 1분봉 정밀 터치 검사

## 다음 할 일

- [ ] 실데이터 어댑터: candle-collector MySQL(`coin.candle`) 리더 or Parquet export
- [ ] 펀딩비 실제 히스토리 주입 (`/fapi/v1/fundingRate`)
- [ ] 리스크 가드레일 (일일 손실 한도, kill switch)
- [ ] 페이퍼 트레이딩 (실시간 시세 + 같은 엔진)
- [ ] direction "both" 롱·숏 동시 (스키마 v2: entryLong/entryShort 분리)
- [ ] **maker→taker 폴백 체결 (passive-then-aggressive)** — 현재 `execution.entryType`은
      `taker`(시장가 즉시)와 `makerLimit`(지정가, 미체결 시 스킵)만 지원. 실거래에서 흔히 쓰는
      "post-only 지정가 먼저 → 타임아웃 내 미체결이면 시장가(taker)로 추격" 방식을 추가하면
      **"반드시 진입"하는 모멘텀 전략의 진짜 순이익**(추격 슬리피지 포함)까지 정직하게 나온다.
      구현: `makerLimit` 위에 타임아웃 폴백만 얹으면 됨(placeholder 주문이 timeout 도달 시
      그 봉 종가로 taker 체결). 주의: 수수료 절감은 되돌아온(나쁜) 거래에 몰리고, 신호 방향으로
      튄(좋은) 거래는 maker 미체결 후 더 비싼 taker로 잡히는 역선택이 있음. BTCUSDC(maker 0)
      스캘핑에선 이게 실전 표준 체결 방식이 될 것. 백테스트↔실거래 동일 로직 원칙에 부합.
