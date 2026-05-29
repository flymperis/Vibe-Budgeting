"""Telegram bot: link accounts, parse messages, record expenses/income."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime, timedelta
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import integrations

try:
    import fcntl
except ImportError:
    fcntl = None

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

POLL_IDLE_SLEEP = 10
POLL_ERROR_SLEEP = 5
POLL_LONG_TIMEOUT = 30


def migrate_telegram(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_links (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            chat_id TEXT NOT NULL UNIQUE,
            telegram_username TEXT NOT NULL DEFAULT '',
            default_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_link_codes (
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_server_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            bot_token TEXT NOT NULL DEFAULT '',
            webhook_secret TEXT NOT NULL DEFAULT '',
            public_base_url TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("INSERT OR IGNORE INTO telegram_server_config (id) VALUES (1)")


LINK_CODE_TTL_MINUTES = 15
LINK_CODE_ALPHABET = string.ascii_uppercase + string.digits

INCOME_HINT_RE = re.compile(
    r"\b(?:μισθ(?:ός|οσ)|salary|income|έσοδ(?:α|ο)|εσοδ(?:α|ο)|paycheck|bonus)\b",
    re.I,
)
TEXT_AMOUNT_RE = re.compile(
    r"^(.+?)\s+(-?\d+(?:[.,]\d{1,2})?)\s*(?:€|eur(?:o?s)?|ευρώ|ευρω)?\s*$",
    re.I,
)
AMOUNT_TEXT_RE = re.compile(
    r"^(-?\d+(?:[.,]\d{1,2})?)\s*(?:€|eur(?:o?s)?|ευρώ|ευρω)?\s+(.+)$",
    re.I,
)


def _env_server_config() -> dict:
    bot_token = TELEGRAM_BOT_TOKEN
    return {
        "bot_token": bot_token,
        "has_bot_token": bool(bot_token),
    }


def default_server_config() -> dict:
    return {
        "bot_token": "",
        "has_bot_token": False,
    }


def get_server_config(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT bot_token
        FROM telegram_server_config
        WHERE id = 1
        """
    ).fetchone()
    env_cfg = _env_server_config()
    if not row:
        return env_cfg
    bot_token = (row["bot_token"] or "").strip() or env_cfg["bot_token"]
    return {
        "bot_token": bot_token,
        "has_bot_token": bool(bot_token),
    }


def is_configured(config: dict) -> bool:
    return bool(config.get("bot_token"))


def server_config_for_form(conn: sqlite3.Connection) -> dict:
    cfg = get_server_config(conn)
    return {
        "bot_token_set": cfg["has_bot_token"],
    }


def parse_server_config_form(form, existing: dict) -> dict:
    bot_token = (form.get("bot_token") or "").strip()
    if not bot_token:
        bot_token = existing.get("bot_token") or ""
    if not bot_token:
        raise ValueError("Bot token is required")
    return {
        "bot_token": bot_token,
        "has_bot_token": True,
    }


def save_server_config(conn: sqlite3.Connection, settings: dict) -> None:
    conn.execute(
        """
        UPDATE telegram_server_config
        SET bot_token = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """,
        (settings["bot_token"],),
    )
    conn.commit()


def clear_webhook(config: dict) -> tuple[bool, str]:
    """Drop any webhook so Telegram accepts getUpdates (polling)."""
    if not config.get("bot_token"):
        return False, "Bot token is missing"
    data, err = _telegram_api(
        "deleteWebhook",
        {"drop_pending_updates": False},
        config["bot_token"],
    )
    if err:
        return False, err
    if data and data.get("ok"):
        return True, "Webhook cleared — polling mode active"
    return False, data.get("description", "deleteWebhook failed") if data else "deleteWebhook failed"


def get_bot_username(config: dict) -> str | None:
    data, _err = _telegram_api("getMe", {}, config["bot_token"])
    if data and data.get("ok"):
        return data.get("result", {}).get("username")
    return None


