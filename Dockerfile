# ============================================================
# Multi-Stage Docker Build — Production-Optimized
# Stage 1: Install dependencies (builder layer — cached)
# Stage 2: Production image (slim, non-root, Gunicorn+Uvicorn)
# ============================================================

# ---- STAGE 1: Builder ----
FROM python:3.10-slim-bookworm AS builder

WORKDIR /app

# Install system dependencies required for building packages
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libpq-dev build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies into a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# ---- STAGE 2: Production ----
FROM python:3.10-slim-bookworm AS production

WORKDIR /app

# Install only runtime dependencies (no build tools)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY . /app/

# Collect static files for Nginx to serve
RUN python manage.py collectstatic --noinput 2>/dev/null || true

# Security: Create and switch to a non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Production ASGI server: Gunicorn with Uvicorn workers
# - Workers = 2 * CPU cores + 1 (tuned via env var or default 4)
# - Graceful timeout for zero-downtime deploys
CMD ["gunicorn", "backend.asgi:application", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]