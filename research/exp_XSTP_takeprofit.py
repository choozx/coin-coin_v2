"""XSTP — XS MOM 주중 익절: 리밸런스 뒤 포트폴리오가 +θ 오르면 전량 정리하고 다음 리밸런스까지 쉼 (사용자, 2026-11-23).

계기: 실매매 첫 주에 잔고가 $174 → $197(+13%) 까지 갔다가 $175 로 돌아왔다 — "그때 정리했으면?".
반론(사전): XS MOM 수익은 소수의 큰 주에 몰린다(최고 3주 빼면 +63 → +43%). 익절은 그 주를 중간에 자른다.

━━ 사전 고정 (2026-11-23 작성 · 결과 보기 전) ━━
틀      exp_XSD_weekday.tranche 와 같은 일 단위 회계(XS MOM 규칙 · 1배 · 펀딩 · 회전 0.10% · 7일 보유).
규칙    리밸런스 종가부터 그 주 누적 수익(일 복리)이 일 종가 기준 ≥ +θ 가 되면 그 종가에 전량 정리(회전 = 보유 총량 × 0.10%),
        다음 리밸런스까지 현금(수익 0). 다음 리밸런스는 빈 상태에서 전량 진입(회전 비용 그대로 계산).
        θ ∈ {+3%, +5%, +8%, +10%, +15%}. 판단은 일 종가로만(실매매 봇은 매시간 볼 수 있지만, 일봉 데이터라 장중 고점은 모른다 — 장중 익절은 이보다 더 일찍 잘린다).
판정    익절이 '개선'이려면 θ 하나가 ① 7개 요일 평균 연 수익 ≥ 기준(익절 없음)의 7개 요일 평균  ② 7개 요일 평균 샤프 ≥ 기준
        ③ 7개 요일 중 5개 이상에서 연 수익이 기준 이상 — 셋 다. (월요일만 좋아지는 건 개선으로 보지 않는다.)
참고    월요일만 결과 · 익절 발동 주 비율 · 발동한 주에 '안 팔았으면' 그 주 끝 수익(놓친 몫).

    .venv/bin/python -u research/exp_XSTP_takeprofit.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_XS_cross_section as xs  # noqa: E402
from research.exp_XSD_weekday import stats  # noqa: E402

THETAS = (None, 0.03, 0.05, 0.08, 0.10, 0.15)


def tranche_tp(days, C, Q, Fd, age, wd, theta, cost=xs.COST):
    """XSD.tranche 와 같되 주중 누적 ≥ θ 면 그 종가에 정리하고 주 끝까지 쉼. 반환: 일 수익 배열, (발동 수, 주 수, 놓친 몫 목록)."""
    n, m = C.shape
    out = np.full(n, np.nan)
    i = int(np.searchsorted(days, xs.START))
    while datetime.fromtimestamp(days[i] / 1000, timezone.utc).weekday() != wd:
        i += 1
    w = np.zeros(m)
    fired = weeks = 0
    missed = []
    for i in range(i, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= xs.AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= xs.MINQ
        js = np.where(live)[0]
        nw = np.zeros(m)
        if len(js) >= 20:
            sig = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * 0.10))
            order = np.argsort(-sig)
            nw[js[order[:k]]] = 0.5 / k
            nw[js[order[-k:]]] = -0.5 / k
        turn = np.abs(nw - w).sum()
        held = np.where(nw != 0)[0]
        cum, flat = 1.0, False
        rest = []                                              # 정리 뒤 '안 팔았으면' 얻었을 일 수익(놓친 몫)
        weeks += 1
        for h in range(1, 8):
            d = i + h
            pr = C[d, held] / C[d - 1, held] - 1
            pr = np.where(np.isfinite(pr), pr, 0.0)
            alive = np.isfinite(C[d, held])
            day = float((nw[held] * (pr - np.where(alive, Fd[d, held], 0.0))).sum())
            if flat:
                out[d] = 0.0
                rest.append(day)
                continue
            out[d] = day - (turn * cost if h == 1 else 0.0)
            cum *= 1 + out[d]
            if theta is not None and cum - 1 >= theta and h < 7:
                out[d] -= np.abs(nw).sum() * cost             # 그 종가에 전량 정리
                flat = True
                fired += 1
        if flat:
            missed.append(float(np.prod([1 + x for x in rest]) - 1))
            w = np.zeros(m)                                    # 다음 주는 빈 상태에서 전량 진입
        else:
            w = nw
    return out, (fired, weeks, missed)


def main():
    days, syms, C, Q, Fd, age = xs.load()
    R, INFO = {}, {}
    for th in THETAS:
        for wd in range(7):
            R[(th, wd)], INFO[(th, wd)] = tranche_tp(days, C, Q, Fd, age, wd, th)
    common = np.all([np.isfinite(R[(None, wd)]) for wd in range(7)], axis=0)
    d0, d1 = days[common][0], days[common][-1]
    print(f"공통 구간 {datetime.fromtimestamp(d0/1000, timezone.utc):%Y-%m-%d} ~ {datetime.fromtimestamp(d1/1000, timezone.utc):%Y-%m-%d} · "
          f"{int(common.sum())}일 · 1배 · 일 종가 기준 익절\n")
    S = {k: stats(v[common]) for k, v in R.items()}
    base_ann = np.mean([S[(None, wd)]["ann"] for wd in range(7)])
    base_sh = np.mean([S[(None, wd)]["sharpe"] for wd in range(7)])
    print(f"{'익절':>6} | {'7요일 평균 연':>12} {'샤프':>6} {'낙폭 중앙':>9} | {'기준 이상 요일':>10} | {'월요일만 연':>10} {'샤프':>6} {'낙폭':>7} | "
          f"{'발동 주':>7} {'놓친 몫(발동 주 평균)':>14}")
    verdict = []
    for th in THETAS:
        anns = np.array([S[(th, wd)]["ann"] for wd in range(7)])
        shs = np.array([S[(th, wd)]["sharpe"] for wd in range(7)])
        mdds = np.array([S[(th, wd)]["mdd"] for wd in range(7)])
        better = int(sum(S[(th, wd)]["ann"] >= S[(None, wd)]["ann"] for wd in range(7)))
        fired = sum(INFO[(th, wd)][0] for wd in range(7)); weeks = sum(INFO[(th, wd)][1] for wd in range(7))
        missed = [x for wd in range(7) for x in INFO[(th, wd)][2]]
        mon = S[(th, 0)]
        lab = "없음" if th is None else f"+{th*100:.0f}%"
        print(f"{lab:>6} | {anns.mean()*100:+11.1f}% {shs.mean():+6.2f} {np.median(mdds)*100:+8.1f}% | {better:>6}/7     | "
              f"{mon['ann']*100:+9.1f}% {mon['sharpe']:+6.2f} {mon['mdd']*100:+6.1f}% | {fired/weeks:6.0%} "
              + (f"{np.mean(missed)*100:+8.2f}% (중앙 {np.median(missed)*100:+.2f})" if missed else "      -"))
        if th is not None:
            ok = [anns.mean() >= base_ann, shs.mean() >= base_sh, better >= 5]
            verdict.append((lab, ok))
    print("\n■ 판정(① 7요일 평균 연 ≥ 기준 · ② 평균 샤프 ≥ 기준 · ③ 5/7 요일 이상에서 연 ≥ 기준)")
    for lab, ok in verdict:
        print(f"  익절 {lab:>5}: {'✅ 개선' if all(ok) else '❌'} [{''.join('○' if x else '×' for x in ok)}]")
    print("\n  '놓친 몫' = 익절한 주에 안 팔고 주 끝까지 들고 있었다면 남은 기간에 더 벌었을 수익(+면 일찍 판 손해, −면 판 덕에 피한 손실).")


if __name__ == "__main__":
    main()
