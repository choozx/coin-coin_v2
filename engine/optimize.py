"""파라미터 최적화 (그리드 서치 + IS/OOS 과최적화 방어 + 멀티프로세싱 병렬).

핵심 방어책:
- In-Sample(앞 70%)에서 최적값 탐색 → Out-of-Sample(뒤 30%)에서 재검증.
  IS만 좋고 OOS에서 무너지면 그 값은 '가짜'(curve-fitting).
- 최소 트레이드 수 미달 조합은 후보에서 제외 (통계적으로 무의미).
- 목적함수 기본 Calmar(수익÷MDD) — 총수익률보다 과최적화에 강함.
- 순위표에서 OOS 성과를 나란히 보여줘 견고성(넓은 봉우리) 판단.

병렬화:
- 조합 IS 평가를 ProcessPoolExecutor로 코어 수만큼 동시 실행.
- 후보(candles/cfg/빌더)는 워커 초기화(initializer) 때 1회만 전달 → 조합마다 재전송 안 함.
- progress_cb(done, total, row)로 완료되는 조합을 즉시 상위(서버)로 흘려보냄 → 실시간 출력.
"""
from __future__ import annotations

import itertools
import os
from concurrent.futures import ProcessPoolExecutor, as_completed

from .candles import Candles
from .backtest import run


def slice_candles(c: Candles, start: int, end: int) -> Candles:
    return Candles(c.open_time[start:end], c.open[start:end], c.high[start:end],
                   c.low[start:end], c.close[start:end], c.volume[start:end], c.timeframe_min)


def expand_spec(spec):
    """탐색 범위 → 값 리스트. spec = {min,max,step} 또는 리스트."""
    if isinstance(spec, list):
        return spec
    lo, hi, st = float(spec["min"]), float(spec["max"]), float(spec["step"])
    if st <= 0 or hi < lo:
        return [lo]
    n = int(round((hi - lo) / st))
    vals = [round(lo + i * st, 10) for i in range(n + 1)]
    # 정수 스텝이면 int로
    if all(float(v).is_integer() for v in vals) and float(st).is_integer():
        vals = [int(v) for v in vals]
    return vals


def objective_value(m, name: str) -> float:
    if name == "return":
        return m.total_return_pct
    if name == "sharpe":
        return m.sharpe()
    if name == "pf":
        pf = m.profit_factor
        return pf if pf != float("inf") else 999.0
    # calmar (기본): 수익 ÷ 최대낙폭
    return m.total_return_pct / max(m.max_drawdown_pct, 0.1)


def metrics_dict(m) -> dict:
    pf = m.profit_factor
    return {
        "return": round(m.total_return_pct, 2),
        "mdd": round(m.max_drawdown_pct, 2),
        "calmar": round(m.total_return_pct / max(m.max_drawdown_pct, 0.1), 2),
        "sharpe": round(m.sharpe(), 2),
        "trades": m.num_trades,
        "winRate": round(m.win_rate_pct, 1),
        "pf": round(pf, 2) if pf != float("inf") else None,
        "liquidations": m.num_liquidations,
    }


# ---- 워커(별도 프로세스)에서 쓰는 전역 상태 + 평가 함수 ----
_W: dict = {}


def _worker_init(names, base_is, base_oos, cfg, build_preset_fn, fixed_params, objective, min_trades):
    _W.clear()
    _W.update(names=names, base_is=base_is, base_oos=base_oos, cfg=cfg,
              build=build_preset_fn, fixed=fixed_params, objective=objective, min_trades=min_trades)


def _params_for(combo):
    params = dict(_W["fixed"])
    for n, v in zip(_W["names"], combo):
        params[n] = v
    return params


def _eval_is(combo):
    """한 조합의 In-Sample 백테스트. 결과 dict 반환 (프로세스 간 전달용, 순수 파이썬 타입)."""
    from .preset import Preset
    try:
        preset = Preset.from_dict(_W["build"](_params_for(combo)), validate=False)
    except Exception:
        return {"combo": combo, "ok": False, "obj": None}
    m = run(_W["base_is"], preset, _W["cfg"])
    if m.num_trades < _W["min_trades"]:
        return {"combo": combo, "ok": False, "obj": None, "trades": m.num_trades}
    obj = objective_value(m, _W["objective"])
    return {
        "combo": combo, "ok": True, "obj": round(obj, 3),
        "params": {n: (combo[i] if not isinstance(combo[i], float) else round(combo[i], 6))
                   for i, n in enumerate(_W["names"])},
        "is": metrics_dict(m),
    }