def get_telegram_link(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT tl.*, a.name AS default_account_name
        FROM telegram_links tl
        LEFT JOIN accounts a ON a.id = tl.default_account_id
        WHERE tl.user_id = ?
        """,
        (int(user_id),),
    ).fetchone()


def get_active_link_code(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT code, expires_at FROM telegram_link_codes
        WHERE user_id = ? AND expires_at > ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (int(user_id), datetime.now().isoformat(timespec="seconds")),
    ).fetchone()


def create_link_code(conn: sqlite3.Connection, user_id: int) -> str:
    uid = int(user_id)
    conn.execute("DELETE FROM telegram_link_codes WHERE user_id = ?", (uid,))
    code = "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(8))
    expires = (datetime.now() + timedelta(minutes=LINK_CODE_TTL_MINUTES)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO telegram_link_codes (code, user_id, expires_at) VALUES (?, ?, ?)",
        (code, uid, expires),
    )
    conn.commit()
    return code


def set_default_account(conn: sqlite3.Connection, user_id: int, account_id: int | None) -> bool:
    uid = int(user_id)
    if account_id is not None:
        ok = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
            (int(account_id), uid),
        ).fetchone()
        if not ok:
            return False
    cur = conn.execute(
        "UPDATE telegram_links SET default_account_id = ? WHERE user_id = ?",
        (account_id, uid),
    )
    conn.commit()
    return cur.rowcount > 0


def unlink_telegram(conn: sqlite3.Connection, user_id: int) -> None:
    uid = int(user_id)
    conn.execute("DELETE FROM telegram_links WHERE user_id = ?", (uid,))
    conn.execute("DELETE FROM telegram_link_codes WHERE user_id = ?", (uid,))
    conn.commit()


def _parse_amount(raw: str) -> float | None:
    try:
        return float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _best_name_match(name: str, options: list[str]) -> str | None:
    needle = name.strip().lower()
    if not needle or not options:
        return None
    lowered = {opt.lower(): opt for opt in options}
    if needle in lowered:
        return lowered[needle]
    for key, original in lowered.items():
        if needle in key or key in needle:
            return original
    return None


def _parse_with_regex(text: str) -> dict | None:
    cleaned = " ".join(text.split())
    if not cleaned:
        return None

    tx_type = "income" if INCOME_HINT_RE.search(cleaned) else "expense"
    label = cleaned
    amount_raw = None

    m = TEXT_AMOUNT_RE.match(cleaned)
    if m:
        label, amount_raw = m.group(1).strip(), m.group(2)
    else:
        m = AMOUNT_TEXT_RE.match(cleaned)
        if m:
            amount_raw, label = m.group(1), m.group(2).strip()

    amount = _parse_amount(amount_raw) if amount_raw else None
    if amount is None or amount == 0:
        return None

    label = INCOME_HINT_RE.sub("", label).strip(" -–—,")
    if not label:
        label = "General"

    return {
        "type": tx_type,
        "amount": abs(amount),
        "category": label,
        "account": None,
        "notes": "",
        "date": datetime.now().date().isoformat(),
        "source": "regex",
    }


def _parse_with_ollama(
    text: str,
    ai_settings: dict,
    expense_categories: list[str],
    income_categories: list[str],
    accounts: list[str],
) -> dict | None:
    if not ai_settings.get("ai_enabled"):
        return None
    base_url = (ai_settings.get("ai_base_url") or "").strip().rstrip("/")
    model = (ai_settings.get("ai_model") or "").strip()
    timeout = int(ai_settings.get("ai_timeout") or integrations.DEFAULT_AI_TIMEOUT)
    if not base_url or not model:
        return None

    today = datetime.now().date().isoformat()
    prompt = f"""You parse short budget messages into JSON for a personal finance app.
Today is {today}.

Expense categories: {json.dumps(expense_categories, ensure_ascii=False)}
Income categories: {json.dumps(income_categories, ensure_ascii=False)}
Accounts: {json.dumps(accounts, ensure_ascii=False)}

User message: {json.dumps(text, ensure_ascii=False)}

Rules:
- type is "expense" or "income"
- amount is a positive number (no currency symbol)
- category must match an existing category name when possible, otherwise a short new label
- account is an exact account name from the list, or null for default
- notes is extra detail or empty string
- date is YYYY-MM-DD (use today if not specified)

Return ONLY valid JSON with keys: type, amount, category, account, notes, date"""

    raw = integrations.ollama_chat(base_url, model, prompt, timeout=timeout, json_mode=True)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    tx_type = str(parsed.get("type", "")).strip().lower()
    if tx_type not in ("expense", "income"):
        return None
    amount = _parse_amount(parsed.get("amount"))
    if amount is None or amount <= 0:
        return None

    date_raw = parsed.get("date")
    movement_date = today
    if date_raw:
        text_date = str(date_raw).strip()
        if len(text_date) >= 10:
            try:
                datetime.strptime(text_date[:10], "%Y-%m-%d")
                movement_date = text_date[:10]
            except ValueError:
                pass

    return {
        "type": tx_type,
        "amount": abs(amount),
        "category": str(parsed.get("category", "General")).strip() or "General",
        "account": parsed.get("account"),
        "notes": str(parsed.get("notes", "")).strip(),
        "date": movement_date,
        "source": "ollama",
    }


def parse_message(
    text: str,
    ai_settings: dict,
    expense_categories: list[str],
    income_categories: list[str],
    accounts: list[str],
) -> dict | None:
    parsed = _parse_with_regex(text)
    if parsed:
        cats = income_categories if parsed["type"] == "income" else expense_categories
        matched = _best_name_match(parsed["category"], cats)
        if matched:
            parsed["category"] = matched
            return parsed

    ollama_parsed = _parse_with_ollama(
        text, ai_settings, expense_categories, income_categories, accounts
    )
    if ollama_parsed:
        cats = income_categories if ollama_parsed["type"] == "income" else expense_categories
        matched = _best_name_match(ollama_parsed["category"], cats)
        if matched:
            ollama_parsed["category"] = matched
        return ollama_parsed

    return parsed


def _lookup_category_id(
    conn: sqlite3.Connection, user_id: int, name: str, *, expense: bool
) -> int:
    table = "categories" if expense else "income_categories"
    uid = int(user_id)
    row = conn.execute(
        f"SELECT id FROM {table} WHERE user_id = ? AND lower(name) = lower(?)",
        (uid, name.strip()),
    ).fetchone()
    if row:
        return int(row["id"])
    conn.execute(f"INSERT INTO {table} (user_id, name) VALUES (?, ?)", (uid, name.strip()))
    return int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])


def _resolve_account_id(
    conn: sqlite3.Connection,
    user_id: int,
    parsed_account,
    default_account_id: int | None,
) -> int | None:
    uid = int(user_id)
    if parsed_account:
        row = conn.execute(
            "SELECT id FROM accounts WHERE user_id = ? AND lower(name) = lower(?)",
            (uid, str(parsed_account).strip()),
        ).fetchone()
        if row:
            return int(row["id"])
    if default_account_id:
        row = conn.execute(
            "SELECT id FROM accounts WHERE id = ? AND user_id = ?",
            (int(default_account_id), uid),
        ).fetchone()
        if row:
            return int(row["id"])
    row = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? ORDER BY name LIMIT 1",
        (uid,),
    ).fetchone()
    return int(row["id"]) if row else None


def save_transaction(
    conn: sqlite3.Connection,
    user_id: int,
    parsed: dict,
    *,
    default_account_id: int | None = None,
) -> tuple[bool, str, str]:
    uid = int(user_id)
    account_id = _resolve_account_id(conn, uid, parsed.get("account"), default_account_id)
    if account_id is None:
        return False, "No account found. Add an account in Settings → Banks first.", ""

    account_row = conn.execute("SELECT name FROM accounts WHERE id = ?", (account_id,)).fetchone()
    account_name = account_row["name"] if account_row else "account"

    tx_type = parsed["type"]
    amount = float(parsed["amount"])
    notes = str(parsed.get("notes") or "").strip()
    movement_date = parsed.get("date") or datetime.now().date().isoformat()

    if tx_type == "income":
        category_id = _lookup_category_id(conn, uid, parsed["category"], expense=False)
        conn.execute(
            """
            INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uid, notes, amount, category_id, account_id, movement_date),
        )
        conn.commit()
        return True, f"Income recorded: {parsed['category']} {amount:.2f}€", account_name

    category_id = _lookup_category_id(conn, uid, parsed["category"], expense=True)
    conn.execute(
        """
        INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (uid, notes, -abs(amount), category_id, account_id, movement_date),
    )
    conn.commit()
    return True, f"Expense recorded: {parsed['category']} {amount:.2f}€", account_name


def undo_last_entry(conn: sqlite3.Connection, user_id: int) -> str:
    uid = int(user_id)
    last_expense = conn.execute(
        """
        SELECT e.id, e.amount, c.name AS category_name, e.spent_at
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.user_id = ?
        ORDER BY e.id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()
    last_income = conn.execute(
        """
        SELECT i.id, i.amount, c.name AS category_name, i.received_at
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        WHERE i.user_id = ?
        ORDER BY i.id DESC LIMIT 1
        """,
        (uid,),
    ).fetchone()

    pick = None
    if last_expense and last_income:
        pick = ("expense", last_expense) if last_expense["id"] > last_income["id"] else ("income", last_income)
    elif last_expense:
        pick = ("expense", last_expense)
    elif last_income:
        pick = ("income", last_income)

    if not pick:
        return "Nothing to undo."

    kind, row = pick
    if kind == "expense":
        conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (row["id"], uid))
        conn.commit()
        return f"Removed expense: {row['category_name']} {abs(row['amount']):.2f}€"
    conn.execute("DELETE FROM income_entries WHERE id = ? AND user_id = ?", (row["id"], uid))
    conn.commit()
    return f"Removed income: {row['category_name']} {row['amount']:.2f}€"


