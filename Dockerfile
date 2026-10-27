```dockerfile
FROM node:26-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    ffmpeg \
    ca-certificates \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

RUN python -m pip install --upgrade pip setuptools wheel

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider

WORKDIR /opt/bgutil-ytdlp-pot-provider/server

RUN npm ci

RUN npx tsc

WORKDIR /app

CMD sh -c 'set -e; echo "Starting BgUtils PO Token Provider..."; node /opt/bgutil-ytdlp-pot-provider/server/build/main.js --host 127.0.0.1 --port 4416 & BGUTIL_PID=$!; READY=0; for i in $(seq 1 40); do if curl -fsS http://127.0.0.1:4416/ping >/dev/null 2>&1; then READY=1; echo "BgUtils PO Token Provider is ONLINE"; break; fi; sleep 1; done; if [ "$READY" != "1" ]; then echo "ERROR: BgUtils PO Token Provider failed to start"; ps aux || true; kill "$BGUTIL_PID" 2>/dev/null || true; exit 1; fi; echo "Starting SnapMega API..."; exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}'
```