def _eval_oos(combo):
    from .preset import Preset
    preset = Preset.from_dict(_W["build"](_params_for(combo)), validate=False)
    m = run(_W["base_oos"], preset, _W["cfg"])
    return metrics_dict(m)


def resolve_workers(n_combos, workers=None):
    cpu = os.cpu_count() or 1
    w = workers if workers else max(1, cpu - 1)     # 코어-1 (시스템 여유)
    return max(1, min(w, 8, n_combos))              # 상한 8, 조합수 이하


def optimize(base, build_preset_fn, fixed_params, sweep_specs, cfg,
             objective="calmar", min_trades=15, is_frac=0.7, top_k=20, max_combos=3000,
             workers=None, progress_cb=None):
    """그리드 서치 (병렬).

    build_preset_fn: params dict → 프리셋 dict (server._build_preset 재사용, 프로세스 간 pickle 가능해야 함)
    fixed_params:    폼 전체 값 (탐색 안 하는 것 포함)
    sweep_specs:     {param: {min,max,step}} 탐색 대상
    progress_cb:     (done, total, row|None) — IS 조합 하나 끝날 때마다 호출. row=통과 결과 dict 또는 None.
    """
    names = list(sweep_specs.keys())
    value_lists = [expand_spec(sweep_specs[n]) for n in names]
    combos = list(itertools.product(*value_lists))
    total = len(combos)
    truncated = total > max_combos
    if truncated:
        combos = combos[:max_combos]

    split = int(len(base) * is_frac)
    do_oos = split < len(base)              # is_frac>=1.0이면 OOS 없음
    base_is = slice_candles(base, 0, split) if do_oos else base
    base_oos = slice_candles(base, split, len(base)) if do_oos else None

    workers = resolve_workers(len(combos), workers)
    initargs = (names, base_is, base_oos, cfg, build_preset_fn, fixed_params, objective, min_trades)

    results = []
    obj_by_combo = {}   # 히트맵용 (2개 탐색 시)

    def _absorb(r, done, n):
        obj_by_combo[r["combo"]] = r.get("obj")
        row = None
        if r.get("ok"):
            row = {"params": r["params"], "combo": r["combo"], "is": r["is"], "obj": r["obj"]}
            results.append(row)
        if progress_cb:
            progress_cb(done, n, {"params": row["params"], "is": row["is"], "obj": row["obj"]} if row else None)

    n = len(combos)
    done = 0
    if workers == 1:
        _worker_init(*initargs)
        for combo in combos:
            done += 1
            _absorb(_eval_is(combo), done, n)
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=initargs) as ex:
            futs = [ex.submit(_eval_is, c) for c in combos]
            for fut in as_completed(futs):
                done += 1
                _absorb(fut.result(), done, n)

    # obj 내림차순, 동점은 combo 오름차순으로 결정적 정렬 (병렬 완료순서와 무관하게 재현성 보장)
    results.sort(key=lambda r: (-r["obj"], r["combo"]))
    top = results[:top_k]

    # 상위 후보만 OOS 재검증 (병렬)
    if do_oos and top:
        oos_workers = resolve_workers(len(top), workers)
        if oos_workers == 1:
            _worker_init(*initargs)
            for r in top:
                r["oos"] = _eval_oos(r["combo"])
        else:
            with ProcessPoolExecutor(max_workers=oos_workers, initializer=_worker_init, initargs=initargs) as ex:
                fut_idx = {ex.submit(_eval_oos, r["combo"]): i for i, r in enumerate(top)}
                for fut in as_completed(fut_idx):
                    top[fut_idx[fut]]["oos"] = fut.result()
        for r in top:
            r["robust"] = bool(r["is"]["return"] > 0 and r["oos"]["return"] > 0)
    for r in top:
        r.pop("combo", None)

    out = {
        "objective": objective,
        "totalCombos": total,
        "evaluated": len(combos),
        "truncated": truncated,
        "passed": len(results),
        "minTrades": min_trades,
        "isFrac": is_frac,
        "workers": workers,
        "names": names,
        "valueLists": value_lists,
        "top": top,
    }

    # 2개 파라미터 탐색 시 히트맵 그리드 (IS 목적함수)
    if len(names) == 2:
        grid = []
        for a in value_lists[0]:
            row = []
            for b in value_lists[1]:
                row.append(obj_by_combo.get((a, b)))
            grid.append(row)
        out["heatmap"] = {"xLabel": names[1], "yLabel": names[0],
                          "xVals": value_lists[1], "yVals": value_lists[0], "grid": grid}
    return out
