# research/ — 전략 엣지 연구

가설을 **적어두고**(→ `BACKLOG.md`), 하나씩 꺼내 **돌려보는**(→ `exp_*.py`) 곳.
scratchpad 가 아니라 리포지토리에 두어 사라지지 않게 한다(예전에 연구 스크립트를
scratchpad 에만 두어 날린 적이 있음).

## 구성
- **`BACKLOG.md`** — 가설 큐. 우선순위·상태·검증법·판정기준·결과. **여기부터 읽는다.**
- **`lib.py`** — 공용 하네스. 실험이 짧아지도록 배관을 모아둠:
  - `load(symbol, days=… | start_ms,end_ms=…)` → (candles, funding_schedule) · 캐시 전용
  - `backtest(candles, preset_dict, symbol, …)` → Metrics · 실수수료·실펀딩, GUI 와 같은 엔진
  - `null_model(candles, timeframe, n_trades, hold_bars, side, …)` → 랜덤진입 수익분포
  - `verdict(strategy_return, null_dist)` → 우연 초과 여부(엣지 판정)
  - `show(tag, m)` / `summarize(m)` → 결과 한 줄/‌dict
- **`exp_*.py`** — 실험 하나 = 가설 하나. `python3 -m research.exp_<이름>` 으로 실행.

## 새 실험 만들기
1. `BACKLOG.md` 에서 `📋 대기` 항목 고르기.
2. `exp_<id>.py` 작성 — `exp_C_taker_delta.py` 를 템플릿으로 복사해 고치면 빠르다.
3. `python3 -m research.exp_<id> [인자]` 실행.
4. 결과를 `BACKLOG.md` 의 해당 항목 **결과** 칸 + 표 상태에 반영.

## 판정 원칙 (프로젝트 대전제)
- **"엣지 없음"도 유효한 결론.** 돈 버리는 봇 배포를 막는 게 성과다.
- 전략 수익이 **매칭 귀무모델의 95%선**을 못 넘으면 엣지 아님(우연·드리프트·커브핏 방어).
- 상승장 롱은 BTC 드리프트로 부풀린 귀무를, 롱숏은 −수수료를 +로 뒤집을 문턱을 넘어야 한다.
- 반드시 **out-of-sample**. IS 1위가 OOS 서 무너지는 커브핏을 이미 실데이터로 확인함.

## 데이터
캐시(`data/candles.db`, 1분봉). 없으면 `/collector` 또는
`python3 -m engine.collector SYMBOL --seed-days N`. 오더플로우 실험은 taker_buy 백필 필요:
`python3 -m engine.candle_store --backfill-taker SYMBOL`. 실펀딩은 `load(...)` 이 자동 로드.

관련 메모리: `edge-research`(판정 프레임·데이터), `auto-trading-project`(설계·후보 순위).
