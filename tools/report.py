"""운영 현황 한 장 — 원장·진입 판정·체결 실측을 한 번에 읽는다.

왜 스크립트인가: 같은 걸 보려고 매번 긴 python3 -c 한 줄을 SSH 로 붙여넣다가 터미널이
문자를 흘려 계속 깨졌다(`from trade` → `fromtrade`). 도구를 리포지토리에 두면 짧은 명령
하나로 끝나고, 다음에 볼 때도 같은 기준으로 본다.

읽기 전용이며 아무것도 고치지 않는다. 파일이 없으면 없다고 말하고 넘어간다.

    python3 tools/report.py                    # data/ 아래 기본 경로
    python3 tools/report.py --data /app/data
    python3 tools/report.py --mode paper       # 원장 버킷 선택(기본 testnet)

⚠ 원장은 WAL 이라 **읽기에도** -shm 쓰기가 필요하다. 컨테이너(root)가 만든 파일이면
   일반 사용자로는 'attempt to write a readonly database' 가 난다 → `sudo` 로 실행할 것.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import statistics
from datetime import datetime, timezone


def _t(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")


def _rows(path):
    """JSONL → dict 리스트. 없으면 None(=파일 없음), 깨진 줄은 건너뛴다."""
    if not os.path.exists(path):
        return None
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out


def ledger(path, mode):
    print(f"\n=== 원장 {path} (mode={mode}) ===")
    if not os.path.exists(path):
        print("  파일 없음"); return
    try:
        c = sqlite3.connect(path)
        rows = c.execute("SELECT entry_price,exit_price,qty,fees,funding,pnl,reason,ts,"
                         "strategy,mode,leverage FROM trade ORDER BY ts").fetchall()
    except sqlite3.OperationalError as e:
        print(f"  읽기 실패: {e}")
        print("  → 원장은 WAL 이라 읽기에도 -shm 쓰기가 필요하다. `sudo python3 ...` 로 실행할 것.")
        return
    dist = collections.Counter(r[9] for r in rows)
    print(f"  전체 {len(rows)}건 · mode 분포 {dict(dist)}")
    r = [x for x in rows if x[9] == mode]
    if not r:
        print(f"  mode={mode} 거래 없음"); return
    days = max(1e-9, (r[-1][7] - r[0][7]) / 86_400_000)
    notional = sum((a + b) * q for a, b, q, *_ in r)
    fees, funding = sum(x[3] for x in r), sum(x[4] for x in r)
    pnl = sum(x[5] for x in r)
    print(f"  {len(r)}건 · {_t(r[0][7])} ~ {_t(r[-1][7])} UTC · {days:.1f}일 → {len(r)/days:.1f}건/일")
    if notional > 0:
        # 진입+청산이 섞인 근사치다. 정확한 maker 비율은 fill_log 가 준다.
        print(f"  수수료 {fees:+.4f} / 명목 {notional:.2f} = {fees/notional*1e4:.2f}bp"
              f"  (전부 maker≈0~2bp, 전부 taker≈5bp)")
    print(f"  펀딩 {funding:+.4f} · 실현손익 {pnl:+.2f}")
    print(f"  청산사유 {dict(collections.Counter(x[6] for x in r))}")
    print(f"  전략별 {dict(collections.Counter(os.path.basename(x[8] or '?') for x in r))}")
    print(f"  레버리지 {sorted(set(x[10] for x in r))}")


def state(path):
    """봇 상태 — 밴 여부와 API 요청 계측. 밴은 '얼마나 보냈나'를 몰라서 당한다."""
    print(f"\n=== 봇 상태 {path} ===")
    if not os.path.exists(path):
        print("  파일 없음"); return
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"  읽기 실패: {e}"); return
    print(f"  {d.get('mode')} · {'⏸ 멈춤' if d.get('paused') else '▶️ 실행중'} · "
          f"갱신 {_t(d.get('updatedAt', 0))} UTC")
    if d.get("bannedUntil"):
        left = (d["bannedUntil"] - datetime.now(timezone.utc).timestamp() * 1000) / 60000
        print(f"  🚫 레이트리밋 밴 — 해제까지 {left:.1f}분 (그동안 포지션 관리 정지)")
    api = d.get("apiReq") or {}
    if api:
        print(f"  API 요청: 직전 폴 {api.get('lastTotal', 0)}회 · 최대 {api.get('peak', 0)}회"
              f"  {api.get('last') or ''}")
        if (api.get("peak") or 0) > 100:
            print("    ⚠ 한 폴에 100회를 넘겼다 — 밴 위험(청산 재시도 루프 의심)")
    if d.get("guardrail"):
        print(f"  🛡 가드레일: {d['guardrail']}")


def entry(path, top=12):
    print(f"\n=== 진입 판정 {path} ===")
    rows = _rows(path)
    if rows is None:
        print("  파일 없음 (트레이더가 아직 이 버전이 아니거나 경로가 다름)"); return
    if not rows:
        print("  비어 있음"); return
    dec = sum(1 for r in rows if r.get("decided"))
    print(f"  {len(rows)}줄 · 판정 {dec} / 미리보기 {len(rows)-dec}"
          f" · {_t(rows[0]['at'])} ~ {_t(rows[-1]['at'])} UTC")
    print("  ── 무엇이 막았나 ──")
    for k, n in collections.Counter(r.get("block") or "진입!" for r in rows).most_common():
        print(f"    {n:7d}  {k}")
    c = collections.Counter()
    for r in rows:
        if r.get("block") != "진입 조건 미충족":
            continue
        for rule in r.get("rules") or []:
            for cond in rule.get("failed") or []:
                # 값은 매 봉 달라지므로 지표 이름만 집계한다(무엇이 병목인가).
                c[f"{rule['side']}: {cond.split('=')[0].split(' ')[0]}"] += 1
    if c:
        print("  ── 어느 조건이 걸렸나(지표별) ──")
        for k, n in c.most_common(top):
            print(f"    {n:7d}  {k}")
    print("  ── 최근 5줄 ──")
    for r in rows[-5:]:
        kind = "판정" if r.get("decided") else "  · "
        print(f"    {_t(r['at'])} [{kind}] {r.get('block') or '🟢 진입 ' + str(r.get('entered'))}")


def fills(path):
    print(f"\n=== 체결 실측 {path} ===")
    rows = _rows(path)
    if rows is None:
        print("  파일 없음 — 이 버전이 배포되기 전 체결은 기록이 없다(기대가가 덮어써져 소급 불가).")
        return
    if not rows:
        print("  비어 있음 — 배포 후 아직 체결이 없음"); return
    print(f"  {len(rows)}건 · {_t(rows[0]['at'])} ~ {_t(rows[-1]['at'])} UTC")
    for kind, label in (("entry", "진입"), ("exit", "청산")):
        sub = [r for r in rows if r.get("kind") == kind]
        if not sub:
            continue
        slips = [r["slipPct"] for r in sub if r.get("slipPct") is not None]
        mrs = [r["makerRatio"] for r in sub if r.get("makerRatio") is not None]
        bps = [r["feeBps"] for r in sub if r.get("feeBps") is not None]
        print(f"  ── {label} {len(sub)}건 ──")
        if slips:
            # 양수 = 불리(손해). 롱·숏 부호를 통일해 놨으므로 평균이 의미를 갖는다.
            print(f"    슬리피지  평균 {statistics.mean(slips)*100:+.2f}bp"
                  f" · 중앙 {statistics.median(slips)*100:+.2f}bp"
                  f" · 최악 {max(slips)*100:+.2f}bp"
                  f" · 불리 비율 {sum(1 for s in slips if s > 0)/len(slips)*100:.0f}%")
        if mrs:
            print(f"    maker 비율 평균 {statistics.mean(mrs):.1f}%"
                  f" · 전부 maker {sum(1 for m in mrs if m >= 99.9)}건"
                  f" · 전부 taker {sum(1 for m in mrs if m <= 0.1)}건")
        if bps:
            print(f"    실수수료  평균 {statistics.mean(bps):.2f}bp")
        want = [r for r in sub if r.get("intendedMaker")]
        if want:
            got = [r["makerRatio"] for r in want if r.get("makerRatio") is not None]
            if got:
                # 엔진은 maker 를 가정했는데 실제로 얼마나 maker 였나 = 정책이 먹히는가.
                print(f"    엔진 maker 가정 {len(want)}건 → 실제 maker 평균 {statistics.mean(got):.1f}%")
        orders = [r["orders"] for r in sub if r.get("orders")]
        if orders:
            print(f"    지정가 재시도  평균 {statistics.mean(orders):.1f}회 · 최대 {max(orders)}회")


def main():
    ap = argparse.ArgumentParser(description="운영 현황 한 장(원장·진입 판정·체결 실측)")
    ap.add_argument("--data", default="data", help="데이터 디렉터리(기본 data)")
    ap.add_argument("--mode", default="testnet", help="원장 버킷: paper|testnet|live")
    a = ap.parse_args()
    state(os.path.join(a.data, "state.json"))
    ledger(os.path.join(a.data, "trades.db"), a.mode)
    entry(os.path.join(a.data, "entry_log.jsonl"))
    fills(os.path.join(a.data, "fill_log.jsonl"))
    print()


if __name__ == "__main__":
    main()
