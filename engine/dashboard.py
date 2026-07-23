"""매매 대시보드 — 봇이 기록한 상태(data/state.json)를 읽어 웹으로 보여준다.

봇(engine.live)과 '별개 프로세스'. read-only 모니터라 대시보드를 몇 번 재배포해도
실매매 봇·캔들 수집은 안 멈춘다(멀티서비스 구조). 상태는 공유 볼륨의 state.json으로 주고받음.

    python3 -m engine.dashboard --port 8080     # http://localhost:8080
    STATE_PATH=/app/data/state.json 로 상태 파일 경로 지정(도커).
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import candle_store
from . import control
from . import ledger
from .preset import bot_config_info, import_preset, list_strategies, save_composed_preset, select_strategy
from . import settings

_HTML = os.path.join(os.path.dirname(__file__), "dashboard.html")
_COLLECTOR_HTML = os.path.join(os.path.dirname(__file__), "collector.html")   # 데이터·수집기 관리
_SETTINGS_HTML = os.path.join(os.path.dirname(__file__), "settings.html")     # 글로벌 설정(가드레일·레버리지 티어)
_CHARTS_JS = os.path.join(os.path.dirname(__file__), "vendor",
                          "lightweight-charts.standalone.production.js")
# 대시보드에는 백테스트 스튜디오가 없다(server.py 전용, 프로덕션 이미지에서 제외).
# 페이지가 그 링크를 감추도록 알려준다 — 안 그러면 헤더에 죽은 링크가 남는다.
_NO_STUDIO = b"<script>window.__NO_STUDIO__=true</script>"
STATE_PATH = os.environ.get("STATE_PATH", "data/state.json")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            with open(_HTML, "rb") as f:
                # 플래그를 **앞에** — 페이지 하단 스크립트가 이 값을 읽으므로 순서가 뒤집히면 안 된다.
                self._send(200, _NO_STUDIO + f.read(), "text/html; charset=utf-8")
        elif self.path in ("/settings", "/settings/"):
            with open(_SETTINGS_HTML, "rb") as f:
                self._send(200, _NO_STUDIO + f.read(), "text/html; charset=utf-8")
        elif self.path in ("/collector", "/collector/"):
            # 수집 심볼 관리·구멍 복구는 프로덕션에서 더 필요하다(수집기가 24/7 도는 곳이니까).
            # 필요한 API(/api/candles·symbols·collect_symbols·heal·collect_chunk)는 여기 다 있다.
            with open(_COLLECTOR_HTML, "rb") as f:
                self._send(200, _NO_STUDIO + f.read(), "text/html; charset=utf-8")
        elif self.path == "/vendor/lightweight-charts.js":
            with open(_CHARTS_JS, "rb") as f:
                self._send(200, f.read(), "application/javascript; charset=utf-8")
        elif self.path == "/api/state":
            try:
                with open(STATE_PATH, encoding="utf-8") as f:
                    self._send(200, f.read())
            except FileNotFoundError:
                self._send(200, json.dumps({"error": "상태 없음 — 봇이 아직 안 돌았거나 state.json 미생성"}))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}))
        elif self.path == "/api/control":
            self._send(200, json.dumps(control.read_control()))
        elif self.path == "/api/strategies":
            self._send(200, json.dumps({"strategies": list_strategies()}))
        elif self.path == "/api/bot-config":      # 봇 실행 설정(심볼·사이징·실행·필터) + 프리셋 기본값
            self._send(200, json.dumps(bot_config_info()))
        elif self.path == "/api/settings":        # 글로벌 설정: 레버리지 티어 + 리스크 가드레일
            self._send(200, json.dumps({"leverageTiers": settings.get_leverage_tiers(),
                                        "guardrails": settings.get_guardrails()}))
        elif self.path == "/api/cache":           # 수집된 심볼 목록(커버리지 요약)
            self._send(200, json.dumps({"symbols": candle_store.list_stats()}))
        elif self.path.split("?")[0] == "/api/trades":
            from urllib.parse import parse_qs, urlparse
            mode = parse_qs(urlparse(self.path).query).get("mode", [None])[0]
            self._send(200, json.dumps({"trades": ledger.load(mode=mode, limit=1000)}))
        elif self.path.split("?")[0] == "/api/stats":
            from urllib.parse import parse_qs, urlparse
            mode = parse_qs(urlparse(self.path).query).get("mode", [None])[0]
            self._send(200, json.dumps(ledger.stats(mode=mode)))
        elif self.path.split("?")[0] == "/api/trade_chart":
            from urllib.parse import parse_qs, urlparse
            from . import trade_chart
            q = parse_qs(urlparse(self.path).query)
            try:
                self._send(200, json.dumps(trade_chart.build(
                    int(q.get("id", [0])[0]), mode=q.get("mode", ["paper"])[0])))
            except Exception as e:
                self._send(400, json.dumps({"error": str(e)}))
        elif self.path.split("?")[0] == "/api/symbols":   # 바이낸스 상장 심볼(자동완성·오타검증)
            from urllib.parse import parse_qs, urlparse
            from . import binance_data
            refresh = parse_qs(urlparse(self.path).query).get("refresh", ["0"])[0] == "1"
            try:
                self._send(200, json.dumps(binance_data.list_symbols(refresh=refresh)))
            except Exception as e:
                # 목록을 못 받아도 편집기는 자유입력으로 계속 동작해야 한다(검증만 비활성).
                self._send(200, json.dumps({"symbols": [], "error": str(e)}))
        elif self.path == "/api/candles":
            import os as _os
            info = {"symbols": candle_store.coverage_report(),
                    "collector": control.service_state("collector"),
                    "collectSymbols": control.get_symbols(),
                    "dbBytes": _os.path.getsize(candle_store.DB_PATH) if _os.path.exists(candle_store.DB_PATH) else 0}
            self._send(200, json.dumps(info))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            if self.path == "/api/control":       # {"service":"trader"|"collector","state":...}
                ctrl = control.set_service(body["service"], body["state"])
            elif self.path == "/api/strategy":     # {"path": "presets/..."} 봇 전략 선택
                ctrl = select_strategy(body["path"])
            elif self.path == "/api/make_preset":  # 프리셋 만들기/수정 — 신호원+실행설정 → 자기완결 프리셋 저장
                self._send(200, json.dumps(save_composed_preset(
                    body.get("name"), body.get("base"), body.get("symbol"),
                    body.get("sizing") or {}, body.get("execution") or {}, body.get("filter") or {},
                    replace_path=body.get("replace"))))
                return
            elif self.path == "/api/import_preset":  # 프리셋 업로드 — JSON 파일을 data/strategies 에 저장
                self._send(200, json.dumps(import_preset(body.get("preset"), body.get("name"))))
                return
            elif self.path == "/api/heal":         # 캔들 구멍 수동 복구
                syms = [body["symbol"]] if body.get("symbol") else [s["symbol"] for s in candle_store.list_stats()]
                self._send(200, json.dumps({"ok": True, "result": {s: candle_store.heal_gaps(s, verbose=False) for s in syms}}))
                return
            elif self.path == "/api/bot-config":         # 봇 실행 설정 저장(무포지션 시 반영)
                ctrl = control.set_bot_config(body.get("config") or {})
            elif self.path == "/api/settings":           # 글로벌 설정 저장(레버리지 티어 / 가드레일)
                if "leverageTiers" in body:
                    settings.set_leverage_tiers(body.get("leverageTiers") or [])
                if "guardrails" in body:
                    settings.set_guardrails(body.get("guardrails") or {})
                self._send(200, json.dumps({"ok": True,
                                            "leverageTiers": settings.get_leverage_tiers(),
                                            "guardrails": settings.get_guardrails()}))
                return
            elif self.path == "/api/collect_chunk":     # 과거 구간 수동 백필(브라우저가 청크 단위로 반복 호출)
                fetched = candle_store.fill_range(body["symbol"].strip().upper(),
                                                  int(body["fromMs"]), int(body["toMs"]), verbose=False)
                self._send(200, json.dumps({
                    "fetched": fetched,
                    "inRange": candle_store.count_range(body["symbol"].strip().upper(),
                                                        int(body["fromMs"]), int(body["toMs"]))}))
                return
            elif self.path == "/api/collect_symbols":   # 수집 심볼 설정(핫리로드)
                ctrl = control.set_symbols(control.clean_symbols(body.get("symbols") or []))
                self._send(200, json.dumps({"ok": True, "control": ctrl}))
                return
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, json.dumps({"ok": True, "control": ctrl}))
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}))

    def log_message(self, *a):
        pass


def main():
    from .env import load_dotenv
    load_dotenv()
    ap = argparse.ArgumentParser(description="매매 대시보드 (봇 상태 모니터)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    print(f"대시보드: http://localhost:{args.port}   (상태 파일: {STATE_PATH})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