def format_balances(conn: sqlite3.Connection, user_id: int, balance_fn) -> str:
    uid = int(user_id)
    cutoff = (datetime.now().date() + timedelta(days=1)).isoformat()
    rows = balance_fn(conn, cutoff, uid)
    if not rows:
        return "No accounts yet."
    lines = ["Balances:"]
    for row in rows:
        lines.append(f"• {row['name']}: {row['current_balance']:.2f}€")
    return "\n".join(lines)


def _telegram_api_get(
    method: str, bot_token: str, params: dict | None = None
) -> tuple[dict | None, str | None]:
    if not bot_token:
        return None, "Bot token is missing. Paste the token from @BotFather and save again."
    query = urlencode(params or {})
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    if query:
        url = f"{url}?{query}"
    req = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=POLL_LONG_TIMEOUT + 10) as resp:
            return json.loads(resp.read().decode()), None
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
            desc = detail.get("description") or str(exc)
        except (json.JSONDecodeError, OSError):
            desc = str(exc)
        return None, f"Telegram API HTTP {exc.code}: {desc}"
    except URLError as exc:
        return None, f"Could not reach Telegram API: {exc.reason}"
    except (json.JSONDecodeError, OSError, TimeoutError) as exc:
        return None, f"Could not reach Telegram API: {exc}"


