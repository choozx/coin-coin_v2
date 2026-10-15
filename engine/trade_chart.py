"""매매 원장의 한 거래 → 진입~청산 구간의 캔들+지표 차트 데이터.

대시보드에서 매매기록 한 줄을 펼치면 그 거래의 진입부터 청산까지를 백테스트 캔들차트처럼
(전략에 쓰인 지표까지) 보여주기 위한 데이터를 만든다. 백테스트가 쓰는 지표추출·OHLC
헬퍼(_chart_indicators / _ohlc_for_chart)를 그대로 재사용 → 차트가 백테스트와 동일하게 나온다.

지표는 진입 전 워밍업 구간까지 계산한 뒤(EMA200·SuperTrend 등 과거 필요), 표시 구간만 잘라 반환.
"""
from __future__ import annotations

from . import candle_store
from . import ledger
from .candles import TIMEFRAME_MINUTES
from .preset import load_preset_file

MINUTE_MS = 60_000


def window(symbol: str, preset_data: dict | None, tf: str, et: int, xt: int) -> dict:
    """진입~청산 구간(양옆 여백 포함) 캔들+지표 — 신호 타임프레임 그대로, 압축 없음.

    대시보드 매매기록과 백테스트 거래 목록이 같이 쓴다. 지표는 앞쪽 워밍업까지 넣어 계산하고 표시 구간만 자른다.
    """
    tf_min = TIMEFRAME_MINUTES[tf]
    dur = max(xt - et, tf_min * MINUTE_MS)
    # 진입 전 여백 ≥80봉 — RSI 다이버전스 저점1 은 진입 최대 60봉(창) 전까지 갈 수 있다. 청산 뒤는 ≥20봉.
    pad0 = max(tf_min * 80 * MINUTE_MS, int(dur * 0.5))
    pad1 = max(tf_min * 20 * MINUTE_MS, int(dur * 0.25))
    disp0, disp1 = et - pad0, xt + pad1
    # 지표 워밍업 1000봉 — RSI(와일더) 수렴·다이버전스 상태(창 60봉)·EMA200 이 전체 백테스트와 같아지도록 넉넉히
    warm = 1000 * tf_min * MINUTE_MS

    # 양 끝을 신호 봉 경계로 — 안 맞추면 마지막 봉이 1분짜리로 잘려 종가·지표가 백테스트와 달라진다
    tf_ms = tf_min * MINUTE_MS
    disp0 = (disp0 // tf_ms) * tf_ms
    disp1 = (disp1 // tf_ms) * tf_ms
    base = candle_store.ensure(symbol, disp0 - warm, disp1 + tf_ms - MINUTE_MS)

    from .server import _chart_indicators, _ohlc_for_chart, price_decimals   # 지연 임포트(순환 회피)
    ohlc, bar_min = _ohlc_for_chart(base, tf_min)
    inds = _chart_indicators(base, tf_min, preset_data) if preset_data else []

    # 이 거래의 다이버전스 선(진입 전에 시작한 마지막 선분)이 잘리지 않게 왼쪽을 넓힌다 — 저점1 이 연쇄로 갱신되면
    # 진입보다 80봉 넘게 앞일 수 있다(DOGE 378건 중 6건). 선분 첫 점 = RSI 저점1(기준선 아래) 이라 '왜 진입했나'가 보인다.
    # 삼각형 추세선도 같은 방식(첫 피벗 → 돌파 봉). 단 진입 직전(≤ 80봉)에 끝난 선분만 — 진입과 무관한 옛 패턴으로 넓히지 않게.
    starts = []                                   # 선(가격·RSI)마다 진입 전 마지막 선분의 첫 점
    recent = et - 80 * tf_ms
    for ind in inds:
        if "다이버전스" not in ind["label"] and "추세선" not in ind["label"]:
            continue
        last, run0, run1 = None, None, None
        for tm, v in ind["data"] + [[None, None]]:
            if v is None:                         # 선분 끝 → 진입 전에 시작했고 진입 직전에 끝났으면 후보
                if run0 is not None and run0 <= et and run1 >= recent:
                    last = run0
                run0 = run1 = None
            else:
                run0 = tm if run0 is None else run0
                run1 = tm
        if last is not None:
            starts.append(last)
    if starts and min(starts) < disp0:
        disp0 = min(starts) - 10 * tf_ms
    ohlc = [b for b in ohlc if disp0 <= b[0] <= disp1]
    for ind in inds:
        d = ind["data"]
        if len(d) == 2 and d[0][1] is not None and d[0][1] == d[1][1] and ohlc:
            # 상수 기준선(예: RSI 기준 27)은 전체 기간 양끝 두 점이라 그냥 자르면 끝점 하나(점)만 남는다 → 표시 구간 양끝으로
            ind["data"] = [[ohlc[0][0], d[0][1]], [ohlc[-1][0], d[0][1]]]
        else:
            kept = [p for p in d if disp0 <= p[0] <= disp1]
            # 표시 구간 밖에서 시작한 선분이 잘려 점 하나만 남으면 화면 끝에 '–' 조각으로 보인다 → 그런 점은 뺀다
            ind["data"] = [p for i, p in enumerate(kept) if p[1] is None
                           or (i > 0 and kept[i - 1][1] is not None) or (i + 1 < len(kept) and kept[i + 1][1] is not None)]
    return {"symbol": symbol, "timeframe": tf, "ohlcBarMin": bar_min,
            "pricePrecision": price_decimals(base), "ohlc": ohlc, "indicators": inds}


def build(trade_id: int, mode: str = "paper", ledger_path: str = None) -> dict:
    """원장의 trade_id 거래에 대한 진입~청산 구간 차트 데이터."""
    rows = ledger.load(ledger_path or ledger.LEDGER_PATH, mode=mode)
    tr = next((r for r in rows if r["id"] == int(trade_id)), None)
    if tr is None:
        raise ValueError(f"거래 없음 (id={trade_id})")

    # 전략 프리셋 로드 → 타임프레임·지표. 파일이 사라졌으면 지표 없이 캔들만.
    preset = None
    try:
        preset = load_preset_file(tr["strategy"], validate=False)
    except Exception:
        preset = None
    tf = preset.timeframe if preset else "15m"
    et, xt = int(tr["entry_time"]), int(tr["exit_time"])
    out = window(tr["symbol"], preset.data if preset else None, tf, et, xt)
    out.update({
        "entry": {"time": et, "price": tr["entry_price"], "side": tr["side"]},
        "exit": {"time": xt, "price": tr["exit_price"], "reason": tr["reason"], "pnl": tr["pnl"]},
        "strategy": tr["strategy"], "leverage": tr["leverage"],
    })
    return out
