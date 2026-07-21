# 페이퍼/실거래 트레이더 컨테이너. GUI 아님 — engine.live 루프만 돈다.
# 마찰점은 TA-Lib(C 라이브러리) — 여기서 소스 빌드해 해결.
FROM python:3.11-slim

# --- TA-Lib C 라이브러리 (지표 계산 백엔드) ---
# 실패 시: TALIB_VERSION 태그 확인(github.com/ta-lib/ta-lib/releases) 또는 0.4.0 소스로 교체.
ARG TALIB_VERSION=0.6.4
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential wget ca-certificates \
 && wget -q https://github.com/ta-lib/ta-lib/releases/download/v${TALIB_VERSION}/ta-lib-${TALIB_VERSION}-src.tar.gz \
 && tar -xzf ta-lib-${TALIB_VERSION}-src.tar.gz \
 && cd ta-lib-${TALIB_VERSION} \
 && ./configure --prefix=/usr \
 && make -j"$(nproc)" && make install \
 && cd .. && rm -rf ta-lib-${TALIB_VERSION}* \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 파이썬 의존성 먼저(캐시). TA-Lib 파이썬 래퍼는 위 C 헤더(/usr/include)에 컴파일됨.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 코드·스키마·프리셋 (GUI/vendor는 .dockerignore로 제외 — 트레이더엔 불필요)
COPY engine/ engine/
COPY schema/ schema/
COPY presets/ presets/

# 페이퍼 트레이딩 기본 실행. 프리셋·간격·잔고·알림은 환경변수로.
#   PRESET          : 프리셋 JSON 경로 (presets/saved/... 또는 examples/...)
#   INTERVAL        : 폴링 간격 초 (기본 60)
#   EQUITY          : 초기 잔고
#   NOTIFY_WEBHOOK  : (선택) Discord/Slack 웹훅 — 진입/청산/에러 알림
ENV PRESET=presets/examples/rsi-oversold-long.json \
    INTERVAL=60 \
    EQUITY=10000
CMD ["sh", "-c", "python -m engine.live \"$PRESET\" --paper --interval \"$INTERVAL\" --equity \"$EQUITY\""]
