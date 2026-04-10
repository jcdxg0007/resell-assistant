#!/bin/bash
set -e

echo "=== Resell Assistant ==="
echo "APP_MODE=${APP_MODE:-api}"

if [ "$APP_MODE" = "celery" ]; then
    echo "Starting Celery worker + beat..."
    exec celery -A app.core.celery_app:celery_app worker --beat --loglevel=info --concurrency=2
else
    echo "Running database migrations..."
    alembic upgrade head || echo "Migration skipped (may already be up to date)"
    echo "Starting API server..."
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips='*'
fi
