#!/bin/sh
# First-boot data load. Idempotent: safe to re-run on every container start,
# because backfill resumes from whatever is already stored.
set -e

echo "[bootstrap] waiting for postgres..."
until python -c "
import asyncio, sys
from simin.config import get_settings
from simin.db.repo import make_engine
from sqlalchemy import text

async def check():
    engine = make_engine(get_settings().pg_dsn)
    try:
        async with engine.connect() as conn:
            await conn.execute(text('SELECT 1 FROM ohlcv LIMIT 1'))
    finally:
        await engine.dispose()

asyncio.run(check())
" 2>/dev/null; do
  sleep 2
done
echo "[bootstrap] postgres ready"

SYMBOLS="${SIMIN_BOOTSTRAP_SYMBOLS:-BTCUSDT ETHUSDT SOLUSDT}"
TFS="${SIMIN_BOOTSTRAP_TIMEFRAMES:-1h 4h 1d}"
START="${SIMIN_BOOTSTRAP_START:-2022-01-01}"

echo "[bootstrap] backfilling $SYMBOLS ($TFS) from $START"
python -m simin.cli backfill --symbols $SYMBOLS --timeframes $TFS --start "$START"
echo "[bootstrap] done"
