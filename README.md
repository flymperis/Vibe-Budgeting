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
| `TZ` | Time zone such as `Europe/Athens`. Sets what "today" is for new entries and when Telegram reminders and summaries go out; the container defaults to UTC. |

Stack: Flask app served by Gunicorn in the container; SQLite database file on the mounted volume.

Gunicorn runs **one worker with four threads**, not multiple worker processes: the live
price caches live in process memory, so a second worker would keep its own copy and double
the calls to the price APIs. Threads still handle concurrent requests.

## Install on iPhone

Vibe Budgeting is a PWA (web app manifest + icons), so Safari can add it to the
Home Screen as a standalone app (no browser chrome):

1. Open the app in **Safari** (Chrome/other browsers on iOS can't add
   standalone web apps).
2. Tap **Share** → **Add to Home Screen** → **Add**.
3. Launch it from the Home Screen icon.

This works over the same URL you already use day-to-day, including a plain
`http://` Tailscale address — the standalone, full-screen launch behavior
doesn't require HTTPS. Some other PWA capabilities (e.g. service workers,
which this app doesn't use) are restricted by iOS to HTTPS or `localhost`, so
if you add other PWA features later, keep that in mind.

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

## Backups

The app backs up its database every night at 03:30 (server time, see `TZ`), and straight away on start-up if the
newest copy is more than a day old. Copies go to `backups/` next to the database (`./budget-data/backups` with the
Docker setup above), are checked with SQLite's integrity check, and are named `vibe-budgeting-YYYY-MM-DD.db`. The
14 newest daily copies are kept, plus the last copy of each of the 12 months before; nothing else in the folder is
touched.

| Variable | Purpose |
|----------|---------|
| `VB_BACKUP_DIR` | Folder for backups (default: `backups` next to the database). Point it at another disk to survive a disk failure. |
| `VB_BACKUP_KEEP_DAYS` | Daily copies to keep (default 14; `0` turns nightly backups off). |
| `VB_BACKUP_KEEP_MONTHS` | Monthly copies to keep (default 12). |
| `VB_BACKUP_TIME` | Time of the nightly backup, `HH:MM` (default `03:30`). |

Take a copy by hand, e.g. before an upgrade (the newest 5 per label are kept):

```bash
docker compose exec vibe-budgeting python -m backup --label predeploy
```

To restore: stop the container, copy the backup over the database, start it again.

```bash
docker compose stop
cp budget-data/backups/vibe-budgeting-2026-11-10.db budget-data/database.db
rm -f budget-data/database.db-wal budget-data/database.db-shm
docker compose start
```

## Tags

Write `#tags` in the notes of any expense or income, e.g. `καφές #Λονδίνο2026` or `hotel #trip2026`, to see what a
trip, a party or a project cost in total, whatever category each entry is in. **Reports → Tags** lists every tag with
its spending, income and dates; open one for its breakdown by category and every entry. Tags in notes are clickable,
and your recent tags appear under the notes field so one tap adds them. They are ordinary text in the notes, so they
also work in recurring rules, shared bills (your share keeps the tag) and Excel export/import. Case and accents don't
matter: `#Διακοπές2026` and `#διακοπες2026` are the same tag.

## Integrations (Ollama + Telegram)

Per-user **Ollama** settings and **Telegram bot token** are in **Settings → Integrations** (saved in the database).

Optional env var `TELEGRAM_BOT_TOKEN` works as a fallback if the DB field is empty.

### Ollama

Run Ollama on your home server (default port **11434**). In Docker with `network_mode: service:tailscale-personal`, use:

```
http://127.0.0.1:11434
```

