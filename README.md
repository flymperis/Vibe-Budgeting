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
| `FLASK_SECRET_KEY` | Strong random string for sessions in production. If unset, a random key is generated on first boot and stored next to the database as `secret_key`. |
| `VB_SECURE_COOKIES` | `true` to mark the session cookie `Secure` (set only when served over HTTPS). |
| `ALLOW_REGISTRATION` | `true` or `false` — allow `/register`. |
| `DATABASE_PATH` | Default `/app/data/database.db` (see `Dockerfile`). |
| `FINNHUB_API_KEY` | Free key from [finnhub.io](https://finnhub.io) — live stock/ETF prices in **Investments → Stocks**. |

Stack: Flask app served by Gunicorn in the container; SQLite database file on the mounted volume.

Gunicorn runs **one worker with four threads**, not multiple worker processes: the live
price caches live in process memory, so a second worker would keep its own copy and double
the calls to the price APIs. Threads still handle concurrent requests.

## Layout

| Path | Contents |
|------|----------|
| `app.py` | Flask app setup, CSRF/login hooks, blueprint registration, boot |
| `config.py` | Constants, panel and section definitions |
| `db.py` | Connection, schema, migrations |
| `prices.py` | Finnhub / CoinGecko / yfinance lookups and their caches |
| `finance.py` | Balances, charts, holdings, recurring entry application |
| `helpers.py` | Normalizers, panel resolution, ownership checks |
| `excel_io.py` | Workbook export and import |
| `routes/` | One blueprint per feature area |
| `templates/panels/` | One partial per UI panel |
| `static/src/input.css` | Tailwind + daisyUI source (theme definitions) |
| `static/tailwind.css` | Compiled CSS — committed, served as-is |

## Development

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests run against a temporary SQLite file and stub out every external price
lookup, so they never touch a real database or the network.

### Styling

The theme lives in `static/src/input.css` (Tailwind v4 + daisyUI 5) and compiles
to `static/tailwind.css`, which is **committed**. Deploys therefore need no Node
and no build step — the Dockerfile just copies the compiled file like any other
static asset. You only need the toolchain to *change* the theme:

```bash
curl -sLo tools/tailwindcss.exe https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-windows-x64.exe
tools/tailwindcss.exe -i static/src/input.css -o static/tailwind.css --minify
```

`tools/daisyui.mjs` and `tools/daisyui-theme.mjs` are committed; the binary is
gitignored because it is ~110 MB. On Linux/macOS swap the release asset name
(`tailwindcss-linux-x64`, `tailwindcss-macos-arm64`, …) and drop the `.exe`.

daisyUI components are namespaced `d-*` (`d-btn`, `d-card`). The prefix exists
because daisyUI's unprefixed names collide with classes this app already
uses — `.card`, `.stats` and `.btn-secondary` all mean something else in
`styles.css`.

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
