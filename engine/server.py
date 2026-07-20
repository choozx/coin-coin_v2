"""로컬 백테스트 GUI 서버 (파이썬 표준 라이브러리만 사용).

    python3 -m engine.server            # http://localhost:8765 접속
    python3 -m engine.server --port 9000

브라우저 폼에서 프리셋 파라미터를 조절 → 실데이터로 백테스트 → 결과 확인.
프론트엔드는 engine/gui.html.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backtest import BacktestConfig, run
from .candles import resample, TIMEFRAME_MINUTES
from .preset import Preset

_HTML = os.path.join(os.path.dirname(__file__), "gui.html")
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


def _price_target(p: dict, prefix: str):
    """폼 파라미터 → 익절/손절 블록. prefix='sl' 또는 'tp'. 'off'면 None."""
    t = p.get(prefix + "Type", "off")
    if t in ("off", "", None):
        return None
    if t in ("percent", "atrMultiple", "price", "riskReward"):
        v = float(p.get(prefix + "Value", 0) or 0)
        return {"type": t, "value": v} if v > 0 else None
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
            groups.append({"side": g.get("side", "long"), "conds": copy.deepcopy(g.get("conditions") or [])})
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
            d = d.setdefault(k, {})
        d[parts[-1]] = val

    if not groups:
        raise ValueError("진입 조건을 하나 이상 추가해줘 (진입 섹션에서 조건 추가)")

    def _grp(nodes):
        return nodes[0] if len(nodes) == 1 else {"op": "AND", "children": nodes}
    entry_rules = [{"side": g["side"], "when": _grp(g["conds"])} for g in groups]
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
    elif p.get("tpType") == "supertrend":       # SuperTrend 전환 익절 (방향 인식)
        exit_block["supertrendExit"] = {
            "period": int(p.get("tpStPeriod", 10) or 10),
            "multiplier": float(p.get("tpStMult", 3.0) or 3.0),
        }
    if p.get("trailingEnabled") and float(p.get("trailingCallback", 0)) > 0:
        exit_block["trailing"] = {
            "enabled": True,
            "callbackPercent": float(p["trailingCallback"]),
            "activationPercent": float(p.get("trailingActivation", 0)),
        }
    # 지표 조건 청산 (지표 평균 복귀) — 역추세 매매의 핵심 청산. 지표는 선택 가능(RSI/Stoch 등)
    if p.get("exitCondEnabled"):
        exit_block["condition"] = {
            "left": {"indicator": p.get("exitCondInd", "RSI"), "period": int(p.get("exitCondPeriod", 14))},
            "cmp": p.get("exitCondCmp", ">"),
            "right": float(p["exitCondValue"]),
        }
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
    if filt:
        preset["filter"] = filt
    return preset


def _downsample(curve, n=400):
    if len(curve) <= n:
        return curve
    step = len(curve) / n
    return [curve[int(i * step)] for i in range(n)] + [curve[-1]]


def _ohlc_for_chart(base, tf_min: int, max_bars: int = 200000):
    """차트용 캔들스틱 OHLC. 백테스트 타임프레임 그대로 리샘플(무압축)해 반환하되,
    max_bars 초과 시에만 연속 봉을 묶어 압축(응답 크기 방어).
    open=첫 open, high=max, low=min, close=마지막 close."""
    import math
    cs = resample(base, tf_min)
    n = len(cs)
    k = 1 if n <= max_bars else math.ceil(n / max_bars)
    out = []
    for s in range(0, n, k):
        e = min(s + k, n)
        out.append([
            int(cs.open_time[s]),
            round(float(cs.open[s]), 2),
            round(float(cs.high[s:e].max()), 2),
            round(float(cs.low[s:e].min()), 2),
            round(float(cs.close[e - 1]), 2),
        ])
    return out, (k * tf_min)   # (캔들목록, 캔들 1개가 나타내는 분)


def _run_backtest(p: dict) -> dict:
    base, fr_rate = _get_candles(p["symbol"], float(p["days"]))
    preset_dict = _build_preset(p)
    preset = Preset.from_dict(preset_dict, validate=True)  # 스키마 검증
    cfg = BacktestConfig(initial_equity=float(p["equity"]), funding_rate=fr_rate)
    m = run(base, preset, cfg)

    from collections import Counter
    import math
    reasons = Counter(t.exit_reason for t in m.trades)

    def _px(x):   # nan → None, 그 외 round
        return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)

    ohlc, bar_min = _ohlc_for_chart(base, TIMEFRAME_MINUTES[p["timeframe"]])
    trades_out = [{
        "side": t.side,
        "entryTime": int(t.entry_time), "entryPrice": round(float(t.entry_price), 2),
        "exitTime": int(t.exit_time), "exitPrice": round(float(t.exit_price), 2),
        "stop": _px(t.stop_price), "tp": _px(t.tp_price),
        "pnl": round(float(t.pnl), 2), "reason": t.exit_reason,
    } for t in m.trades]
    return {
        "preset": preset_dict,
        "dataRange": [int(base.open_time[0]), int(base.open_time[-1])],
        "candles": len(base),
        "fundingRate": fr_rate,
        "ohlc": ohlc,
        "ohlcBarMin": bar_min,
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
        "equityCurve": _downsample([[t, round(e, 2)] for t, e in m.equity_curve]),
    }


def _run_optimize(p: dict, emit) -> None:
    """최적화를 NDJSON 스트리밍으로 진행 — emit(dict)이 한 줄씩 클라이언트로 흘려보냄.

    이벤트: start → combo(진행/완료 조합, done/total) … → finalizing → done(최종 순위표).
    """
    from . import optimize as opt
    base, fr_rate = _get_candles(p["symbol"], float(p["days"]))
    cfg = BacktestConfig(initial_equity=float(p["equity"]), funding_rate=fr_rate)
    sweep = p.get("sweep", {})              # {param: {min,max,step}}
    if not sweep:
        emit({"type": "error", "error": "탐색할 파라미터를 하나 이상 체크해줘"})
        return

    names = list(sweep.keys())
    value_lists = [opt.expand_spec(sweep[n]) for n in names]
    total = 1
    for vl in value_lists:
        total *= len(vl)
    workers = opt.resolve_workers(min(total, 3000))
    emit({"type": "start", "total": total, "names": names, "workers": workers})

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
    )
    result["type"] = "done"
    emit(result)


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
        if self.path in ("/", "/index.html"):
            with open(_HTML, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/vendor/lightweight-charts.js":
            with open(_CHARTS_JS, "rb") as f:
                self._send(200, f.read(), "application/javascript; charset=utf-8")
        elif self.path == "/api/cache":
            self._send(200, json.dumps(_cache_list()))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/api/optimize":
            self._optimize_stream()
            return
        routes = {"/api/backtest": _run_backtest,
                  "/api/collect": _run_collect, "/api/collect_chunk": _run_collect_chunk}
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
            _run_optimize(params, emit)
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
