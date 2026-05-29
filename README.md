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
| `FINNHUB_API_KEY` | Free key from [finnhub.io](https://finnhub.io) — live stock/ETF prices in **Investments → Stocks**. |

Stack: Flask app served by Gunicorn in the container; SQLite database file on the mounted volume.

## Integrations (Ollama + Telegram)

Per-user **Ollama** settings and **Telegram bot** settings are in **Settings → Integrations** (saved in the database).

Optional env vars (`TELEGRAM_BOT_TOKEN`, etc.) still work as a fallback if the DB fields are empty.

### Ollama

Run Ollama on your home server (default port **11434**). Set Base URL and model in **Integrations → Local AI**, then **Test connection**.

### Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather) → copy the token.
2. Expose the app on **HTTPS** (Telegram requires it), e.g. [Tailscale Funnel](https://tailscale.com/kb/1223/tailscale-funnel/):

   ```bash
   tailscale funnel 5000
   ```

3. In the app: **Settings → Integrations → Telegram bot**
   - **Bot token** — from BotFather
   - **Webhook secret** — any random string (or leave blank to auto-generate)
   - **Public HTTPS base URL** — e.g. `https://your-machine.tailXXXX.ts.net` (no path)

4. Click **Save Telegram settings** — the app registers the webhook automatically and shows the full webhook URL.

5. **Generate link code** → in Telegram: `/link YOURCODE` → send `supermarket 20`

### Telegram usage

| Message | Notes |
|---------|--------|
| `supermarket 20` | Regex — no AI needed |
| `salary 1500` | Income |
| `χθες καφές 3.50` | Needs **Enable AI** + working Ollama |
| `/balance`, `/undo`, `/help` | Commands |
