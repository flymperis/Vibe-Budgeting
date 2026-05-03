# Vibe Budgeting.

Small Flask + SQLite budget tracker. Run with Docker or locally with Python.

## Quick start (Docker)

From the repository root:

```bash
mkdir -p budget-data
docker compose build
docker compose up -d
```

Open **http://localhost:5000**.

- Data lives on the host in `./budget-data` (mounted at `/app/data` in the container). Removing or recreating the container does **not** delete it as long as this volume stays.
- The image defaults `DATABASE_PATH=/app/data/database.db` (see `Dockerfile`). You can override via environment.

### Useful environment variables

| Variable | Purpose |
|----------|---------|
| `FLASK_SECRET_KEY` | Session signing in production (**set a long random value**). |
| `DATABASE_PATH` | SQLite file path inside the container (default `/app/data/database.db`). |
| `ALLOW_REGISTRATION` | `true` / `false` — allow open signup on `/register`. |
| `VB_LEGACY_ADMIN_PASSWORD` | Only used when migrating an old DB without users; default legacy login is `admin` / `changeme` until you change it. |

## Local development (no Docker)

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python app.py
```

Creates `database.db` in the current directory.

## Embedding in your own stack

See **`examples/vibe-budgeting.snippet.yml`** for a drop-in `docker-compose` service fragment (adjust `build.context` if you clone this repo next to your compose file).

## Windows deploy helper (optional)

For setups where your compose file lives **next to** a subfolder named `Vibe-Budgeting` (zip-based update of that folder only):

1. Open CMD and `cd` to that folder (the one that contains `docker-compose.yml`).
2. Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\Vibe-Budgeting\scripts\update-vibe-server.ps1 -BaseDir (Get-Location) -ServiceName budget-app
```

Or run `scripts\update-vibe-server.bat` from that same folder after `cd`.

`-ServiceName` must match your compose service name. If you use this repository alone (`build: .` at the repo root), prefer **`git pull`** instead of this script.

## Requirements

- Docker with Compose **or** Python 3.12+ with dependencies from `requirements.txt`
