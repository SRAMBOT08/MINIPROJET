#!/bin/bash
set -e

echo "=== Starting Intra AI Unified Instance ==="

export PORT="${PORT:-10000}"
export HOSTNAME="0.0.0.0"
export NODE_ENV="production"
export PYTHONUNBUFFERED="1"

# Start local Redis if REDIS_URL points to localhost/127.0.0.1 or is default
if [[ "${REDIS_URL:-redis://127.0.0.1:6379/0}" == *"localhost"* ]] || [[ "${REDIS_URL:-redis://127.0.0.1:6379/0}" == *"127.0.0.1"* ]]; then
    echo "Starting local Redis server..."
    redis-server --daemonize yes --appendonly no --save "" --protected-mode no
fi

cleanup() {
    echo "Stopping all services..."
    kill -TERM "$UVICORN_PID" "$NEXT_PID" "$CADDY_PID" 2>/dev/null || true
    wait
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "Starting FastAPI backend on 127.0.0.1:8000..."
cd /app/backend
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 &
UVICORN_PID=$!

echo "Starting Next.js frontend on 127.0.0.1:3000..."
cd /app/frontend/standalone
PORT=3000 HOSTNAME=127.0.0.1 node server.js &
NEXT_PID=$!

# Wait briefly for FastAPI and Next.js to start
sleep 2

echo "Starting Caddy reverse proxy on port ${PORT}..."
caddy run --config /etc/caddy/Caddyfile --adapter caddyfile &
CADDY_PID=$!

echo "Intra AI is ready and serving on port ${PORT}!"

# Wait for any process to exit
wait -n "$UVICORN_PID" "$NEXT_PID" "$CADDY_PID"
cleanup