def _telegram_api(method: str, payload: dict, bot_token: str) -> tuple[dict | None, str | None]:
    if not bot_token:
        return None, "Bot token is missing. Paste the token from @BotFather and save again."
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        f"https://api.telegram.org/bot{bot_token}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()), None
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
            desc = detail.get("description") or str(exc)
        except (json.JSONDecodeError, OSError):
            desc = str(exc)
        return None, f"Telegram API HTTP {exc.code}: {desc}"
    except URLError as exc:
        return None, f"Could not reach Telegram API: {exc.reason}"
    except (json.JSONDecodeError, OSError, TimeoutError) as exc:
        return None, f"Could not reach Telegram API: {exc}"


def send_message(chat_id: str | int, text: str, *, bot_token: str) -> None:
    _telegram_api("sendMessage", {"chat_id": str(chat_id), "text": text}, bot_token)


def test_telegram_connection(config: dict) -> tuple[bool, str]:
    if not config.get("bot_token"):
        return False, "Bot token is missing. Paste the token from @BotFather and save again."
    data, err = _telegram_api("getMe", {}, config["bot_token"])
    if err:
        return False, err
    if not data or not data.get("ok"):
        return False, data.get("description", "getMe failed") if data else "getMe failed"
    username = data.get("result", {}).get("username") or "?"
    return True, f"Connected to @{username}"


