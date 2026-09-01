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
- **귀무는 '무엇을 제거하는가'로 설계한다.** 전략과 귀무가 신호 말고 다른 것도 다르면 그 차이를
  신호로 오독한다. L 에서 실제로 당했다 — 순위 전략은 랭킹이 이어져 회전율 9~48%, 랜덤 선택은
  매번 새로 뽑아 ~80%. 순비용으로 재니 **덜 사고팔았다는 이유만으로** 70조합 중 16개가 세 구간을
  '통과'했는데 **전부 −9~−97% 손실**이었다(귀무는 −74~−99%).
  → **gross(신호)와 net(비용)을 나눠 본다.** ①비용 0에서 귀무를 넘는가(신호가 있는가)
  ②그 신호가 실비용을 견디는가. ①이 실패하면 ②는 볼 필요가 없다.
- **격자를 훑으면 다중검정을 같이 본다.** 70조합을 p95 로 재면 우연히 3.5개가 통과한다.
  '몇 개가 통과했나'가 그 기대치를 넘는지, 그리고 **복수 구간에서 같은 조합이** 통과하는지가 판정.
- **깊게 음수인 구간에서의 '귀무 초과'는 엣지가 아니다.** 둘 다 파산이면 덜 파산한 것뿐이다.
  통과 판정이 나오면 **절대 수익률을 반드시 눈으로 확인할 것.**

### 실행 함정
- `python3 -m research.exp_X` 로 돌리면 그 모듈은 `__main__` 이다. 안에서
  `import research.exp_X` 하면 **두 번째 사본**이 생겨 모듈 전역(수수료 상수 등)을 바꿔도
  지금 도는 코드엔 안 먹힌다. 전역을 바꿔야 하면 `globals()` 를 쓸 것.
  (L 에서 gross 스캔이 net 과 똑같은 숫자를 내 발각됐다.)
- 백그라운드/리다이렉트 실행은 출력이 버퍼링된다 → `python3 -u` 로 돌릴 것.

## 데이터
캐시(`data/candles.db`, 1분봉). 없으면 `/collector` 또는
`python3 -m engine.collector SYMBOL --seed-days N`. 오더플로우 실험은 taker_buy 백필 필요:
`python3 -m engine.candle_store --backfill-taker SYMBOL`. 실펀딩은 `load(...)` 이 자동 로드.

관련 메모리: `edge-research`(판정 프레임·데이터), `auto-trading-project`(설계·후보 순위).
