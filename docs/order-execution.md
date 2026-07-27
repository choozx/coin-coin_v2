# 주문 체결 — maker/taker 플로우와 BBO 재호가 설계

> 실거래 진입·청산이 실제로 어떤 순서로 체결되는지, 왜 maker 를 노리는데 taker 로 새는지,
> 그리고 이를 고치기 위한 **BBO 재호가(maker 추격) 설계 결정**을 기록한다.
> 코드: `engine/binance_broker.py`(주문 실행) · `engine/executor.py`(노브 전달) · `engine/backtest.py`(봉 단위 지정가).

## 왜 maker 를 노리나

BTCUSDC 선물은 **maker 수수료 0%**, taker 0.05%(`binance_math.fees_for_symbol`). 15분봉 전략이라도
진입·청산 양다리가 taker 로 나가면 왕복 0.1% 가 손익에서 빠진다. 실제 한 거래에서 gross 손실의
**21% 가 수수료**였다(진입 65,095 → 청산 64,782.80, gross −23.98, 수수료 −5.00, pnl −29.14).
maker 로만 체결됐다면 그 5.00 은 0 이었다.

## 체결 로직이 있는 두 레이어 (헷갈리지 말 것)

체결에는 성격이 다른 두 층이 있고, 이 문서가 다루는 건 **②번(브로커 레이어)** 이다.

| 레이어 | 위치 | 해상도 | 하는 일 |
|---|---|---|---|
| ① 봉 단위 지정가 진입 | `backtest.Stepper` (`makerTimeoutBars`) | 1분봉 | 신호봉 종가에 지정가를 걸고 N봉 안에 터치하면 maker, 아니면 taker 추격. 백테스트·라이브 공통. |
| ② 주문 실행 | `binance_broker.limit_then_market` | 초 단위 | 실제 거래소 주문 한 건을 어떻게 낼지. post-only 지정가 → 미체결분 시장가. **라이브 전용.** |

①은 "언제 진입 주문을 낼지", ②는 "그 주문을 어떤 방식으로 체결시킬지"다. 이 문서의 재호가 설계는
②에만 들어간다 — ①의 봉 단위 pending 로직은 안 건드린다.

## 이전 구현(v1): 직접 만든 BBO — 한 번 걸고 안 되면 taker

> 아래는 재호가 도입 **전** 동작이다(`max_attempts=1` 이면 지금도 이 경로). 문제와 그 원인을 남겨둔다.

`limit_then_market` 은 바이낸스 네이티브 BBO 옵션(아래 참조)을 **쓰지 않고 직접 구현**했다.

```
호가창 조회(fetch_order_book) → best bid/ask 추출
  → 붙는 쪽(매수=best bid / 매도=best ask)에 post-only(GTX) 지정가
  → timeout_s 초 대기(_wait_fill, 상태 폴링만)
     ├─ 전량 체결 → 끝 (maker)
     └─ 미체결/부분체결 → 주문 취소 → 남은 수량 시장가(taker)
```

- **GTX(post-only)** 라 지정가 부분은 maker 보장 — 만약 걸자마자 교차하면 거래소가 거부하고 즉시 시장가.
- **부분체결은 섞인다** — 채워진 만큼 maker, 남은 만큼만 taker. 손익·수수료는 실제 체결내역(my_trades)에서 확정.

### 문제였던 것: taker 로 잘 샜다 (아래 '재호가'로 해결)

1. **재호가가 없다.** 처음 읽은 best bid 에 한 번 걸고 `timeout_s`(기본 3초) 동안 기다리기만 한다.
   대기 중 호가가 도망가면 걸어둔 지정가는 터치에서 멀어져 미체결 → taker.
2. **best bid = 그 가격 큐의 맨 뒤.** 채결되려면 앞 물량이 다 빠지고 누가 내 호가로 팔아줘야 한다.
   15분봉 전략에서 3초 안에 그런 일은 드물다.

즉 BBO 구현 자체는 맞지만, 체결 전략이 "한 번 걸고 안 되면 taker" 라 maker 0% 를 거의 못 먹는다.

### 참고: 바이낸스 네이티브 BBO(`priceMatch`)는 왜 안 쓰나

바이낸스 선물 API 에 `priceMatch` 파라미터가 있고 문서에서 "BBO Orders" 라 부른다
(`QUEUE`/`QUEUE_5/10/20` = 내 쪽 호가, `OPPONENT`/… = 반대쪽 호가; `price` 와 병용 불가).
그런데 네이티브로 바꿔도 이 문제는 안 풀린다:

- **재호가는 네이티브도 안 한다** — 가격은 *주문 시점* 호가에 고정, 이후 시장이 움직여도 안 따라간다.
- **QUEUE 는 maker 보장 안 됨** — 공식 문구상 "상황에 따라 taker 로 체결될 수 있음". 반면 우리 GTX 는 maker 아니면 거부라 **오히려 GTX 가 수수료 목적엔 낫다.**