def fetch_updates(config: dict, offset: int) -> tuple[list[dict], str | None]:
    data, err = _telegram_api_get(
        "getUpdates",
        config["bot_token"],
        {"offset": offset, "timeout": POLL_LONG_TIMEOUT},
    )
    if err:
        return [], err
    if not data or not data.get("ok"):
        desc = data.get("description", "getUpdates failed") if data else "getUpdates failed"
        return [], desc
    return data.get("result") or [], None


def _poll_loop(get_connection: Callable, balance_fn: Callable, lock_path: str) -> None:
    lock_fd = None
    if fcntl is not None:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            print("[telegram] Poller skipped — another worker is already polling.", flush=True)
            return
    else:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock_fd, str(os.getpid()).encode())
        except FileExistsError:
            print("[telegram] Poller skipped — lock file exists.", flush=True)
            return

    print("[telegram] Polling started (Tailscale-safe, no public URL).", flush=True)
    offset = 0
    try:
        while True:
            conn = get_connection()
            try:
                config = get_server_config(conn)
            finally:
                conn.close()

            if not is_configured(config):
                time.sleep(POLL_IDLE_SLEEP)
                continue

            updates, err = fetch_updates(config, offset)
            if err:
                print(f"[telegram] Poll error: {err}", flush=True)
                time.sleep(POLL_ERROR_SLEEP)
                continue

            for update in updates:
                update_id = int(update.get("update_id", 0))
                conn = get_connection()
                try:
                    handle_update(update, conn, balance_fn, config)
                finally:
                    conn.close()
                if update_id >= offset:
                    offset = update_id + 1
    finally:
        if lock_fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def start_poller(get_connection: Callable, balance_fn: Callable, lock_path: str) -> None:
    thread = threading.Thread(
        target=_poll_loop,
        args=(get_connection, balance_fn, lock_path),
        name="telegram-poller",
        daemon=True,
    )
    thread.start()


def _link_account(conn: sqlite3.Connection, code: str, chat_id: str, username: str) -> str:
    row = conn.execute(
        "SELECT user_id, expires_at FROM telegram_link_codes WHERE code = ?",
        (code.strip().upper(),),
    ).fetchone()
    if not row:
        return "Invalid or expired code. Generate a new one in Settings → Integrations."
    expires = datetime.fromisoformat(str(row["expires_at"]))
    if datetime.now() > expires:
        conn.execute("DELETE FROM telegram_link_codes WHERE code = ?", (code.strip().upper(),))
        conn.commit()
        return "Code expired. Generate a new one in Settings → Integrations."

    uid = int(row["user_id"])
    existing = conn.execute(
        "SELECT user_id FROM telegram_links WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()
    if existing and int(existing["user_id"]) != uid:
        return "This Telegram chat is already linked to another user."

    conn.execute("DELETE FROM telegram_links WHERE user_id = ?", (uid,))
    conn.execute(
        """
        INSERT INTO telegram_links (user_id, chat_id, telegram_username)
        VALUES (?, ?, ?)
        """,
        (uid, str(chat_id), username or ""),
    )
    conn.execute("DELETE FROM telegram_link_codes WHERE code = ?", (code.strip().upper(),))
    conn.commit()
    return "Linked successfully. Send: supermarket 20"


def _user_context(conn: sqlite3.Connection, user_id: int) -> dict:
    uid = int(user_id)
    ai_settings = integrations.get_user_integrations(conn, uid)
    expense_categories = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM categories WHERE user_id = ? ORDER BY name", (uid,)
        ).fetchall()
    ]
    income_categories = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM income_categories WHERE user_id = ? ORDER BY name", (uid,)
        ).fetchall()
    ]
    accounts = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM accounts WHERE user_id = ? ORDER BY name", (uid,)
        ).fetchall()
    ]
    link = get_telegram_link(conn, uid)
    return {
        "ai_settings": ai_settings,
        "expense_categories": expense_categories,
        "income_categories": income_categories,
        "accounts": accounts,
        "default_account_id": int(link["default_account_id"]) if link and link["default_account_id"] else None,
    }


