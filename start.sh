#!/bin/sh
set -e

echo "Running database migrations..."
python -m alembic upgrade head

echo "Starting Julian..."
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