Set Base URL and model in **Integrations → Local AI**, then **Test connection**. A small instruct model such as
`qwen3:8b` works well (thinking is switched off, replies take ~1s once the model is loaded; how long it stays
loaded is left to the Ollama server's `OLLAMA_KEEP_ALIVE`). For receipt photos also set
**Receipt OCR model**, e.g. `glm-ocr`.

### Voice messages (Whisper)

Voice messages to the bot are turned into text by a local Whisper server with an OpenAI-compatible API, e.g.
[speaches](https://github.com/speaches-ai/speaches) (faster-whisper), then handled exactly like a typed message.
Nothing leaves your network. Run it next to Ollama:

```
podman run -d --name whisper -p 8000:8000 \
  -v whisper-models:/home/ubuntu/.cache/huggingface/hub \
  ghcr.io/speaches-ai/speaches:latest-cpu      # or :latest-cuda with an NVIDIA GPU
```

Each user turns it on in **Vibe Budgeting X → Settings → Integrations → Voice messages** (URL, model, language,
**Test**); both apps share the same database, so the bot here uses those settings. For users who never set it,
these environment variables are the fallback:

| Variable | Default | |
|---|---|---|
| `VB_WHISPER_URL` | (empty = voice off) | e.g. `http://127.0.0.1:8000` |
| `VB_WHISPER_MODEL` | `deepdml/faster-whisper-large-v3-turbo-ct2` | smaller and faster on CPU: `Systran/faster-whisper-small` (weaker Greek) |
| `VB_WHISPER_LANGUAGE` | `el` | empty lets Whisper guess the language |

Download the model once: `curl -X POST http://127.0.0.1:8000/v1/models/deepdml/faster-whisper-large-v3-turbo-ct2`. Your category and account names are sent to
Whisper as hints, so `Revolut` or `Ticket Restaurant` come out spelled the way the app knows them.

### Telegram bot (polling — Tailscale-only)

No public URL or Tailscale Funnel needed. The app **polls** Telegram outbound (`getUpdates`) — nothing is exposed to the internet.

1. Create a bot with [@BotFather](https://t.me/BotFather) → copy the token.
2. **Settings → Integrations → Telegram bot** → paste token → **Test bot token** → **Save**.
3. **Generate link code** → in Telegram: `/link YOURCODE` → send `καφές 3.50`.

The budget app stays on Tailscale only (`http://personal-disk-share:5000`). Telegram works via outbound HTTPS to `api.telegram.org`.

### Telegram usage

| Message | What happens |
|---------|--------|
| `χθες καφές 3.50`, `efood 18 ticket` | Expense; date words and account names (also Greeklish, e.g. `άλφα visa`, `μετρητά`) are picked up |
| `καφές 3, σουβλάκια 12` | Several entries from one message |
| `μισθός 1500` | Income |
| `σούπερ μάρκετ 60 μοιρασμένο` | Shared bill split equally in your group (name the group if you have several) |
| `η Άννα πλήρωσε πίτσα 30 μισά` | Shared bill someone else paid, split between you two |
| voice message | Transcribed by Whisper, shown as «Heard: …», then handled like the same text (entries or questions) |
| photo of a receipt | Total, date and shop read via the OCR model; a caption can name the account or say it's shared |
| `πόσα ξόδεψα σε delivery τον προηγούμενο μήνα;` | Answered from the database (the AI only plans the lookup) |
| `καφές 3 #Λονδίνο2026` | Entry with a tag |
| `/tag Λονδίνο2026` … `/tag off` | Tag every new entry while it's on (handy for a trip) |
| `πόσα κόστισαν οι #Λονδίνο2026;` or `/tags` | What a tag (or each tag) cost, by category |
| `/month` `/lastmonth` `/balance` `/owe` `/account` `/notifications` `/undo` `/help` | Commands |

The bot also writes on its own (each can be switched off in **Settings → Integrations** or with `/notifications`):

| Notification | When |
|---|---|
| Budget alert | A category passes 80% or 100% of its budget (08:00–23:00, and right after a bot entry) — needs budgets switched on |
| Card payment | N days before the due date (default 3), on the day, and the day after if unpaid — needs a statement and due day on the card |
| Bills | 09:00 the day before a recurring expense, with a warning if the account is short |
| Weekly summary | Sunday 20:00: the week vs your 8-week average, top categories, budget so far, what's coming up |
| Monthly summary | 1st of the month 09:00 (or the 2nd/3rd if the server was down): last month by category, vs the month before, budget result |

Every entry comes with buttons to change its category, account or date, or undo it. The bot works with whatever categories you create, in Greek or English
("Σούπερ μάρκετ", "Groceries", "Super Market" all catch `σούπερ μάρκετ 20`), and files unmatched items under your own
catch-all (Other, General, Λοιπά, …). Categories follow your own past
choices: re-file `efood` under another category once and the bot uses that from then on. Without AI (or if Ollama is
unreachable) simple messages are still recorded by a basic parser.