def handle_update(update: dict, conn: sqlite3.Connection, balance_fn, config: dict) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message or "text" not in message:
        return

    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return

    text = str(message["text"]).strip()
    username = str(message.get("from", {}).get("username") or "")
    bot_token = config["bot_token"]

    if text.startswith("/start"):
        send_message(
            chat_id,
            "Vibe Budgeting bot\n\n"
            "1) Settings → Integrations → Generate link code\n"
            "2) Send: /link YOURCODE\n"
            "3) Send entries: supermarket 20\n\n"
            "Commands: /help /balance /undo /unlink",
            bot_token=bot_token,
        )
        return

    if text.startswith("/help"):
        send_message(
            chat_id,
            "Examples:\n"
            "• supermarket 20\n"
            "• 45 sklavenitis\n"
            "• salary 1500\n\n"
            "With AI enabled in Integrations:\n"
            "• χθες καφές 3.50\n\n"
            "Commands: /link /balance /undo /unlink",
            bot_token=bot_token,
        )
        return

    if text.startswith("/link"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Usage: /link AB12CD34", bot_token=bot_token)
            return
        send_message(chat_id, _link_account(conn, parts[1], str(chat_id), username), bot_token=bot_token)
        return

    link = conn.execute(
        "SELECT user_id, default_account_id FROM telegram_links WHERE chat_id = ?",
        (str(chat_id),),
    ).fetchone()
    if not link:
        send_message(
            chat_id,
            "Not linked. Generate a code in Settings → Integrations, then /link CODE",
            bot_token=bot_token,
        )
        return

    user_id = int(link["user_id"])

    if text.startswith("/unlink"):
        unlink_telegram(conn, user_id)
        send_message(chat_id, "Telegram unlinked.", bot_token=bot_token)
        return

    if text.startswith("/balance"):
        send_message(chat_id, format_balances(conn, user_id, balance_fn), bot_token=bot_token)
        return

    if text.startswith("/undo"):
        send_message(chat_id, undo_last_entry(conn, user_id), bot_token=bot_token)
        return

    ctx = _user_context(conn, user_id)
    parsed = parse_message(
        text,
        ctx["ai_settings"],
        ctx["expense_categories"],
        ctx["income_categories"],
        ctx["accounts"],
    )
    if not parsed:
        hint = "Try: supermarket 20"
        if not ctx["ai_settings"].get("ai_enabled"):
            hint += "\n(Enable AI in Integrations for free-form messages.)"
        send_message(chat_id, f"Could not parse.\n{hint}", bot_token=bot_token)
        return

    ok, reply, account_name = save_transaction(
        conn,
        user_id,
        parsed,
        default_account_id=ctx["default_account_id"],
    )
    if ok:
        source = parsed.get("source", "?")
        send_message(chat_id, f"✅ {reply}\n({source}, {account_name})", bot_token=bot_token)
    else:
        send_message(chat_id, f"❌ {reply}", bot_token=bot_token)