→ 네이티브 전환의 이득은 코드 단순화(호가 조회~주문 사이 레이스 제거) 정도지 수수료 개선이 아니다.
출처: [Binance — Understanding BBO Orders](https://www.binance.com/en/support/faq/understanding-and-using-bbo-orders-on-binance-futures-7f93c89ef09042678cfa73e8a28612e8),
[API Change Log(priceMatch)](https://developers.binance.com/docs/derivatives/change-log).

## 결정: BBO 재호가(maker 추격) N회 → 남으면 taker

**상태: 구현됨(2026-07).** 코드: `binance_broker.limit_then_market(max_attempts=…)` +
`executor.LiveExecutor.maker_max_attempts`(env `MAKER_MAX_ATTEMPTS`, 기본 5). 테스트:
`tests/test_broker_reprice.py`. 스프레드를 넘지 않고 maker 로 붙되, 못 채우면 무한정 끌지 않고
정해진 횟수만큼만 추격한 뒤 남은 수량을 taker 로 마무리한다.

### 새 플로우

```
남은수량 = 주문수량
반복 최대 MAKER_MAX_ATTEMPTS 회:
    best bid/ask 새로 가져오기               # 매 회차 재호가(reprice)
    붙는 쪽에 post-only(GTX) 지정가로 '남은수량'
    MAKER_FILL_TIMEOUT_SEC 초 대기
    부분체결분 확정 → 남은수량 차감
    남은수량 == 0 → 끝 (전량 maker)
    미체결 취소 → 다음 회차
반복 종료 후 남은수량 > 0:
    남은수량만 시장가(taker)                 # 이미 채워진 maker분은 그대로
반환: maker분 + taker분 합친 Fill
```

### 파라미터

| 노브 | 기본값 | 의미 |
|---|---|---|
| `MAKER_MAX_ATTEMPTS` | **5** | maker 재호가 시도 횟수. 초과 시 남은 수량 taker. |
| `MAKER_FILL_TIMEOUT_SEC` | **3** | 회차당 대기 초(기존 노브 재사용). |

- 최악의 경우 taker 까지 `5 × 3 = 15초`. 폴링 간격(60초) 안이라 그 폴은 최대 15초 블록된다.
- `MAKER_MAX_ATTEMPTS=1` 이면 현행(한 번 걸고 taker)과 동일 — 하위호환.
- **기본값 5 는 모든 배포에 이 동작을 기본 적용한다**(현재는 사실상 1회).

### 적용 범위

`limit_then_market` 은 **진입·maker 청산 둘 다** 쓴다 → 둘 다 5회 추격 후 taker 로 바뀐다.
슈퍼트렌드 maker 청산도 포함. ①의 봉 단위 pending(`makerTimeoutBars`)은 별개라 무영향.

## 트레이드오프 (이건 슬리피지 제거가 아니라 형태 변환)

재호가 추격은 교차 슬리피지를 없애는 대신 다른 비용으로 옮긴다. 채택하되 인지하고 있어야 한다.

1. **체결 미스 위험** — 추세 진입에서 가격이 달아나면 best bid 는 영원히 안 채워질 수 있다.
   5회 경계 + taker 마무리(옵션 B)로 완화했지만, 결국 달아난 가격을 taker 로 잡는다.
2. **추격 슬리피지** — 롱에서 bid 를 100→101→102 로 따라 올리면 신호가보다 비싸게 maker 체결.
   교차 슬리피지가 사라진 대신 추격 슬리피지가 생긴다.
3. **역선택** — maker 가 채워지는 순간은 대개 가격이 나한테 돌아올 때(=모멘텀이 꺾일 때)다.
4. **백테스트 괴리** — 이 로직은 브로커 레이어(라이브 전용)라 백테스트 Stepper 엔 없다.
   백테스트는 신호가 maker 가정 → 라이브 체결가가 백테스트와 더 벌어진다. 파리티 테스트가 잡는
   범위 밖(그건 판정 로직 일치만 검증). 성과 비교 시 이 갭을 감안할 것.

## 구현 시 체크리스트

- [x] `binance_broker.limit_then_market` 에 `max_attempts` 추가, 회차 루프로 재작성
- [x] 회차별 부분체결 누적 + 취소/재확인 레이스 처리(기존 `_settled` 재사용)
- [x] GTX 거부(교차) 회차 처리 — 그 회차를 소진으로 보고 남은 수량 taker
- [x] `executor.LiveExecutor` 에 `maker_max_attempts`(env `MAKER_MAX_ATTEMPTS`, 기본 5) → open/close 에서 전달
- [x] `.env.example` 에 `MAKER_MAX_ATTEMPTS` 문서화
- [x] 테스트: 전량 maker / 부분체결 후 taker / 전 회차 미체결 후 전량 taker / GTX 거부 (`tests/test_broker_reprice.py`)
