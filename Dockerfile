# ── Stage 1: Build Backend Dependencies ─────────────────────────
FROM python:3.11-slim AS backend-builder

WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r backend/requirements.txt

# ── Stage 2: Build Frontend (Next.js Standalone) ───────────────
FROM node:20-slim AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production
RUN npm run build

# ── Stage 3: Final Runtime Image ──────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# Install runtime dependencies: Node.js 20, Redis, curl, libpq5, bash, ca-certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gnupg \
        libpq5 \
        redis-server \
        bash && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Caddy static binary directly from caddy:2-alpine
COPY --from=caddy:2-alpine /usr/bin/caddy /usr/local/bin/caddy

# Install Python backend dependencies
COPY --from=backend-builder /wheels /wheels
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r backend/requirements.txt && rm -rf /wheels

# Copy backend application code
COPY backend /app/backend

# Copy frontend standalone build
COPY --from=frontend-builder /app/frontend/public /app/frontend/standalone/public
COPY --from=frontend-builder /app/frontend/.next/standalone /app/frontend/standalone
COPY --from=frontend-builder /app/frontend/.next/static /app/frontend/standalone/.next/static

# Copy Caddyfile and startup script
COPY Caddyfile /etc/caddy/Caddyfile
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 10000

ENV PORT=10000

CMD ["/app/start.sh"]
