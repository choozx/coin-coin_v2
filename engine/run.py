"""백테스트 실행 CLI.

    python3 -m engine.run presets/examples/rsi-oversold-long.json
    python3 -m engine.run <preset.json> [--minutes N] [--equity E] [--seed S]

데이터 소스: 현재는 합성 1분봉. 실데이터는 추후 어댑터로 교체(docs/data-source.md).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from .backtest import BacktestConfig, run
from .preset import Preset
from . import synthetic


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser(description="프리셋 백테스트")
    ap.add_argument("preset", help="프리셋 JSON 경로")
    ap.add_argument("--real", type=float, metavar="DAYS",
                    help="바이낸스 실데이터 N일치 수집해서 백테스트 (API 키 불필요)")
    ap.add_argument("--minutes", type=int, default=60 * 24 * 60, help="합성 1분봉 개수 (기본 60일)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--vol", type=float, default=0.0008, help="분당 변동성(합성)")
    ap.add_argument("--funding", type=float, default=0.0001, help="펀딩비율 상수 근사(실데이터는 실히스토리 사용)")
    args = ap.parse_args()

    preset = Preset.load(args.preset)
    cfg = BacktestConfig(initial_equity=args.equity, funding_rate=args.funding)

    if args.real:
        from . import candle_store
        print(f"바이낸스 {preset.symbol} 1분봉 {args.real}일치 (로컬 캐시 사용)...")
        base = candle_store.ensure_days(preset.symbol, args.real, verbose=True)
        src = f"바이낸스 실데이터 1분봉 {len(base)}개"
        # 실제 펀딩비 평균을 상수 근사로 주입 (구간별 반영은 추후)
        try:
            fr = candle_store.ensure_funding(preset.symbol, days=args.real)
            if fr:
                avg = sum(r for _, r in fr) / len(fr)
                cfg.funding_rate = avg
                print(f"펀딩비 실히스토리 {len(fr)}건, 평균 {avg*100:+.4f}%/8h 적용")
        except Exception as e:
            print(f"펀딩 히스토리 수집 실패({e}) → 상수 근사 사용")
    else:
        base = synthetic.generate(args.minutes, vol_per_min=args.vol, seed=args.seed)
        src = f"합성 1분봉 {len(base)}개"

    print(f"프리셋   : {preset.name}  [{preset.symbol} {preset.timeframe} {preset.direction}]")
    print(f"데이터   : {src}  "
          f"({_fmt(base.open_time[0])} ~ {_fmt(base.open_time[-1])} UTC)")
    gaps = base.gap_report()
    if gaps:
        print(f"⚠️ 결측 구간 {len(gaps)}개")
    print("-" * 52)

    m = run(base, preset, cfg)
    print(m.summary())

    if m.trades:
        print("-" * 52)
        print("청산 사유 분포:")
        from collections import Counter
        for reason, cnt in Counter(t.exit_reason for t in m.trades).most_common():
            print(f"  {reason:12s}: {cnt}건")


if __name__ == "__main__":
    main()
