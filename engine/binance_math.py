"""바이낸스 USDⓈ-M 선물 정산 공식.

docs/binance-formulas.md 의 공식을 코드로 옮긴 것. 청산가/펀딩비/수수료.
브래킷(MMR/cum)은 v1에서 단일 tier 근사 — 실거래 전 API로 교체 필요.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


# --- 수수료 기본값 (VIP0, override 가능) ------------------------------------
DEFAULT_TAKER_FEE = 0.0005   # 0.05%
DEFAULT_MAKER_FEE = 0.0002   # 0.02%


@dataclass
class MarginBracket:
    """유지증거금 브래킷 (단일 tier 근사).

    실제로는 명목가치 구간별로 달라짐. v1은 tier0 근사값 사용.
    실거래 전 GET /fapi/v1/leverageBracket 으로 심볼별 교체.
    """
    mmr: float = 0.004    # 유지증거금률 (0.4%)
    cum: float = 0.0      # 유지증거금 공제액 (Maintenance Amount)


def liquidation_price(entry: float, qty: float, leverage: int, side: int,
                      bracket: MarginBracket, wallet_balance: float = None) -> float:
    """격리 마진, one-way 포지션 청산가.

        LiqPrice = (WB + cum - side*qty*EP) / (qty*MMR - side*qty)

    side: 롱=+1, 숏=-1
    wallet_balance: 이 포지션에 할당한 증거금. 미지정 시 명목가치/leverage.
    """
    ep = entry
    if wallet_balance is None:
        wallet_balance = qty * ep / leverage
    num = wallet_balance + bracket.cum - side * qty * ep
    den = qty * bracket.mmr - side * qty
    return num / den


def funding_boundaries_ms(start_ms: int, end_ms: int):
    """[start, end] 구간에 포함되는 펀딩 정산 시각(00/08/16 UTC) ms 리스트."""
    out = []
    dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    # start 이상인 첫 정산시각으로 정렬
    anchor = dt.replace(minute=0, second=0, microsecond=0)
    hour_slots = [0, 8, 16]
    # anchor 시각의 하루 00:00부터 후보 생성
    day0 = anchor.replace(hour=0)
    t = day0
    while int(t.timestamp() * 1000) <= end_ms:
        if t.hour in hour_slots:
            ms = int(t.timestamp() * 1000)
            if start_ms <= ms <= end_ms:
                out.append(ms)
        # 8시간씩 진행
        t = t.fromtimestamp(t.timestamp() + 8 * 3600, tz=timezone.utc)
    return out


def is_funding_time(open_time_ms: int) -> bool:
    """이 1분봉 시작시각이 펀딩 정산시각(00/08/16 UTC 정각)인가."""
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    return dt.minute == 0 and dt.hour in (0, 8, 16)


def funding_fee(mark_price: float, qty: float, side: int, funding_rate: float) -> float:
    """펀딩료(계정 관점, 음수=지불, 양수=수취).

    펀딩료 = 명목가치 * 펀딩비율. 비율>0이면 롱이 지불, 숏이 수취.
    """
    notional = mark_price * qty
    # 롱(+1): rate>0이면 지불(-). 숏(-1): rate>0이면 수취(+).
    return -side * notional * funding_rate


def trade_fee(price: float, qty: float, taker: bool = True,
              taker_fee: float = DEFAULT_TAKER_FEE, maker_fee: float = DEFAULT_MAKER_FEE) -> float:
    """체결 수수료(양수). 명목가치 기준."""
    rate = taker_fee if taker else maker_fee
    return price * qty * rate
