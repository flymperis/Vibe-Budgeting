FROM python:3.12-slim

WORKDIR /app

# Default matches docker-compose volume ./budget-data:/app/data — avoids writing SQLite under /app/database.db
# (container layer) where data is lost on recreate. Override if you bind-mount a single file elsewhere.
ENV DATABASE_PATH=/app/data/database.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# --preload: init_db() runs once in the master; without it each worker imports app and races on SQLite.
# Single worker + threads (not -w 2): the in-memory price caches (_price_cache,
# _stock_price_cache) live per-process, so multiple worker processes would each
# keep their own cache, doubling external price-API calls and making the
# "prices fetched Xs ago" display inconsistent between requests. One process
# with threads keeps the cache correct while still handling concurrent requests.
CMD ["gunicorn", "--preload", "-w", "1", "--worker-class", "gthread", "--threads", "4", "-b", "0.0.0.0:5000", "-c", "gunicorn.conf.py", "app:app"]
