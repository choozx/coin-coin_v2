"""통합 웹 서버 (파이썬 표준 라이브러리만 사용).

    python3 -m engine.server            # http://localhost:8765 접속
    python3 -m engine.server --port 9000

라우트:  /(랜딩)=매매 대시보드(/dashboard 별칭) · /backtest=백테스트 스튜디오 · /collector=데이터·수집기 · /settings=글로벌 설정
프론트엔드: dashboard.html · gui.html · collector.html · settings.html.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backtest import BacktestConfig, run
from . import null_model
from . import binance_math as bm
from . import control
from .candles import resample, TIMEFRAME_MINUTES
from .preset import (Preset, bot_config_info, import_preset, list_strategies, save_composed_preset,
                     select_strategy, send_preset_to_deploy)
from . import ledger

_HTML = os.path.join(os.path.dirname(__file__), "gui.html")
_DASH_HTML = os.path.join(os.path.dirname(__file__), "dashboard.html")   # 매매 대시보드(같은 포트 /dashboard)
_COLLECTOR_HTML = os.path.join(os.path.dirname(__file__), "collector.html")   # 데이터·수집기 관리(/collector)
_SETTINGS_HTML = os.path.join(os.path.dirname(__file__), "settings.html")     # 글로벌 설정(/settings)
_VIDEO_HTML = os.path.join(os.path.dirname(__file__), "video.html")           # 트레이딩 영상 분석(/video)
_RANKING_HTML = os.path.join(os.path.dirname(__file__), "ranking.html")       # 백테스트 결과 랭킹(/ranking)
STATE_PATH = os.environ.get("STATE_PATH", "data/state.json")
# 차트 라이브러리(TradingView Lightweight Charts, Apache 2.0). 벤더링해서 오프라인에서도 동작.
_CHARTS_JS = os.path.join(os.path.dirname(__file__), "vendor",
                          "lightweight-charts.standalone.production.js")


def _get_candles(symbol, days):
    """수집된(캐시된) 캔들만 사용 — 백테스트 중 네트워크 수집 안 함.

    없으면 ValueError → GUI가 '데이터 수집 탭에서 먼저 수집' 안내.
    """
    from . import candle_store
    base = candle_store.load_recent(symbol, days)
    fr_rate = 0.0001
    fr = candle_store.load_funding_cached(symbol, int(base.open_time[0]), int(base.open_time[-1]))
    if fr:
        fr_rate = sum(r for _, r in fr) / len(fr)
    return base, fr_rate


def _check_other_symbols(preset_dict: dict, base) -> None:
    """조건이 다른 코인(operand.symbol)을 보면 그 코인 1분봉이 같은 기간에 있어야 한다 — 없으면 그 조건이
    조용히 늘 거짓이 되어 '거래 0건'으로만 보인다. 95% 미만이면 ValueError 로 수집을 안내한다."""
    from . import candle_store
    from .conditions import referenced_symbols
    ex = preset_dict.get("exit") or {}
    nodes = [preset_dict.get("entry"), *[r.get("when") for r in preset_dict.get("entryRules") or []],
             ex.get("condition"), *(ex.get("conditionBySide") or {}).values()]
    own = (preset_dict.get("market") or {}).get("symbol")
    s, e = int(base.open_time[0]), int(base.open_time[-1])
    for sym in sorted(referenced_symbols(*nodes) - {own}):
        have, want = candle_store.count_range(sym, s, e), len(base)
        if have < want * 0.95:
            raise ValueError(f"조건이 {sym} 를 보는데 이 기간 {sym} 1분봉이 {have / max(1, want):.0%} 만 있어요 — "
                             f"📥 데이터 수집에서 {sym} 를 먼저 받아 주세요")


def _price_target(p: dict, prefix: str):
    """폼 파라미터 → 익절/손절 블록. prefix='sl' 또는 'tp'. 'off'면 None."""
    t = p.get(prefix + "Type", "off")
    if t in ("off", "", None):
        return None
    if t in ("percent", "atrMultiple", "price", "riskReward"):
        v = float(p.get(prefix + "Value", 0) or 0)
        return {"type": t, "value": v} if v > 0 else None
    if t == "indicator":                     # 진입 시점 지표 값(EMA50 등) ± 버퍼 — 손절 전용
        d = {"type": t, "indicator": {"indicator": p.get(prefix + "Ind", "EMA") or "EMA",
                                      "period": int(p.get(prefix + "IndPeriod", 50) or 50)}}
        buf = float(p.get(prefix + "Buffer", 0) or 0)
        if buf > 0:
            d["bufferPercent"] = buf
        return d
    if t == "retrace":                       # 직전 파동의 value% 되돌림 가격 — 익절 전용
        v = float(p.get(prefix + "Value", 0) or 0)
        return {"type": t, "value": v, "lookback": int(p.get(prefix + "Lookback", 50) or 50)} if v > 0 else None
    if t in ("swing", "swingLow", "swingHigh"):
        d = {"type": t, "lookback": int(p.get(prefix + "Lookback", 20))}
        buf = float(p.get(prefix + "Buffer", 0) or 0)
        if buf > 0:
            d["bufferPercent"] = buf
        return d
    return None


def _build_preset(p: dict) -> dict:
    """폼 파라미터 → 프리셋 JSON."""
    # 진입: 그룹(AND, 방향 포함)들 → 방향별 규칙 entryRules. ((A&B)→롱) or ((C&D)→숏)
    import copy
    groups_in = p.get("entryGroups") or []
    # 하위호환: [[node...]] (방향 없음) → 전부 long 그룹으로
    groups = []
    for g in groups_in:
        if isinstance(g, dict):
            groups.append({"side": g.get("side", "long"), "conds": copy.deepcopy(g.get("conditions") or []),
                           "scale": float(g.get("sizeScale") or 1)})
        elif isinstance(g, list) and g:
            groups.append({"side": "long", "conds": copy.deepcopy(g)})
    groups = [g for g in groups if g["conds"]]

    # 최적화 진입 파라미터 오버라이드: 키 "@entry:gi:ci:dotted.path" → 해당 조건 노드에 값 설정
    for key, val in p.items():
        if not (isinstance(key, str) and key.startswith("@entry:")):
            continue
        try:
            _, gi, ci, path = key.split(":", 3)
            node = groups[int(gi)]["conds"][int(ci)]
        except (ValueError, IndexError, KeyError):
            continue
        d, parts = node, path.split(".")
        for k in parts[:-1]:
            d = d[int(k)] if isinstance(d, list) else d.setdefault(k, {})
        d[int(parts[-1]) if isinstance(d, list) else parts[-1]] = val

    if not groups:
        raise ValueError("진입 조건을 하나 이상 추가해줘 (진입 섹션에서 조건 추가)")

    def _grp(nodes):
        return nodes[0] if len(nodes) == 1 else {"op": "AND", "children": nodes}
    entry_rules = [{"side": g["side"], "when": _grp(g["conds"]),
                    **({"sizeScale": g["scale"]} if g.get("scale", 1) != 1 else {})} for g in groups]
    # entry(폴백/스키마용) = 모든 when을 OR
    whens = [r["when"] for r in entry_rules]
    entry = whens[0] if len(whens) == 1 else {"op": "OR", "children": whens}
    sides = {g["side"] for g in groups}
    market_direction = sides.pop() if len(sides) == 1 else "both"

    exit_block = {}
    sl = _price_target(p, "sl")
    if sl:
        exit_block["stopLoss"] = sl
    tp = _price_target(p, "tp")
    if tp:
        exit_block["takeProfit"] = tp
    # SuperTrend 전환 청산 — 익절/손절 슬롯 또는 지표청산 체크박스 (같은 트리거, 사유 라벨만 다름)
    if p.get("tpType") == "supertrend":
        exit_block["supertrendExit"] = {
            "period": int(p.get("tpStPeriod", 10) or 10),
            "multiplier": float(p.get("tpStMult", 3.0) or 3.0),
            "as": "takeProfit",
        }
    elif p.get("slType") == "supertrend":
        exit_block["supertrendExit"] = {
            "period": int(p.get("slStPeriod", 10) or 10),
            "multiplier": float(p.get("slStMult", 3.0) or 3.0),
            "as": "stopLoss",
        }
    elif p.get("exitStEnabled"):
        exit_block["supertrendExit"] = {
            "period": int(p.get("exitStPeriod", 10) or 10),
            "multiplier": float(p.get("exitStMult", 3.0) or 3.0),
            "as": "exit",
        }
    if p.get("trailingEnabled") and float(p.get("trailingCallback", 0)) > 0:
        exit_block["trailing"] = {
            "enabled": True,
            "callbackPercent": float(p["trailingCallback"]),
            "activationPercent": float(p.get("trailingActivation", 0)),
        }
    if p.get("beEnabled") and float(p.get("beValue", 0) or 0) > 0:
        exit_block["breakeven"] = {
            "type": p.get("beType") or "riskReward",
            "value": float(p["beValue"]),
            "offsetPercent": float(p.get("beOffset", 0) or 0),
        }
        if exit_block["breakeven"]["type"] == "retrace":
            exit_block["breakeven"]["lookback"] = int(p.get("beLookback", 50) or 50)
    # 지표 조건 청산 (지표 평균 복귀) — 역추세 매매의 핵심 청산. 지표는 선택 가능(RSI/Stoch 등)
    if p.get("exitCondEnabled"):
        exit_block["condition"] = {
            "left": {"indicator": p.get("exitCondInd", "RSI"), "period": int(p.get("exitCondPeriod", 14))},
            "cmp": p.get("exitCondCmp", ">"),
            "right": float(p["exitCondValue"]),
        }
    # 직접 청산 조건(고급) — 화면 'exitCustom' 칸(JSON). {"long": 조건, "short": 조건} — 한쪽만 있어도 된다.
    # 영상 분석 프리셋처럼 지표 복귀 청산(RSI 등 4종)으로 안 되는 조건(종가<SMA100, RSI 하향 교차 50 …)을 담는다.
    # 지표 복귀 청산도 켜져 있으면 방향마다 OR 로 합친다(engine 은 conditionBySide 가 있으면 condition 을 안 본다).
    xc = p.get("exitCustom")
    if isinstance(xc, str):
        xc = xc.strip()
    if xc:
        try:
            d = json.loads(xc) if isinstance(xc, str) else xc
        except json.JSONDecodeError as e:
            raise ValueError(f"직접 청산 조건 JSON 오류: {e}")
        if not isinstance(d, dict) or not ({"long", "short"} & set(d)):
            raise ValueError('직접 청산 조건은 {"long": 조건, "short": 조건} 형태여야 해요')
        common = exit_block.pop("condition", None)
        by = {}
        for side in ("long", "short"):
            nodes = [n for n in (d.get(side), common) if n]
            if nodes:
                by[side] = nodes[0] if len(nodes) == 1 else {"op": "OR", "children": nodes}
        if by:
            exit_block["conditionBySide"] = by
    # 시간 청산
    if int(p.get("timeStopBars", 0) or 0) > 0:
        exit_block["timeStop"] = {"maxBars": int(p["timeStopBars"])}

    sizing = {
        "leverage": int(p["leverage"]),
        "marginMode": "isolated",
        "size": {"type": p.get("sizeType", "equityPercent"), "value": float(p["sizeValue"])},
    }
    if float(p.get("minLiqBuffer", 0)) > 0:
        sizing["minLiquidationBuffer"] = float(p["minLiqBuffer"])
    if p.get("levTierEnabled") and p.get("levTiers"):       # 잔고별 레버리지 티어
        tiers = []
        for t in p["levTiers"]:
            row = {"leverage": int(t["leverage"])}
            mb = t.get("maxBalance")
            if mb not in (None, ""):
                row["maxBalance"] = float(mb)
            tiers.append(row)
        tiers.sort(key=lambda x: x.get("maxBalance", float("inf")))   # 오름차순, ∞(상한없음) 마지막
        if tiers:
            sizing["leverageTiers"] = tiers

    filt = {}
    if int(p.get("cooldownBars", 0)) > 0:
        filt["cooldownBars"] = int(p["cooldownBars"])
    if int(p.get("avoidFundingMin", 0)) > 0:
        filt["avoidFundingWindowMinutes"] = int(p["avoidFundingMin"])

    preset = {
        "schemaVersion": "1.0",
        "name": p.get("name", "GUI 프리셋"),
        "market": {"exchange": "binance-futures", "symbol": p["symbol"],
                   "timeframe": p["timeframe"], "direction": market_direction},
        "entry": entry,
        "entryRules": entry_rules,
        "exit": exit_block,
        "sizing": sizing,
    }
    if p.get("entryType") == "makerLimit":       # 지정가 maker 진입
        execution = {"entryType": "makerLimit"}
        to = p.get("makerTimeoutSeconds")
        if to not in (None, ""):                 # passive-then-aggressive (미체결 시 taker 추격)
            execution["makerTimeoutSeconds"] = float(to)
        preset["execution"] = execution
    if filt:
        preset["filter"] = filt
    return preset


def _downsample(curve, n=400):
    if len(curve) <= n:
        return curve
    step = len(curve) / n
    return [curve[int(i * step)] for i in range(n)] + [curve[-1]]


def price_decimals(base) -> int:
    """차트·거래 목록에 보낼 가격 소수 자릿수 — 최고가 기준 유효숫자 6자리, 최소 2.

    BTC(~10만) 2 · DOGE(~0.4) 7 · 1000PEPE(~0.02) 8. 고정 2자리였을 땐 DOGE 캔들이 0.12·0.13 계단으로 뭉갰다.
    """
    import math
    import numpy as np
    mx = float(np.nanmax(base.close)) if len(base) else 0.0
    if not (mx > 0):
        return 2
    return max(2, 6 - int(math.floor(math.log10(mx))))


def _ohlc_for_chart(base, tf_min: int, max_bars: int = 200000):
    """차트용 캔들스틱 OHLC. 백테스트 타임프레임 그대로 리샘플(무압축)해 반환하되,
    max_bars 초과 시에만 연속 봉을 묶어 압축(응답 크기 방어).
    open=첫 open, high=max, low=min, close=마지막 close."""
    import math
    cs = resample(base, tf_min)
    n = len(cs)
    k = 1 if n <= max_bars else math.ceil(n / max_bars)
    dp = price_decimals(base)
    out = []
    for s in range(0, n, k):
        e = min(s + k, n)
        out.append([
            int(cs.open_time[s]),
            round(float(cs.open[s]), dp),
            round(float(cs.high[s:e].max()), dp),
            round(float(cs.low[s:e].min()), dp),
            round(float(cs.close[e - 1]), dp),
        ])
    return out, (k * tf_min)   # (캔들목록, 캔들 1개가 나타내는 분)


# ---- 전략에 쓰인 지표 추출·계산 (차트 표시용) ----
_OVERLAY_INDS = {"SMA", "EMA", "VWAP", "SUPERTREND", "BB_upper", "BB_mid", "BB_lower", "BOX_HIGH", "BOX_LOW",
                 "PREV_HIGH", "PREV_LOW", "PIVOT_LOW", "PIVOT_HIGH", "TRENDLINE_HIGH", "TRENDLINE_LOW"}
# 오실레이터 → 패널 그룹키 (같은 그룹은 한 패널에 겹쳐 그림)
_IND_PANE = {
    "RSI": "rsi", "MACD": "macd", "MACD_signal": "macd", "MACD_hist": "macd",
    "ATR": "atr", "STOCH_K": "stoch", "STOCH_D": "stoch",
    "STOCHRSI_K": "stochrsi", "STOCHRSI_D": "stochrsi", "CCI": "cci", "MFI": "mfi",
    "RVOL": "rvol", "TAKER_DELTA": "taker", "TAKER_DELTA_RATIO": "taker",
    "CVD": "cvd", "CVD_EMA": "cvd", "HAWKEYE": "hawkeye",
    "QQE_MOD": "qqe", "QQE_RSI": "qqe", "QQE_LINE": "qqe", "SUPERTREND_DIR": "st_dir",
    "RSI_DIV": "rsi_div", "ATR_RANK": "atr_rank", "MA_ANGLE": "ma_angle", "MA_CROSSES": "ma_crosses",
}
_IND_COLOR = {
    "EMA": "#e8a13a", "SMA": "#c77dff", "VWAP": "#4cc9f0", "SUPERTREND": "#26d07c",
    "BB_upper": "#8a90a0", "BB_mid": "#6c7080", "BB_lower": "#8a90a0",
    "RSI": "#e8a13a", "MACD": "#4cc9f0", "MACD_signal": "#f0708a", "MACD_hist": "#6c7080",
    "ATR": "#c77dff", "STOCH_K": "#4cc9f0", "STOCH_D": "#f0708a",
    "STOCHRSI_K": "#4cc9f0", "STOCHRSI_D": "#f0708a", "CCI": "#e8a13a", "MFI": "#3aa76d",
    "RVOL": "#c77dff", "TAKER_DELTA": "#4cc9f0", "TAKER_DELTA_RATIO": "#4cc9f0",
    "CVD": "#26d07c", "CVD_EMA": "#f0708a", "HAWKEYE": "#c77dff",
    "QQE_MOD": "#4cc9f0", "QQE_RSI": "#e8a13a", "QQE_LINE": "#f0708a", "RSI_DIV": "#eb6834", "ATR_RANK": "#c77dff",
}
_IND_NAME = {
    "SUPERTREND": "SuperTrend", "BB_upper": "BB상단", "BB_mid": "BB중심", "BB_lower": "BB하단",
    "MACD_signal": "MACD signal", "MACD_hist": "MACD hist", "STOCH_K": "Stoch %K",
    "STOCH_D": "Stoch %D", "STOCHRSI_K": "StochRSI %K", "STOCHRSI_D": "StochRSI %D",
    "TAKER_DELTA_RATIO": "테이커델타비율", "TAKER_DELTA": "테이커델타", "CVD_EMA": "CVD EMA",
    "QQE_MOD": "QQE MOD", "QQE_RSI": "QQE RSI", "QQE_LINE": "QQE 라인", "HAWKEYE": "HawkEye",
    "RSI_DIV": "RSI 다이버전스", "ATR_RANK": "변동성 순위", "MA_ANGLE": "이평 각도", "MA_CROSSES": "이평 교차 횟수",
    "MA_DIST": "이평 괴리(ATR)", "RETRACE_UP": "반등 되돌림%", "RETRACE_DN": "눌림 되돌림%",
    "TRENDLINE_HIGH": "고점 추세선", "TRENDLINE_LOW": "저점 추세선",
}


def _ind_label(op: dict) -> str:
    name = op["indicator"]
    disp = _IND_NAME.get(name, name)
    pr = op.get("params") or {}
    parts = []
    if op.get("period"):
        parts.append(str(op["period"]))
    if "multiplier" in pr:
        parts.append("×" + str(pr["multiplier"]))
    if "stddev" in pr:
        parts.append("σ" + str(pr["stddev"]))
    if "fast" in pr:
        parts.append(f"{pr.get('fast')}/{pr.get('slow')}/{pr.get('signal')}")
    lab = f"{disp}({','.join(parts)})" if parts else disp
    if pr.get("htf"):
        from .conditions import _tf_label
        lab += f" {_tf_label(int(pr['htf']))}"
    return f"{op['symbol']} {lab}" if op.get("symbol") else lab


def _chart_indicators(base, tf_min: int, preset_dict: dict, max_bars: int = 200000):
    """프리셋 조건 트리에 쓰인 지표를 신호 TF에서 계산해 차트 봉에 정렬한 시계열로 반환.

    반환: [{label, overlay, pane, color, data:[[time_ms, value|None], ...]}, ...]
    - overlay=True(가격 스케일)는 캔들 위, False(오실레이터)는 pane 그룹별 하단 패널.
    - SUPERTREND_DIR(±1)은 라인이 더 유용하므로 SUPERTREND 라인 오버레이로 치환.
    """
    import math
    from .conditions import SeriesResolver
    cs = resample(base, tf_min)
    n = len(cs)
    if n == 0:
        return []
    dv = max(4, price_decimals(base))          # 가격 스케일 지표(SMA·밴드 등)도 싼 코인에서 안 뭉개지게
    k = 1 if n <= max_bars else math.ceil(n / max_bars)
    resolver = SeriesResolver(cs, (preset_dict.get("market") or {}).get("symbol"))

    operands = []
    div_sides: dict = {}

    def walk(node):
        if not isinstance(node, dict):
            return
        if "op" in node:
            for ch in node.get("children") or []:
                walk(ch)
            return
        for sd in ("left", "right"):
            op = node.get(sd)
            if isinstance(op, dict) and "indicator" in op:
                operands.append(op)
        lo = node.get("left")
        if isinstance(lo, dict) and lo.get("indicator") == "RSI_DIV":     # 조건이 쓰는 방향만 그린다
            up = node.get("cmp") in (">", ">=") or node.get("cross") == "crossOver"
            div_sides.setdefault(json.dumps(lo, sort_keys=True), set()).add(1 if up else -1)

    for r in preset_dict.get("entryRules") or []:
        walk(r.get("when"))
    if not preset_dict.get("entryRules"):
        walk(preset_dict.get("entry"))
    ex = preset_dict.get("exit") or {}
    walk(ex.get("condition"))
    st = ex.get("supertrendExit")
    if st:   # 청산에 쓴 SuperTrend 라인도 오버레이
        operands.append({"indicator": "SUPERTREND", "period": st.get("period", 10),
                         "params": {"multiplier": st.get("multiplier", 3.0)}})

    # SUPERTREND_DIR → SUPERTREND 라인으로 치환, 중복 제거
    seen, uniq = set(), []
    for op in operands:
        if op.get("indicator") == "SUPERTREND_DIR":
            op = {"indicator": "SUPERTREND", "period": op.get("period", 10),
                  "params": op.get("params") or {"multiplier": 3.0}}
        key = json.dumps(op, sort_keys=True)
        if key not in seen:
            seen.add(key)
            uniq.append(op)

    def subsample(series):
        out = []
        for s in range(0, n, k):
            v = series[min(s + k, n) - 1]
            out.append([int(cs.open_time[s]),
                        None if (v is None or (isinstance(v, float) and math.isnan(v))) else round(float(v), dv)])
        return out

    result = []
    for op in uniq:
        name = op["indicator"]
        if name == "SUPERTREND":
            # 상승/하락 구간을 색이 다른 두 선으로 분리, 상대 구간은 nan→끊김(전환점 연결선 제거).
            import numpy as np
            from . import indicators as ind
            per = op.get("period") or 10
            mult = float((op.get("params") or {}).get("multiplier", 3.0))
            line, d = ind.supertrend(cs.high, cs.low, cs.close, per, mult)
            lbl = _ind_label(op)
            result.append({"label": lbl + " ↑상승", "overlay": True, "pane": "price",
                           "color": "#26d07c", "data": subsample(np.where(d > 0, line, np.nan))})
            result.append({"label": lbl + " ↓하락", "overlay": True, "pane": "price",
                           "color": "#cf5b5b", "data": subsample(np.where(d < 0, line, np.nan))})
            continue
        if name == "HAWKEYE":
            # 원본: 거래량 막대를 상태색으로(강세 초록/약세 빨강/중립 회색). 히스토그램.
            import numpy as np
            from . import indicators as ind
            per = op.get("period") or 200
            div = float((op.get("params") or {}).get("divisor", 3.6))
            state = ind.hawkeye(cs.high, cs.low, cs.close, cs.volume, per, div)
            data = []
            for s in range(0, n, k):
                i = min(s + k, n) - 1
                sv = state[i]
                if sv is None or (isinstance(sv, float) and math.isnan(sv)):
                    data.append([int(cs.open_time[s]), None])
                else:
                    color = "#26d07c" if sv > 0 else "#cf5b5b" if sv < 0 else "#6c7080"
                    data.append([int(cs.open_time[s]), round(float(cs.volume[i]), 4), color])
            result.append({"label": _ind_label(op) + " 볼륨", "overlay": False, "pane": "hawkeye",
                           "type": "histogram", "color": "#6c7080", "data": data})
            continue
        if name == "QQE_MOD":
            # 원본: secondaryRSI-50 컬럼(막대) + 신호색(매수 파랑/매도 빨강/중립 회색) + 보조 트렌드라인.
            import talib
            from . import indicators as ind
            pr = op.get("params") or {}
            rl = op.get("period") or int(pr.get("rsi_length", 6))
            sm = int(pr.get("smoothing", 5))
            fp = float(pr.get("factor_primary", 3.0)); fs = float(pr.get("factor_secondary", 1.61))
            thr = float(pr.get("threshold", 3.0)); bbl = int(pr.get("bb_length", 50)); bbm = float(pr.get("bb_mult", 0.35))
            line_p, rsi_p = ind.qqe(cs.close, rl, sm, fp)
            line_s, rsi_s = ind.qqe(cs.close, rl, sm, fs)
            basis = talib.SMA(line_p - 50.0, bbl)
            dev = bbm * talib.STDDEV(line_p - 50.0, bbl, nbdev=1)
            upper, lower = basis + dev, basis - dev
            rp, rs = rsi_p - 50.0, rsi_s - 50.0
            hist, ln = [], []
            for s in range(0, n, k):
                i = min(s + k, n) - 1
                t = int(cs.open_time[s])
                if math.isnan(rs[i]):
                    hist.append([t, None])
                else:
                    if not math.isnan(upper[i]) and rs[i] > thr and rp[i] > upper[i]:
                        c = "#00c3ff"
                    elif not math.isnan(lower[i]) and rs[i] < -thr and rp[i] < lower[i]:
                        c = "#ff0062"
                    else:
                        c = "#707070"
                    hist.append([t, round(float(rs[i]), 4), c])
                lv = line_s[i] - 50.0
                ln.append([t, None if math.isnan(lv) else round(float(lv), 4)])
            result.append({"label": "QQE MOD 히스토그램", "overlay": False, "pane": "qqe",
                           "type": "histogram", "color": "#707070", "data": hist})
            result.append({"label": "QQE 라인(보조)", "overlay": False, "pane": "qqe",
                           "type": "line", "color": "#d0d0d0", "data": ln})
            continue
        if name in ("FRACTAL", "PULLBACK_FRACTAL"):
            # 프랙탈은 '중심 봉 → 확정 봉' 짧은 가로선(그 저점/고점 가격)으로 — 어디서 생겼고 언제 확정됐는지 보인다.
            # 눌림 조건은 기준 이평도 같이 그린다.
            from . import indicators as ind
            pr = op.get("params") or {}
            if name == "PULLBACK_FRACTAL":
                nf, per = int(pr.get("n", 2)), op.get("period") or 20
                ma_nm = "SMA" if pr.get("sma") else "EMA"
                ma = (ind.sma if ma_nm == "SMA" else ind.ema)(cs.close, per)
                result.append({"label": f"{ma_nm}({per})", "overlay": True, "pane": "price",
                               "color": "#e8a13a", "data": subsample(ma)})
            else:
                nf = int(op.get("period") or 2)
            lows, highs = ind.fractal_points(cs.high, cs.low, nf)
            t_of = lambda i: int(cs.open_time[(i // k) * k])
            for idxs, arr, lab, color in ((lows, cs.low, "프랙탈 저점", "#ff5c6c"), (highs, cs.high, "프랙탈 고점", "#4c9be8")):
                pts = []
                for c0 in idxs.tolist():
                    if c0 + nf < n and t_of(c0) < t_of(c0 + nf):
                        v = round(float(arr[c0]), dv)
                        pts += [[t_of(c0), v], [t_of(c0 + nf), v], [t_of(c0 + nf) + 1, None]]
                if pts:
                    result.append({"label": lab, "overlay": True, "pane": "price", "color": color, "data": pts})
            continue
        if name == "PIN_BAR":
            continue                       # 봉 모양 그 자체라 캔들에서 보인다 — ±1 신호를 선으로 그리면 바늘뿐
        if name == "TRIANGLE_BREAK":
            # 돌파마다 그 삼각형의 위·아래 추세선(첫 피벗 → 돌파 봉까지 연장)을 가격 차트에. 선분은 [점1, 점2, 끊기].
            from . import indicators as ind
            pr = op.get("params") or {}
            evs = ind._triangle_events(cs.high, cs.low, cs.close, op.get("period") or 5,
                                       int(pr.get("lookback", 100)), float(pr.get("tol", 10)))
            t_of = lambda i: int(cs.open_time[(i // k) * k])
            up, dn = [], []
            for t, _side, (i1, h1, _i2, _h2), (j1, l1, _j2, _l2), U, L in evs:
                up += [[t_of(i1), round(float(h1), dv)], [t_of(t), round(float(U), dv)], [t_of(t) + 1, None]]
                dn += [[t_of(j1), round(float(l1), dv)], [t_of(t), round(float(L), dv)], [t_of(t) + 1, None]]
            if up:
                result.append({"label": "삼각형 위 추세선", "overlay": True, "pane": "price", "color": "#4cc9f0", "data": up})
                result.append({"label": "삼각형 아래 추세선", "overlay": True, "pane": "price", "color": "#f0a04c", "data": dn})
            continue
        if name == "RSI_DIV":
            # 신호값(+1/0/-1)을 선으로 그리면 0 에 붙은 평선 + 바늘이라 읽을 수 없다.
            # 대신 RSI 선 · 기준선 · 다이버전스를 이루는 두 저점(약세는 고점)을 잇는 선을 그린다 —
            # 가격 차트(오버레이)와 RSI 패널 양쪽에. 선분은 [점1, 점2, 끊기(None)] 로 보낸다(GUI 가 None 에서 끊는다).
            from . import indicators as ind
            pr = op.get("params") or {}
            per = op.get("period") or 14
            lvl = float(pr.get("level", 30))
            cvd_mode = int(pr.get("cvd", 0)) if cs.taker_buy is not None else 0
            segs = ind.rsi_divergence_segments(cs.high, cs.low, cs.close, per, lvl, int(pr.get("min_gap", 5)),
                                               int(pr.get("window", 60)), float(pr.get("strength", 0)),
                                               cs.volume, cs.taker_buy, cvd_mode)
            if int(pr.get("cvd", 0)) and cs.taker_buy is None:
                segs = []                                    # 조건이 항상 false 인 것과 맞춘다
            r = ind.rsi(cs.close, per)
            t_of = lambda i: int(cs.open_time[(i // k) * k])
            last_t = int(cs.open_time[((n - 1) // k) * k])
            result.append({"label": f"RSI({per})", "overlay": False, "pane": "rsi_div",
                           "color": "#e8a13a", "data": subsample(r)})
            sides = div_sides.get(json.dumps(op, sort_keys=True), {1, -1})
            for v in ([lvl] if 1 in sides else []) + ([100.0 - lvl] if -1 in sides else []):
                result.append({"label": f"기준 {v:g}", "overlay": False, "pane": "rsi_div", "color": "#6c7080",
                               "data": [[int(cs.open_time[0]), v], [last_t, v]]})
            for side, color, nm in ((1, "#eb6834", "강세"), (-1, "#c77dff", "약세")):
                if side not in sides:
                    continue
                pts = {"price": [], "rsi": []}
                prev_end = -1
                for d in segs:
                    if d["side"] != side:
                        continue
                    (a0, _), (a1, _) = d["price"]
                    (b0, _), _ = d["rsi"]
                    t0, t1 = t_of(min(a0, b0)), t_of(a1)
                    if t0 <= prev_end or t_of(a0) >= t1 or t_of(b0) >= t1:
                        continue                                 # 축약(k>1)으로 겹치면 생략
                    for key in ("price", "rsi"):
                        (i0, v0), (i1, v1) = d[key]
                        pts[key] += [[t_of(i0), round(v0, dv)], [t_of(i1), round(v1, dv)], [t1 + 1, None]]
                    prev_end = t1 + 1
                if not pts["price"]:
                    continue
                result.append({"label": f"{nm} 다이버전스(가격)", "overlay": True, "pane": "price",
                               "color": color, "data": pts["price"]})
                result.append({"label": f"{nm} 다이버전스(RSI)", "overlay": False, "pane": "rsi_div",
                               "color": color, "data": pts["rsi"]})
            continue
        try:
            series = resolver.resolve(op)
        except Exception:
            continue
        overlay = name in _OVERLAY_INDS and not op.get("symbol")     # 다른 심볼 가격선은 이 차트 축척이 아니다
        result.append({
            "label": _ind_label(op),
            "overlay": overlay,
            "pane": "price" if overlay else _IND_PANE.get(name, name.lower()),
            "color": _IND_COLOR.get(name, "#8a90a0"),
            "data": subsample(series),
        })
    # 같은 선이 두 조건에서 나오면 한 번만(예: 정배열 EMA20 과 눌림 조건의 EMA20)
    seen_lbl, uniq_res = set(), []
    for r in result:
        key = (r["label"], r["pane"])
        if r.get("overlay") and key in seen_lbl:
            continue
        seen_lbl.add(key)
        uniq_res.append(r)
    result = uniq_res
    # 가격 위 이평이 여럿이면 기간 짧은 순으로 색을 나눈다 — 전부 같은 주황이면 정배열을 못 읽는다
    _MA_COLORS = ["#e8a13a", "#4cc9f0", "#c77dff", "#26d07c", "#f0708a"]
    import re as _re
    mas = [r for r in result if r.get("overlay") and _re.match(r"^(EMA|SMA)\((\d+)\)$", r["label"])]
    for i, r in enumerate(sorted(mas, key=lambda r: int(_re.match(r"^\w+\((\d+)\)$", r["label"]).group(1)))):
        r["color"] = _MA_COLORS[i % len(_MA_COLORS)]
    return result


def _run_backtest(p: dict, emit=None) -> dict:
    """백테스트 한 번. emit 이 있으면 진행 이벤트를 흘린다(/api/backtest_stream — 화면 진행률 바).

    전체 진행률(pct) 배분: 캔들 0~5 · 백테스트 5~85 · 랜덤 판정 85~95 · 차트 95~100.
    """
    def stage(pct, text):
        if emit:
            emit({"type": "progress", "pct": pct, "text": text})

    stage(0, "캔들 불러오는 중")
    base, fr_rate = _get_candles(p["symbol"], float(p["days"]))
    preset_dict = _build_preset(p)
    preset = Preset.from_dict(preset_dict, validate=True)  # 스키마 검증
    _check_other_symbols(preset_dict, base)
    maker_fee, taker_fee = bm.fees_for_symbol(p["symbol"])
    cfg = BacktestConfig(initial_equity=float(p["equity"]), funding_rate=fr_rate,
                         maker_fee=maker_fee, taker_fee=taker_fee)
    span_d = (int(base.open_time[-1]) - int(base.open_time[0])) / 86_400_000
    run_text = f"백테스트 중 · 1분봉 {len(base):,}개 ({span_d:.0f}일)"
    stage(5, run_text)
    last = {"pct": -1}

    def on_progress(done, total):
        pct = 5 + int(80 * done / max(1, total))
        if pct != last["pct"]:
            last["pct"] = pct
            stage(pct, run_text)

    m = run(base, preset, cfg, progress_cb=on_progress if emit else None)
    stage(85, f"랜덤 진입 2,000번과 비교 중 · 거래 {m.num_trades}건")
    # '우연보다 나은가' — 같은 거래들(보유·방향 그대로)을 아무 시각에나 넣은 2000번과 비교.
    # 수익률만 보여주면 좋아 보이는 전략이 대부분 이 문턱 아래였다(research/BACKLOG). 최적화기와 같은 함수.
    try:
        verdict = null_model.judge(m, base, p["timeframe"], cfg.initial_equity, taker_fee=cfg.taker_fee)
    except Exception as e:            # 판정 실패가 백테스트 결과를 못 내게 하면 안 된다
        verdict = {"error": f"{type(e).__name__}: {e}"}

    from collections import Counter
    import math
    reasons = Counter(t.exit_reason for t in m.trades)

    dp = price_decimals(base)

    def _px(x):   # nan → None, 그 외 round
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), dp)

    # 전체 캔들 차트는 안 보낸다 — 거래 목록에서 한 건을 누르면 /api/bt_trade_chart 가 그 구간만 신호 TF 그대로 그린다.
    # (2000일 5m 를 한 차트에 넣으면 20만 봉 상한에 걸려 4봉씩 묶였고, 묶음 안 RSI 저점이 안 보였다.)
    stage(95, "결과 정리 중")
    trades_out = [{
        "side": t.side,
        "entryTime": int(t.entry_time), "entryPrice": round(float(t.entry_price), dp),
        "exitTime": int(t.exit_time), "exitPrice": round(float(t.exit_price), dp),
        "stop": _px(t.stop_price), "tp": _px(t.tp_price),
        "qty": float(t.qty), "leverage": int(t.leverage),
        "fees": round(float(t.fees), 2), "funding": round(float(t.funding), 2),
        "pnl": round(float(t.pnl), 2), "reason": t.exit_reason,
    } for t in m.trades]
    return {
        "preset": preset_dict,
        "dataRange": [int(base.open_time[0]), int(base.open_time[-1])],
        "candles": len(base),
        "fundingRate": fr_rate,
        "makerFee": cfg.maker_fee,
        "takerFee": cfg.taker_fee,
        "pricePrecision": dp,
        "trades": trades_out,
        "metrics": {
            "totalReturnPct": round(m.total_return_pct, 3),
            "initialEquity": m.initial_equity,
            "finalEquity": round(m.final_equity, 2),
            "numTrades": m.num_trades,
            "wins": len(m.wins), "losses": len(m.losses),
            "winRatePct": round(m.win_rate_pct, 1),
            "profitFactor": round(m.profit_factor, 3) if m.profit_factor != float("inf") else None,
            "maxDrawdownPct": round(m.max_drawdown_pct, 2),
            "sharpe": round(m.sharpe(), 2),
            "numLiquidations": m.num_liquidations,
            "totalFunding": round(m.total_funding, 2),
            "totalFees": round(m.total_fees, 2),
        },
        "exitReasons": dict(reasons),
        "verdict": verdict,
        # 같은 기간 그냥 들고만 있었으면(1배·수수료 없음) — 판정 패널의 '보유 대비' 비교용
        "buyHoldPct": round((float(base.close[-1]) / float(base.close[0]) - 1) * 100, 2),
        "equityCurve": _downsample([[t, round(e, 2)] for t, e in m.equity_curve]),
    }


def _run_backtest_ranked(p: dict, emit=None) -> dict:
    """화면 백테스트 = 실행 + 랭킹 자동 등록(engine/ranking.py). 등록 실패가 결과를 막지는 않는다."""
    res = _run_backtest(p, emit)
    try:
        from . import ranking
        res["rankingKey"] = ranking.record(p, res)
        if res["rankingKey"] is None:
            res["rankingSkipped"] = f"기간 {ranking.MIN_DAYS}일(1년) 미만이라 랭킹에 등록하지 않았어요"
    except Exception as e:
        print(f"  [랭킹 등록 실패] {type(e).__name__}: {e}", flush=True)
    return res


def _bt_trade_chart(p: dict) -> dict:
    """백테스트 거래 한 건 → 진입~청산 구간 캔들+지표(신호 TF 그대로). p = {preset, entryTime, exitTime}."""
    from . import trade_chart
    pr = p["preset"]
    mk = pr["market"]
    return trade_chart.window(mk["symbol"], pr, mk["timeframe"], int(p["entryTime"]), int(p["exitTime"]))


def _run_optimize(p: dict, emit) -> None:
    """최적화를 NDJSON 스트리밍으로 진행 — emit(dict)이 한 줄씩 클라이언트로 흘려보냄.

    이벤트: start → combo(진행/완료 조합, done/total) … → finalizing → done(최종 순위표).
    """
    from . import optimize as opt
    base, fr_rate = _get_candles(p["symbol"], float(p["days"]))
    maker_fee, taker_fee = bm.fees_for_symbol(p["symbol"])
    cfg = BacktestConfig(initial_equity=float(p["equity"]), funding_rate=fr_rate,
                         maker_fee=maker_fee, taker_fee=taker_fee)
    sweep = p.get("sweep", {})              # {param: {min,max,step}}
    try:
        _check_other_symbols(_build_preset(p), base)
    except ValueError as e:
        emit({"type": "error", "error": str(e)}); return
    if not sweep:
        emit({"type": "error", "error": "탐색할 파라미터를 하나 이상 체크해줘"})
        return

    names = list(sweep.keys())
    value_lists = [opt.expand_spec(sweep[n]) for n in names]
    total = 1
    for vl in value_lists:
        total *= len(vl)
    workers = opt.resolve_workers(min(total, 3000))
    # AI 최적화는 같은 프리셋에 반복될수록 다중검정이 쌓인다 → 프리셋별 누적 조합 수로 귀무 게이트를 건다
    gate_n = None
    if p.get("aiOpt"):
        from . import ai_optimize
        gate_n = ai_optimize.tested(p.get("presetName")) + min(total, 3000)
    emit({"type": "start", "total": total, "names": names, "workers": workers, "gateCombos": gate_n})

    use_oos = bool(p.get("useOOS", True))
    seen = {"n": 0}

    def on_combo(done, tot, row):
        seen["n"] = done
        ev = {"type": "combo", "done": done, "total": tot, "passed": row is not None}
        if row is not None:
            ev["params"] = row["params"]
            ev["is"] = row["is"]
            ev["obj"] = row["obj"]
        emit(ev)

    result = opt.optimize(
        base, _build_preset, p, sweep, cfg,
        objective=p.get("objective", "calmar"),
        min_trades=int(p.get("minTrades", 15)),
        is_frac=0.7 if use_oos else 1.0,
        progress_cb=on_combo,
        gate_combos=gate_n,
    )
    if gate_n is not None:
        result["gateCombos"] = ai_optimize.add_tested(p.get("presetName"), result["evaluated"])
    result["rankingRegistered"] = _register_range_top(p, result, base, emit)
    result["type"] = "done"
    emit(result)


RANGE_RANK_TOP = 5     # 범위 테스트 상위 몇 개를 전체 기간으로 다시 돌려 랭킹에 올리나


def _register_range_top(p: dict, result: dict, base, emit) -> list:
    """범위 테스트 상위 조합을 **전체 기간** 백테스트로 다시 돌려 랭킹에 등록 — 일반 백테스트와 같은 기준으로 비교되게.
    조합 순위는 IS(앞 70%) 목적함수 기준이라 그 수익을 그대로 올리면 기간이 달라 비교가 안 된다.
    'N조합 중 k위'를 같이 남겨 고른 결과(운이 섞임)임이 보이게 한다. 1년 미만 기간이면 돌리지 않는다."""
    from . import ranking
    top = (result.get("top") or [])[:RANGE_RANK_TOP]
    if not top:
        return []
    span_days = (int(base.open_time[-1]) - int(base.open_time[0])) / 86_400_000
    if round(span_days) < ranking.MIN_DAYS:
        return [{"skipped": f"기간 {ranking.MIN_DAYS}일(1년) 미만이라 랭킹에 등록하지 않았어요"}]
    out = []
    for k, row in enumerate(top, 1):
        emit({"type": "ranking", "text": f"랭킹 등록 — 상위 {k}/{len(top)}위 조합을 전체 기간으로 다시 백테스트 중"})
        try:
            pf = {**p, **row["params"]}
            res = _run_backtest(pf)
            key = ranking.record(pf, res, source={"kind": "range", "rank": k, "combos": result.get("evaluated"),
                                                  "params": row["params"]})
            out.append({"rank": k, "key": key, "returnPct": res["metrics"]["totalReturnPct"]})
        except Exception as e:
            out.append({"rank": k, "error": f"{type(e).__name__}: {e}"})
    return out


def _run_collect(p: dict) -> dict:
    from . import candle_store
    symbol = p["symbol"].strip().upper()
    start, end = int(p["startMs"]), int(p["endMs"])
    if end <= start:
        return {"error": "종료일이 시작일보다 뒤여야 해"}
    before = candle_store.stats(symbol)["count"]
    fetched = candle_store.fill_range(symbol, start, end, verbose=False)
    st = candle_store.stats(symbol)
    return {
        "symbol": symbol,
        "fetched": fetched,                                  # 신규 저장(겹치는 건 제외)
        "inRange": candle_store.count_range(symbol, start, end),
        "total": st["count"],
        "expected": (end - start) // 60000 + 1,
    }


def _run_collect_chunk(p: dict) -> dict:
    """한 청크(시간 구간)만 수집 — 브라우저가 최신→과거로 반복 호출하며 진행/중지 제어."""
    from . import candle_store
    symbol = p["symbol"].strip().upper()
    fm, to = int(p["fromMs"]), int(p["toMs"])
    fetched = candle_store.fill_range(symbol, fm, to, verbose=False)
    return {"fetched": fetched, "inRange": candle_store.count_range(symbol, fm, to)}


def _cache_list() -> dict:
    from . import candle_store
    return {"symbols": candle_store.list_stats()}


_SAVED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "presets", "saved")


def _safe_name(name: str) -> str:
    """파일명 안전화 — 경로구분자/제어문자만 치환(한글·공백은 유지)."""
    out = "".join("_" if c in '/\\:*?"<>|' or ord(c) < 32 else c for c in (name or "").strip())
    return (out or "preset")[:80]


def _save_preset(p: dict) -> dict:
    """현재 GUI 설정을 로컬 파일로 저장. p = {name, form, params}.

    form = GUI 복원용 폼상태(cpSerialize), params = 백테스트 파라미터(collect).
    저장 파일엔 폼상태 + 빌드된 프리셋(스키마형) 둘 다 담아 사람이/엔진이 바로 읽게 한다.
    """
    name = (p.get("name") or "").strip()
    if not name:
        raise ValueError("프리셋 이름이 없습니다")
    os.makedirs(_SAVED_DIR, exist_ok=True)
    record = {"name": name, "savedAt": int(time.time() * 1000),
              "form": p.get("form"), "params": p.get("params")}
    try:                                    # 스키마형 프리셋도 함께 저장(검증까지)
        preset = _build_preset(p.get("params") or {})
        Preset.from_dict(preset, validate=True)
        record["preset"] = preset
    except Exception as e:
        record["presetError"] = str(e)      # 빌드 실패해도 폼상태는 저장
    path = os.path.join(_SAVED_DIR, _safe_name(name) + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return {"ok": True, "name": name, "path": path,
            "built": "preset" in record, "error": record.get("presetError")}


def _delete_preset(p: dict) -> dict:
    """저장된 프리셋 파일 삭제. p = {name}."""
    name = (p.get("name") or "").strip()
    if not name:
        raise ValueError("프리셋 이름이 없습니다")
    path = os.path.join(_SAVED_DIR, _safe_name(name) + ".json")
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    return {"ok": True, "deleted": existed, "name": name}


def _list_saved_presets() -> dict:
    """presets/saved/*.json 의 프리셋 목록 — GUI 복원용 form과 함께 반환."""
    import glob
    out = []
    for fp in sorted(glob.glob(os.path.join(_SAVED_DIR, "*.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("form"):
                out.append({"name": rec.get("name") or os.path.basename(fp)[:-5],
                            "form": rec["form"], "savedAt": rec.get("savedAt")})
        except Exception:
            continue
    return {"presets": out}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard", "/dashboard/"):   # 랜딩 = 매매 대시보드
            with open(_DASH_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.split("?")[0] in ("/backtest", "/backtest/"):   # 백테스트 스튜디오(?preset=이름 → 그 프리셋으로 열기)
            with open(_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/vendor/lightweight-charts.js":
            with open(_CHARTS_JS, "rb") as f:
                self._send(200, f.read(), "application/javascript; charset=utf-8")
        elif self.path == "/api/cache":
            self._send(200, json.dumps(_cache_list()))
        elif self.path == "/api/presets":
            self._send(200, json.dumps(_list_saved_presets()))
        elif self.path in ("/settings", "/settings/"):       # 글로벌 설정(가드레일·레버리지 티어)
            with open(_SETTINGS_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path in ("/video", "/video/"):             # 트레이딩 영상 분석(자막 → 요약·규칙)
            with open(_VIDEO_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.split("?")[0] in ("/ranking", "/ranking/"):   # 백테스트 결과 랭킹
            with open(_RANKING_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/ranking":                    # 랭킹 표(가벼운 행들)
            from . import ranking
            self._send(200, json.dumps(ranking.list_results(), ensure_ascii=False))
        elif self.path == "/api/video_list":                 # 저장된 영상 분석 목록
            from . import video_analyze
            self._send(200, json.dumps(video_analyze.list_analyses()))
        elif self.path in ("/collector", "/collector/"):     # 데이터·수집기 관리 페이지
            with open(_COLLECTOR_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            try:
                with open(STATE_PATH, encoding="utf-8") as f:
                    self._send(200, f.read())
            except FileNotFoundError:
                self._send(200, json.dumps({"error": "상태 없음 — 봇이 아직 안 돌았거나 state.json 미생성"}))
        elif self.path == "/api/control":
            self._send(200, json.dumps(control.read_control()))
        elif self.path == "/api/bot-config":                 # 봇 실행 설정 + 현재 프리셋 기본값
            self._send(200, json.dumps(bot_config_info()))
        elif self.path == "/api/settings":                   # 글로벌 설정(레버리지 티어 + 리스크 가드레일)
            from . import settings
            self._send(200, json.dumps({"leverageTiers": settings.get_leverage_tiers(),
                                        "guardrails": settings.get_guardrails()}))
        elif self.path == "/api/strategies":                 # 봇이 고를 수 있는 전략 목록
            self._send(200, json.dumps({"strategies": list_strategies()}))
        elif self.path.split("?")[0] == "/api/trades":       # 매매 원장 전체 이력
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            mode = q.get("mode", [None])[0]
            self._send(200, json.dumps({"trades": ledger.load(mode=mode, limit=1000)}))
        elif self.path.split("?")[0] == "/api/stats":        # 원장 집계(승률·손익비·MDD·전략별)
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            mode = q.get("mode", [None])[0]
            self._send(200, json.dumps(ledger.stats(mode=mode)))
        elif self.path.split("?")[0] == "/api/trade_chart":  # 한 거래의 진입~청산 캔들+지표
            from urllib.parse import parse_qs, urlparse
            from . import trade_chart
            q = parse_qs(urlparse(self.path).query)
            try:
                self._send(200, json.dumps(trade_chart.build(
                    int(q.get("id", [0])[0]), mode=q.get("mode", ["paper"])[0])))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
        elif self.path.split("?")[0] == "/api/symbols":       # 데이터탭: 바이낸스 상장 심볼(자동완성)
            from urllib.parse import parse_qs, urlparse
            from . import binance_data
            refresh = parse_qs(urlparse(self.path).query).get("refresh", ["0"])[0] == "1"
            try:
                self._send(200, json.dumps(binance_data.list_symbols(refresh=refresh)))
            except Exception as e:
                self._send(200, json.dumps({"symbols": [], "error": str(e)}))
        elif self.path == "/api/candles":                    # 데이터탭: 심볼별 커버리지·신선도·구멍
            from . import candle_store
            info = {"symbols": candle_store.coverage_report(),
                    "collector": control.service_state("collector"),
                    "collectSymbols": control.get_symbols(),
                    "dbBytes": os.path.getsize(candle_store.DB_PATH) if os.path.exists(candle_store.DB_PATH) else 0}
            self._send(200, json.dumps(info))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/video_analyze":             # 영상 분석(자막 받기 → Claude) + 진행(NDJSON)
            from . import video_analyze
            self._ndjson_stream(video_analyze.run)
            return
        if self.path == "/api/backtest_stream":           # 백테스트 + 진행률(NDJSON)
            self._ndjson_stream(lambda params, emit: emit({"type": "done", "result": _run_backtest_ranked(params, emit)}))
            return
        if self.path == "/api/optimize":
            self._optimize_stream()
            return
        if self.path == "/api/control":                      # 봇/수집기 멈춤·재개
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                self._send(200, json.dumps({"ok": True, "control": control.set_service(body["service"], body["state"])}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/bot-config":                   # 봇 실행 설정 저장(무포지션 시 반영)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, json.dumps({"ok": True, "control": control.set_bot_config(body.get("config") or {})}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/settings":                     # 글로벌 설정 저장(레버리지 티어 / 가드레일)
            from . import settings
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if "leverageTiers" in body:
                    settings.set_leverage_tiers(body.get("leverageTiers") or [])
                if "guardrails" in body:
                    settings.set_guardrails(body.get("guardrails") or {})
                if body.get("resetLossStreak"):      # 연속 손실 기준선을 지금으로 — 래치 수동 해제
                    settings.set_streak_reset_ms(int(time.time() * 1000))
                self._send(200, json.dumps({"ok": True, "leverageTiers": settings.get_leverage_tiers(),
                                            "guardrails": settings.get_guardrails()}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/network":                      # 거래소 전환 testnet↔mainnet(무포지션 시)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                self._send(200, json.dumps({"ok": True, "control": control.set_network(body["network"])}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/strategy":                     # 봇 전략 선택(무포지션 시 전환)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                self._send(200, json.dumps({"ok": True, "control": select_strategy(body["path"])}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/make_preset":                  # 프리셋 만들기 — 신호원+실행설정 → 자기완결 프리셋 저장
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, json.dumps(save_composed_preset(
                    body.get("name"), body.get("base"), body.get("symbol"),
                    body.get("sizing") or {}, body.get("execution") or {}, body.get("filter") or {},
                    replace_path=body.get("replace"))))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/import_preset":                # 프리셋 업로드 — JSON 파일을 data/strategies 에 저장
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, json.dumps(import_preset(body.get("preset"), body.get("name"))))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/send_to_deploy":               # 로컬 프리셋 → 배포 대시보드로 중계 전송(SSH 터널)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(200, json.dumps(send_preset_to_deploy(body.get("path"), body.get("deployUrl"))))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/heal":                          # 데이터탭: 캔들 구멍 수동 복구
            from . import candle_store
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                syms = [body["symbol"]] if body.get("symbol") else [s["symbol"] for s in candle_store.list_stats()]
                res = {s: candle_store.heal_gaps(s, verbose=False) for s in syms}
                self._send(200, json.dumps({"ok": True, "result": res}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        if self.path == "/api/collect_symbols":               # 데이터탭: 수집 심볼 설정(핫리로드)
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                syms = control.clean_symbols(body.get("symbols") or [])
                self._send(200, json.dumps({"ok": True, "control": control.set_symbols(syms)}))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
            return
        from . import video_analyze, ranking, ai_optimize
        routes = {"/api/ai_propose": ai_optimize.propose, "/api/ai_explain": ai_optimize.explain,   # 백테스트 'AI 최적화'
                  "/api/backtest": _run_backtest, "/api/bt_trade_chart": _bt_trade_chart,
                  "/api/video_preset": video_analyze.make_preset,          # 영상 분석 → 백테스트 프리셋
                  "/api/video_chat": video_analyze.chat,                   # 대화하며 초안 다듬기(한 턴)
                  "/api/video_chat_load": video_analyze.chat_load, "/api/video_chat_reset": video_analyze.chat_reset,
                  "/api/video_chat_save": video_analyze.chat_save,         # 대화의 현재 초안 → 프리셋 저장
                  "/api/video_discard": video_analyze.discard,             # 못 쓸 프리셋 → presets/discarded 보관함
                  "/api/video_restore": video_analyze.restore,             # 보관함 → presets/saved
                  "/api/video_search": video_analyze.search,               # 유튜브 검색(제외 채널 거름)
                  "/api/video_excluded": video_analyze.excluded_list, "/api/video_exclude": video_analyze.exclude_channel,
                  "/api/video_unexclude": video_analyze.unexclude_channel,
                  "/api/ranking_item": ranking.get, "/api/ranking_delete": ranking.delete,
                  "/api/collect": _run_collect, "/api/collect_chunk": _run_collect_chunk,
                  "/api/save_preset": _save_preset, "/api/delete_preset": _delete_preset}
        if self.path not in routes:
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            params = json.loads(self.rfile.read(length))
            result = routes[self.path](params)
            self._send(200, json.dumps(result))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(400, json.dumps({"error": str(e)}))

    def _optimize_stream(self):
        """NDJSON 스트리밍 — 조합이 완료되는 대로 한 줄씩 흘려보냄 (진행률 + 실시간 결과)."""
        self._ndjson_stream(_run_optimize)

    def _ndjson_stream(self, fn):
        """fn(params, emit) 를 돌리며 emit(dict) 를 한 줄씩 흘려보낸다(최적화·백테스트 진행률 공용)."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            params = json.loads(self.rfile.read(length))
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()

        try:
            fn(params, emit)
        except (BrokenPipeError, ConnectionResetError):
            pass   # 클라이언트가 중간에 끊음
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                emit({"type": "error", "error": str(e)})
            except Exception:
                pass


def main():
    from .env import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"백테스트 GUI: http://localhost:{args.port}  (Ctrl+C 종료)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
