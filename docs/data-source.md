# 캔들 데이터 소스 — candle-collector

> 백테스트 데이터가 저장된 곳. 수집기: https://github.com/choozx/candle-collector (Go)
> 이 문서는 백테스트 **데이터 어댑터**가 맞춰야 할 실제 저장 포맷.

---

## 요약 판단

| 항목 | 내용 | 우리에게 의미 |
|------|------|---------------|
| 소스 | `fapi.binance.com/fapi/v1/klines` | **바이낸스 USDT-M 선물** = 우리 대상과 정확히 일치 ✅ |
| 타임프레임 | **1분봉(1m) 단일** (하드코딩) | 상위 TF 자유 합성 가능 + 청산 시뮬 정밀도↑ ✅ |
| DB | MySQL, DB명 `coin` | 어댑터는 MySQL 리더 (또는 1회 export) |
| 펀딩비 | **수집 안 됨** | 별도 확보 필요 ⚠️ |
| 수집 시작 | 코드상 최소 `2022-01-01` | 실제 보유 범위는 심볼별로 확인 필요 |

---

## 테이블 스키마

### `candle`
바이낸스 klines 12필드를 그대로 저장. (컬럼명 = gorm 태그)

| 컬럼 | 타입 | 의미 |
|------|------|------|
| `open_time` | bigint (ms epoch) | 캔들 시작 시각 |
| `symbol` | int | **symbol.code FK** (문자열 아님!) |
| `open_price` | double | 시가 |
| `high_price` | double | 고가 |
| `low_price` | double | 저가 |
| `close_price` | double | 종가 |
| `volume` | double | 거래량(base) |
| `close_time` | bigint (ms epoch) | 캔들 종료 시각 |
| `quote_asset_volume` | double | 거래대금(quote) |
| `number_of_trades` | int | 체결 건수 |
| `taker_buy_base_asset_volume` | double | 테이커 매수 거래량 |
| `taker_buy_quote_asset_volume` | double | 테이커 매수 거래대금 |

> ⚠️ gorm 모델에 PK·유니크 인덱스 정의 없음 → **중복 행 가능성**. 어댑터에서
> `(symbol, open_time)` 기준 dedup 필수. 결측 구간(수집 누락)도 방어.

### `symbol`
| 컬럼 | 타입 | 의미 |
|------|------|------|
| `code` | int PK autoincrement | 캔들 테이블이 참조하는 코드 |
| `name` | varchar | 심볼명 (예: `BTCUSDT`) |
| `is_update` | bool | 수집 활성 여부 |

→ 심볼명으로 조회하려면 `candle JOIN symbol ON candle.symbol = symbol.code`.

---

## 데이터 어댑터 설계 함의

1. **1분봉 → 상위 TF 리샘플링**
   - 프리셋 `timeframe`이 `15m`이면 1m 15개를 OHLCV 규칙으로 집계
     (open=첫 open, high=max, low=min, close=마지막 close, volume=합)
   - 경계 정렬 주의: 15m 봉은 매시 00/15/30/45분에 시작
2. **청산 시뮬은 1분봉으로**
   - 신호 판정은 프리셋 TF(예 1h)로, **청산·손절 터치 판정은 1분봉**으로 내려가서 확인
     → "이 1시간 안에 청산가를 먼저 건드렸나 손절가를 먼저 건드렸나"를 정밀 판정
3. **심볼 int 코드 매핑**: 어댑터가 `symbol` 테이블을 먼저 읽어 이름↔코드 맵 구성
4. **시간축**: 모두 ms epoch UTC. 펀딩 정산(00/08/16 UTC) 경계 계산에 그대로 사용
5. **중복/결측 방어**: 로드 시 `(symbol, open_time)` dedup + 연속성 검사(구멍 로깅)

### 접근 방식 선택지 (다음에 결정)
- **A. MySQL 직접 리드**: 어댑터가 `coin` DB에 붙어 쿼리. 항상 최신, DB 접속정보 필요
- **B. 1회 export → Parquet**: 심볼·기간별로 parquet로 덤프 후 백테스트는 파일만 읽음.
  재현성·속도 좋음. (권장: 로컬 DB가 이 세션에서 안 보이므로 export 산출물을 넘겨받는 방식도 가능)

---

## 펀딩비 데이터 확보 (빈 곳 메우기)

수집기가 안 모았으므로 별도 필요:
- **정확**: 바이낸스 `GET /fapi/v1/fundingRate?symbol=BTCUSDT` 로 과거 펀딩비율 히스토리 확보
  (8시간 간격, `fundingTime` + `fundingRate`). candle-collector에 수집 기능 추가도 가능.
- **근사 시작**: 우선 상수(예 0.01%/8h)로 엔진 뼈대 검증 → 나중에 실제값 주입
- 상세 공식은 [`binance-formulas.md`](./binance-formulas.md) 참조
