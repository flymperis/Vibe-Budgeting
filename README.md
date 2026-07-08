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
| `VB_SECURE_COOKIES` | `true` to mark the session cookie `Secure` (set only when served over HTTPS). |
| `ALLOW_REGISTRATION` | `true` or `false` — allow `/register`. |
| `DATABASE_PATH` | Default `/app/data/database.db` (see `Dockerfile`). |
| `FINNHUB_API_KEY` | Free key from [finnhub.io](https://finnhub.io) — live stock/ETF prices in **Investments → Stocks**. |

Stack: Flask app served by Gunicorn in the container; SQLite database file on the mounted volume.

## Integrations (Ollama + Telegram)

Per-user **Ollama** settings and **Telegram bot token** are in **Settings → Integrations** (saved in the database).

Optional env var `TELEGRAM_BOT_TOKEN` works as a fallback if the DB field is empty.

### Ollama

Run Ollama on your home server (default port **11434**). In Docker with `network_mode: service:tailscale-personal`, use:

```
http://127.0.0.1:11434
```

Set Base URL and model in **Integrations → Local AI**, then **Test connection**.

### Telegram bot (polling — Tailscale-only)

No public URL or Tailscale Funnel needed. The app **polls** Telegram outbound (`getUpdates`) — nothing is exposed to the internet.

1. Create a bot with [@BotFather](https://t.me/BotFather) → copy the token.
2. **Settings → Integrations → Telegram bot** → paste token → **Test bot token** → **Save**.
3. **Generate link code** → in Telegram: `/link YOURCODE` → send `supermarket 20`.

The budget app stays on Tailscale only (`http://personal-disk-share:5000`). Telegram works via outbound HTTPS to `api.telegram.org`.

### Telegram usage

| Message | Notes |
|---------|--------|
| `supermarket 20` | Regex — no AI needed |
| `salary 1500` | Income |
| `χθες καφές 3.50` | Needs **Enable AI** + working Ollama |
| `/balance`, `/undo`, `/help` | Commands |
