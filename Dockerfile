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
CMD ["gunicorn", "--preload", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]
