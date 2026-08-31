"""체결 실측 로그 — 엔진이 기대한 체결과 거래소가 실제로 준 체결의 차이를 남긴다.

왜 필요한가: `LiveExecutor.open()` 은 실제 체결로 포지션을 덮어쓴다(`pos.entry_price =
fill.price`). 그 순간 **엔진이 기대했던 가격이 사라진다.** 원장에도 maker/taker 구분이 없다.
그래서 며칠을 돌려도 "슬리피지가 얼마였나 · maker 로 몇 % 나 빠졌나"에 답할 수 없었다 —
백테스트 가정을 실측으로 교정하려면 바로 그 두 값이 필요한데.

여기서 체결 하나를 한 줄로 남긴다: 기대가 · 실제가 · 불리 방향 슬리피지 · maker/taker 수량 ·
실수수료. `Fill` 이 이미 다 들고 있으므로 **버리지 않고 적기만** 하면 된다.

환경변수:
    FILL_LOG_PATH   기록 파일(기본 data/fill_log.jsonl). 빈 값이면 기록 끔.
    FILL_LOG_KEEP   보관 줄 수(기본 5000). 넘으면 오래된 줄부터 버린다.
"""
from __future__ import annotations

import os

from . import jsonl_log

DEFAULT_PATH = os.environ.get("FILL_LOG_PATH", "data/fill_log.jsonl")
DEFAULT_KEEP = int(os.environ.get("FILL_LOG_KEEP", "5000") or 0)


def is_buy(kind: str, side: int) -> bool:
    """이 체결이 매수인가. 진입 롱 / 청산 숏이 매수다 — 슬리피지의 '불리한 방향'이 여기서 갈린다."""
    return (kind == "entry" and side == 1) or (kind == "exit" and side == -1)


def adverse_pct(kind: str, side: int, expected: float, actual: float):
    """불리한 방향 슬리피지(%). **양수 = 손해**, 음수 = 기대보다 유리하게 체결.

    매수는 비싸게 사면 손해, 매도는 싸게 팔면 손해다. 부호를 통일해야 평균을 낼 수 있다 —
    raw 차이를 그냥 평균 내면 롱·숏이 상쇄돼 '슬리피지 0' 이라는 거짓말이 나온다.
    """
    if not expected or expected <= 0 or actual is None:
        return None
    diff = (actual - expected) / expected * 100.0
    return diff if is_buy(kind, side) else -diff


def build(kind: str, symbol: str, side: int, expected_price: float, expected_qty: float,
          fill, *, reason: str = None, intended_maker: bool = False,
          network: str = None, at_ms: int = None) -> dict:
    """체결 한 건 → 기록용 dict. fill 은 binance_broker.Fill."""
    price, qty = float(fill.price), float(fill.qty)
    mk, tk = float(fill.maker_qty or 0.0), float(fill.taker_qty or 0.0)
    notional = price * qty
    fee = None if fill.fee is None else float(fill.fee)
    return {
        "at": int(at_ms if at_ms is not None else (fill.ts or 0)),
        "kind": kind, "symbol": symbol, "side": "롱" if side == 1 else "숏",
        "reason": reason, "network": network,
        "expectedPrice": round(float(expected_price), 6) if expected_price else None,
        "price": round(price, 6),
        "slipPct": _r(adverse_pct(kind, side, expected_price, price), 5),
        "expectedQty": round(float(expected_qty), 8) if expected_qty else None,
        "qty": round(qty, 8),
        "makerQty": round(mk, 8), "takerQty": round(tk, 8),
        "makerRatio": _r(mk / (mk + tk) * 100.0 if (mk + tk) > 0 else None, 2),
        "intendedMaker": bool(intended_maker),      # 엔진이 maker 를 가정했는가(가정 vs 실제 대조용)
        "fee": _r(fee, 8),
        # 수수료를 명목 대비 bp 로 — maker/taker 판정의 교차검증이 된다(maker 0~2bp, taker ~5bp).
        "feeBps": _r(fee / notional * 10_000.0 if (fee is not None and notional > 0) else None, 3),
        "orders": len(fill.order_ids or []),        # 지정가 재시도 횟수(BBO 를 몇 번 쫓았나)
    }


def _r(v, nd):
    return None if v is None else round(float(v), nd)


def summary(rec: dict) -> str:
    """한 줄 요약 — 체결 직후 docker logs 에 바로 보이게."""
    slip = rec.get("slipPct")
    slip_s = "슬립 -" if slip is None else f"슬립 {slip:+.4f}%"
    mr = rec.get("makerRatio")
    mk_s = "maker -" if mr is None else f"maker {mr:.0f}%"
    fb = rec.get("feeBps")
    return (f"{'진입' if rec['kind'] == 'entry' else '청산'} {rec['side']} @{rec['price']} "
            f"(기대 {rec.get('expectedPrice')}) · {slip_s} · {mk_s}"
            + (f" · {fb:.2f}bp" if fb is not None else ""))


def record(**kw) -> dict:
    """build + append. 실패해도 예외를 올리지 않는다(체결 경로에서 부르므로 절대 막으면 안 된다)."""
    try:
        rec = build(**kw)
    except Exception:
        return {}
    jsonl_log.append(rec, DEFAULT_PATH, DEFAULT_KEEP)
    return rec


def tail(path: str = None, n: int = 50) -> list:
    return jsonl_log.tail(DEFAULT_PATH if path is None else path, n)
