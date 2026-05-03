# Vibe Budgeting.

Run with **Docker** only:

```bash
git clone https://github.com/flymperis/Vibe-Budgeting.git
cd Vibe-Budgeting
mkdir -p budget-data
docker compose up --build -d
```

Open **http://localhost:5000**.

Data is stored in `./budget-data` on the host (survives container recreate).

Optional environment variables (set under `environment:` in `docker-compose.yml`):

| Variable | Purpose |
|----------|---------|
| `FLASK_SECRET_KEY` | Strong random string for sessions in production. |
| `ALLOW_REGISTRATION` | `true` or `false` — allow `/register`. |
| `DATABASE_PATH` | Default `/app/data/database.db` (see `Dockerfile`). |

Stack: Flask app served by Gunicorn in the container; SQLite database file on the mounted volume.
