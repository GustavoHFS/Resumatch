# ==============================================================================
# Stage 1: Build React + TypeScript + Pure Vanilla CSS SPA
# ==============================================================================
FROM node:22-alpine AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ==============================================================================
# Stage 2: FastAPI + SQLModel WAL + Headless Chromium A4 PDF Runtime
# ==============================================================================
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY backend/ ./backend/

RUN pip install --no-cache-dir . \
    && playwright install --with-deps chromium-headless-shell

COPY examples/ ./examples/
COPY scripts/ ./scripts/
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist
RUN mkdir -p /app/user_data

EXPOSE 8000

CMD ["sh", "-c", "python scripts/bootstrap_demo.py && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
