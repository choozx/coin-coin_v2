# 바이낸스 USDⓈ-M 선물 — 공식 정산 공식 모음

> 백테스트/페이퍼/실거래 엔진이 청산·펀딩비·증거금을 계산할 때 참조하는 **단일 진실 소스**.
> 모든 공식은 바이낸스 공식 문서 기준. 하단 출처 참조.

---

## 1. 청산가 (Liquidation Price)

### 변수 정의 (공식 문서 기준)

| 변수 | 의미 |
|------|------|
| `WB` | Wallet Balance. 격리=격리지갑잔고(=해당 포지션에 할당한 증거금), 교차=교차지갑잔고 |
| `TMM1` | 나머지 모든 계약의 유지증거금 합 (Contract 1 제외) |
| `UPNL1` | 나머지 모든 계약의 미실현손익 합 (Contract 1 제외) |
| `cumB` | 해당 포지션의 유지증거금 공제액 (Maintenance Amount) — 구간표에서 나옴 |
| `Side1BOTH` | 포지션 방향. **롱=+1, 숏=-1** |
| `Position1BOTH` | 포지션 크기 절대값 (코인 수량) |
| `EP1BOTH` | 진입가 (Entry Price) |
| `MMRB` | 유지증거금률 (Maintenance Margin Rate) — 구간표에서 나옴 |

### 일반식 (one-way 모드)

```
                WB − TMM1 + UPNL1 + cumB − (Side1BOTH × Position1BOTH × EP1BOTH)
LiqPrice = ────────────────────────────────────────────────────────────────────
                        Position1BOTH × MMRB − (Side1BOTH × Position1BOTH)
```

### 격리 마진 단순화 (isolated, 포지션 1개)

격리 모드에서는 **`TMM1 = 0`, `UPNL1 = 0`**, `WB = 이 포지션에 할당한 증거금`.

```
              WB + cumB − Side × Pos × EP
LiqPrice = ───────────────────────────────
                  Pos × MMR − Side × Pos
```

- **롱 (Side=+1)**: 분모가 음수(MMR<1) → 진입가보다 **아래**에 청산가
- **숏 (Side=-1)**: 진입가보다 **위**에 청산가

### 백테스트 구현 노트

- `WB`(격리 증거금) = `명목가치 / leverage` = `Pos × EP / leverage` (진입 시점)
- `cumB`, `MMR`는 **포지션 명목가치 구간(tier)에 따라 달라짐** → 심볼별 구간표 필요
- 구간표는 하드코딩 대신 **바이낸스 API `GET /fapi/v1/leverageBracket`** 으로 심볼별로 받아 캐싱 권장
- 백테스트에서는 명목가치가 첫 구간(가장 낮은 tier) 안에 있으면 그 심볼의 tier0 `MMR`/`cumB`만 써도 근사 충분. 큰 포지션이면 tier 재계산 필요.

---

## 2. 펀딩비 (Funding Fee)

```
펀딩료 = 포지션 명목가치 × 펀딩비율
포지션 명목가치 = 마크가격(Mark Price) × 포지션 크기
```

- **정산 주기**: 8시간마다 — **00:00 / 08:00 / 16:00 UTC**
- 정산 시점에 포지션을 들고 있을 때만 발생. 정산 직전 청산하면 안 냄.
- **펀딩비율 > 0**: 롱이 숏에게 지불
- **펀딩비율 < 0**: 숏이 롱에게 지불
- 거래소가 걷는 게 아니라 트레이더 간 직접 이전 (peer-to-peer)

### 백테스트 구현 노트

- 포지션 보유 중 00:00/08:00/16:00 UTC 캔들 경계를 지날 때마다 펀딩료를 손익에 반영
- 과거 펀딩비율은 **바이낸스 API `GET /fapi/v1/fundingRate`** 로 확보 (심볼별 히스토리)
- 펀딩비율 데이터가 없으면 상수 근사(예: ±0.01%/8h)로 시작하고 나중에 실제값 주입
- `filter.avoidFundingWindowMinutes`, `filter.maxFundingRate` 필터가 이 값을 참조

---

## 3. 유지증거금 (Maintenance Margin)

```
유지증거금 = 포지션 명목가치 × MMR − cumB(Maintenance Amount)
```

- 계정의 (증거금 + 미실현손익)이 유지증거금 밑으로 내려가면 청산 트리거
- MMR은 명목가치가 클수록 단계적으로 커짐(레버리지 상한도 같이 내려감) = **구간(tier) 시스템**

---

## 4. 수수료 (Trading Fee) — 백테스트 기본값

> 정확한 값은 계정 VIP 등급/BNB 할인에 따라 다름. 아래는 표준(VIP0) 근사. 설정으로 override 가능하게.

| 항목 | USDⓈ-M 선물 표준 |
|------|------------------|
| Maker | 0.0200% |
| Taker | 0.0500% |
| 슬리피지 | 백테스트에서 별도 가정 (예: taker + N틱) |

- 시장가 진입/청산 = taker, 지정가 체결 = maker 로 모델링
- 레버리지 매매는 명목가치 기준으로 수수료가 붙어서 실제 자본 대비 체감이 큼 (레버리지 배수만큼)

---

## 5. 백테스트에서 반드시 지켜야 할 순서

한 캔들 안에서 여러 이벤트가 겹칠 때 처리 우선순위:

```
1. 펀딩 정산 (캔들이 00/08/16 UTC 경계 포함 시)
2. 청산 체크   ← 캔들 저가/고가가 청산가를 건드리면 손절보다 먼저 청산!
3. 손절/익절/트레일링 체크
4. 지표 재계산 → 진입/청산 신호 판정
```

> **가장 흔한 백테스트 버그**: 손절만 체크하고 청산을 안 봐서, 실제로는 청산당했을 포지션을
> "손절로 잘 막았다"고 착각 → 백테스트 수익률이 실제보다 뻥튀기됨.

---

## 출처

- [How to Calculate Liquidation Price of USDⓈ-M Futures Contracts (Binance Support)](https://www.binance.com/en/support/faq/how-to-calculate-liquidation-price-of-usd%E2%93%A2-m-futures-contracts-b3c689c1f50a44cabb3a84e663b81d93)
- [Leverage and Margin of USDⓈ-M Futures (Binance Support)](https://www.binance.com/en/support/faq/detail/360033162192)
- [Binance Futures Liquidation Protocols](https://www.binance.com/en/support/faq/binance-futures-liquidation-protocols-360033525271)
- 펀딩비: Binance Support — Funding Rate / Funding Fee 계산 문서
- 구간표/브래킷: 바이낸스 API `GET /fapi/v1/leverageBracket`, 펀딩 히스토리 `GET /fapi/v1/fundingRate`

> ⚠️ 공식 페이지의 청산가 방정식은 이미지로 게시되어 있어 위 식은 문서의 변수 정의로부터 복원한 것.
> 실거래 붙이기 전 반드시 실제 바이낸스 계정의 청산가 표시와 대조 검증할 것.
