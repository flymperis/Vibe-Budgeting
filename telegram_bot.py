"""Telegram bot: link accounts, record expenses/income/shared bills from text
and receipt photos, answer questions, and let entries be fixed with buttons.

Message understanding lives in telegram_parse, answers in telegram_ask; this
module is the Telegram side (polling, commands, buttons) and the database
writes."""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import string
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import integrations
import shared
import telegram_ask
import telegram_notify
import telegram_parse as tp

try:
    import fcntl
except ImportError:
    fcntl = None

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

POLL_IDLE_SLEEP = 10
POLL_ERROR_SLEEP = 5
POLL_LONG_TIMEOUT = 30
NOTIFY_EVERY = 60  # seconds between notification checks
ALLOWED_UPDATES = ["message", "callback_query"]
MAX_PHOTO_BYTES = 10 * 1024 * 1024

LINK_CODE_TTL_MINUTES = 15
LINK_CODE_ALPHABET = string.ascii_uppercase + string.digits

FALLBACK_CATEGORY_NAME = tp.FALLBACK_CATEGORY

BOT_COMMANDS = [
    ("help", "What the bot understands"),
    ("month", "This month's spending by category"),
    ("lastmonth", "Last month's spending by category"),
    ("balance", "Account balances"),
    ("owe", "Who owes whom in shared groups"),
    ("account", "Default account for new entries"),
    ("notifications", "Choose which reminders and summaries you get"),
    ("undo", "Remove the last entry made here"),
]


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
    # Every row the bot writes, so its buttons and /undo act on exactly that
    # row (and never on something entered on the web).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'shared')),
            row_id INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'ai',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_telegram_entries_user ON telegram_entries(user_id, id)")


# --- server config and linking ---------------------------------------------------


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
    return "Linked successfully. Try: καφές 3.50"


# --- categories and accounts ------------------------------------------------------


def _ensure_fallback_category(conn: sqlite3.Connection, user_id: int, *, expense: bool) -> str:
    table = "categories" if expense else "income_categories"
    uid = int(user_id)
    conn.execute(
        f"INSERT OR IGNORE INTO {table} (user_id, name) VALUES (?, ?)",
        (uid, FALLBACK_CATEGORY_NAME),
    )
    conn.commit()
    row = conn.execute(
        f"SELECT name FROM {table} WHERE user_id = ? AND lower(name) = lower(?)",
        (uid, FALLBACK_CATEGORY_NAME),
    ).fetchone()
    return row["name"] if row else FALLBACK_CATEGORY_NAME


def _ensure_salary_category(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO income_categories (user_id, name) VALUES (?, ?)",
        (int(user_id), "Salary"),
    )
    conn.commit()


def _lookup_category_id(conn: sqlite3.Connection, user_id: int, name: str, *, expense: bool) -> int:
    table = "categories" if expense else "income_categories"
    uid = int(user_id)
    rows = conn.execute(f"SELECT id, name FROM {table} WHERE user_id = ? ORDER BY name", (uid,)).fetchall()
    by_name = {r["name"]: int(r["id"]) for r in rows}
    matched = tp.best_name_match(name.strip(), list(by_name))
    if matched:
        return by_name[matched]
    fallback = _ensure_fallback_category(conn, uid, expense=expense)
    row = conn.execute(
        f"SELECT id FROM {table} WHERE user_id = ? AND lower(name) = lower(?)",
        (uid, fallback),
    ).fetchone()
    if row:
        return int(row["id"])
    raise ValueError(f"Category '{name}' not found and no fallback category exists.")


def _user_accounts(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """The user's real accounts (not the Shared balances ledger), by name."""
    ledger = shared.ledger_account_id(conn, user_id)
    return [
        {"id": int(r["id"]), "name": r["name"], "type": r["account_type"]}
        for r in conn.execute(
            "SELECT id, name, account_type FROM accounts WHERE user_id = ? ORDER BY name", (int(user_id),)
        ).fetchall()
        if int(r["id"]) != ledger
    ]


def _resolve_account_id(conn, user_id: int, detected: int | None, default_account_id: int | None) -> int | None:
    accounts = _user_accounts(conn, user_id)
    ids = {a["id"] for a in accounts}
    for candidate in (detected, default_account_id):
        if candidate and int(candidate) in ids:
            return int(candidate)
    return accounts[0]["id"] if accounts else None


def _user_context(conn: sqlite3.Connection, user_id: int) -> tuple[tp.Context, dict]:
    uid = int(user_id)
    _ensure_fallback_category(conn, uid, expense=True)
    _ensure_fallback_category(conn, uid, expense=False)
    _ensure_salary_category(conn, uid)
    expense_categories = [
        r["name"] for r in conn.execute("SELECT name FROM categories WHERE user_id = ? ORDER BY name", (uid,))
    ]
    income_categories = [
        r["name"] for r in conn.execute("SELECT name FROM income_categories WHERE user_id = ? ORDER BY name", (uid,))
    ]
    link = get_telegram_link(conn, uid)
    ctx = tp.Context(
        expense_categories=expense_categories,
        income_categories=income_categories,
        accounts=_user_accounts(conn, uid),
        history=tp.load_history(conn, uid),
        ai_settings=integrations.get_user_integrations(conn, uid),
    )
    extra = {
        "default_account_id": int(link["default_account_id"]) if link and link["default_account_id"] else None,
    }
    return ctx, extra


# --- writing entries ------------------------------------------------------------------


def _log_entry(conn, user_id: int, kind: str, row_id: int, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO telegram_entries (user_id, kind, row_id, source) VALUES (?, ?, ?, ?)",
        (int(user_id), kind, int(row_id), source),
    )
    return int(cur.lastrowid)


def save_transaction(conn, user_id: int, txn: tp.Txn, *, default_account_id: int | None = None) -> tuple[int | None, str]:
    """(telegram entry id, error message). Writes one expense or income row."""
    uid = int(user_id)
    account_id = _resolve_account_id(conn, uid, txn.account_id, default_account_id)
    if account_id is None:
        return None, "No account found. Add an account in Settings → Banks first."
    try:
        if txn.type == "income":
            category_id = _lookup_category_id(conn, uid, txn.category, expense=False)
            cur = conn.execute(
                """
                INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (uid, txn.notes, abs(txn.amount), category_id, account_id, txn.date),
            )
        else:
            category_id = _lookup_category_id(conn, uid, txn.category, expense=True)
            cur = conn.execute(
                """
                INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (uid, txn.notes, -abs(txn.amount), category_id, account_id, txn.date),
            )
    except ValueError as exc:
        return None, str(exc)
    entry_id = _log_entry(conn, uid, txn.type, int(cur.lastrowid), txn.source)
    conn.commit()
    return entry_id, ""


def save_shared(conn, user_id: int, txn: tp.Txn, plan: tp.SharedPlan, *, default_account_id: int | None = None) -> tuple[int | None, str]:
    """A shared bill split equally between the plan's participants, recorded
    in the user's own books like the web form does."""
    uid = int(user_id)
    cents = int(round(txn.amount * 100))
    member_ids = shared.group_member_ids(conn, plan.group_id)
    me = shared.member_person(conn, plan.group_id, uid)
    if me is None:
        return None, "You are not an active member of that group."
    entries = [(pid, pid in plan.participants, "") for pid in member_ids]
    try:
        splits = shared.compute_splits("equal", cents, entries)
    except ValueError as exc:
        return None, str(exc)
    my_share = sum(c for pid, c, _ in splits if pid == me)
    category_id = _lookup_category_id(conn, uid, txn.category, expense=True) if my_share > 0 else None
    account_id = None
    if plan.payer_is_me:
        account_id = _resolve_account_id(conn, uid, txn.account_id, default_account_id)
        if account_id is None:
            return None, "No account found. Add an account in Settings → Banks first."
    link = (category_id, account_id) if (plan.payer_is_me or my_share > 0) else None
    expense_id = shared.create_expense(
        conn, uid, plan.group_id, shared.clean_name(plan.description(txn.notes)) or "Expense", cents,
        plan.paid_by, "equal", txn.date, splits, link,
    )
    entry_id = _log_entry(conn, uid, "shared", expense_id, txn.source)
    conn.commit()
    return entry_id, ""


def _entry(conn, user_id: int, entry_id: int):
    return conn.execute(
        "SELECT * FROM telegram_entries WHERE id = ? AND user_id = ?", (int(entry_id), int(user_id))
    ).fetchone()


def _expense_row(conn, uid: int, entry) -> sqlite3.Row | None:
    if entry["kind"] == "expense":
        return conn.execute(
            """
            SELECT e.id, -e.amount AS amount, e.notes, date(e.spent_at) AS day, e.account_id, e.category_id,
                   c.name AS category, a.name AS account
            FROM expenses e JOIN categories c ON c.id = e.category_id LEFT JOIN accounts a ON a.id = e.account_id
            WHERE e.id = ? AND e.user_id = ?
            """,
            (entry["row_id"], uid),
        ).fetchone()
    if entry["kind"] == "income":
        return conn.execute(
            """
            SELECT i.id, i.amount, i.notes, date(i.received_at) AS day, i.account_id, i.category_id,
                   c.name AS category, a.name AS account
            FROM income_entries i JOIN income_categories c ON c.id = i.category_id
            LEFT JOIN accounts a ON a.id = i.account_id
            WHERE i.id = ? AND i.user_id = ?
            """,
            (entry["row_id"], uid),
        ).fetchone()
    return None


def _shared_state(conn, uid: int, entry) -> dict | None:
    e = conn.execute(
        """
        SELECT e.*, g.name AS group_name FROM shared_expenses e JOIN shared_groups g ON g.id = e.group_id
        WHERE e.id = ?
        """,
        (entry["row_id"],),
    ).fetchone()
    if not e or not shared.group_for_user(conn, uid, e["group_id"]):
        return None
    me = shared.member_person(conn, e["group_id"], uid)
    my_share, i_paid = shared.expense_involvement(conn, e["id"], me)
    link = conn.execute(
        """
        SELECT l.category_id, l.account_id, c.name AS category, a.name AS account
        FROM shared_expense_links l LEFT JOIN categories c ON c.id = l.category_id
        LEFT JOIN accounts a ON a.id = l.account_id
        WHERE l.expense_id = ? AND l.user_id = ?
        """,
        (e["id"], uid),
    ).fetchone()
    parts = conn.execute(
        "SELECT COUNT(*) AS n FROM shared_expense_splits WHERE expense_id = ? AND share_cents > 0", (e["id"],)
    ).fetchone()["n"]
    return {
        "row": e,
        "me": me,
        "my_share": my_share,
        "i_paid": i_paid,
        "link": link,
        "parts": int(parts),
        "payer": shared.person_display_name(conn, uid, e["paid_by"]),
    }


def _fmt_day(day: str) -> str:
    try:
        d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    except ValueError:
        return day
    today = datetime.now().date()
    if d == today:
        return "today"
    if d == today - timedelta(days=1):
        return "yesterday"
    return d.strftime("%a %d %b").replace(" 0", " ")


_SOURCE_NOTES = {
    "basic": "\n(basic parser — AI unavailable)",
    "receipt": "\n🧾 from receipt",
}


def render_entry(conn, user_id: int, entry) -> tuple[str, dict | None]:
    """Message text and buttons for one bot entry, read fresh from the DB."""
    uid = int(user_id)
    note = _SOURCE_NOTES.get(entry["source"], "")
    if entry["kind"] in ("expense", "income"):
        row = _expense_row(conn, uid, entry)
        if not row:
            return "This entry was removed.", None
        label = "Income" if entry["kind"] == "income" else "Expense"
        lines = [f"✅ {label} {float(row['amount']):.2f}€ · {row['category']}"]
        if row["notes"]:
            lines.append(f"📝 {row['notes']}")
        lines.append(f"📅 {_fmt_day(row['day'])} · 🏦 {row['account'] or '—'}")
        return "\n".join(lines) + note, _main_keyboard(entry["id"], account=True)
    state = _shared_state(conn, uid, entry)
    if not state:
        return "This shared bill was removed.", None
    e = state["row"]
    lines = [f"✅ Shared {e['amount_cents'] / 100:.2f}€ · {e['group_name']}", f"📝 {e['description']}"]
    who = "you" if state["i_paid"] else state["payer"]
    share = f"your share {state['my_share'] / 100:.2f}€"
    if state["link"] and state["link"]["category"] and state["my_share"]:
        share += f" → {state['link']['category']}"
    lines.append(f"👥 paid by {who} · split {state['parts']} ways · {share}")
    account = state["link"]["account"] if (state["link"] and state["i_paid"]) else None
    lines.append(f"📅 {_fmt_day(e['spent_on'])}" + (f" · 🏦 {account}" if account else ""))
    return "\n".join(lines) + note, _main_keyboard(entry["id"], account=state["i_paid"], category=state["my_share"] > 0)


def _main_keyboard(entry_id: int, *, account: bool = True, category: bool = True) -> dict:
    row1 = []
    if category:
        row1.append({"text": "🏷 Category", "callback_data": f"e:{entry_id}:c"})
    if account:
        row1.append({"text": "🏦 Account", "callback_data": f"e:{entry_id}:a"})
    row2 = [
        {"text": "📅 Date", "callback_data": f"e:{entry_id}:d"},
        {"text": "↩️ Undo", "callback_data": f"e:{entry_id}:u"},
    ]
    return {"inline_keyboard": [r for r in (row1, row2) if r]}


def _grid(buttons: list[dict], per_row: int) -> list[list[dict]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _back(entry_id: int) -> list[dict]:
    return [{"text": "« Back", "callback_data": f"e:{entry_id}:b"}]


def _category_keyboard(conn, uid: int, entry) -> dict:
    table = "income_categories" if entry["kind"] == "income" else "categories"
    rows = conn.execute(f"SELECT id, name FROM {table} WHERE user_id = ? ORDER BY name", (uid,)).fetchall()
    buttons = [{"text": r["name"][:30], "callback_data": f"e:{entry['id']}:C:{r['id']}"} for r in rows]
    return {"inline_keyboard": _grid(buttons, 3) + [_back(entry["id"])]}


def _account_keyboard(conn, uid: int, entry) -> dict:
    buttons = [
        {"text": a["name"][:30], "callback_data": f"e:{entry['id']}:A:{a['id']}"}
        for a in _user_accounts(conn, uid)
    ]
    return {"inline_keyboard": _grid(buttons, 2) + [_back(entry["id"])]}


def _date_keyboard(entry_id: int) -> dict:
    today = datetime.now().date()
    buttons = [
        {"text": label, "callback_data": f"e:{entry_id}:D:{offset}"}
        for offset, label in ((0, "Today"), (1, "Yesterday"), (2, (today - timedelta(days=2)).strftime("%a %d")),
                              (3, (today - timedelta(days=3)).strftime("%a %d")))
    ]
    return {"inline_keyboard": _grid(buttons, 2) + [_back(entry_id)]}


def _owns(conn, table: str, row_id: int, uid: int) -> bool:
    return bool(conn.execute(f"SELECT 1 FROM {table} WHERE id = ? AND user_id = ?", (int(row_id), uid)).fetchone())


def _set_category(conn, uid: int, entry, category_id: int) -> str:
    if entry["kind"] == "shared":
        if not _owns(conn, "categories", category_id, uid):
            return "Unknown category."
        state = _shared_state(conn, uid, entry)
        if not state:
            return "Bill not found."
        account_id = state["link"]["account_id"] if state["link"] else None
        shared.set_expense_link(conn, uid, entry["row_id"], category_id, account_id if state["i_paid"] else None)
    else:
        table, cat_table = ("income_entries", "income_categories") if entry["kind"] == "income" else ("expenses", "categories")
        if not _owns(conn, cat_table, category_id, uid):
            return "Unknown category."
        conn.execute(f"UPDATE {table} SET category_id = ? WHERE id = ? AND user_id = ?", (category_id, entry["row_id"], uid))
    conn.commit()
    return "Category changed"


def _set_account(conn, uid: int, entry, account_id: int) -> str:
    if account_id not in {a["id"] for a in _user_accounts(conn, uid)}:
        return "Unknown account."
    if entry["kind"] == "shared":
        state = _shared_state(conn, uid, entry)
        if not state or not state["i_paid"]:
            return "Only the payer's account can change."
        category_id = state["link"]["category_id"] if state["link"] else None
        shared.set_expense_link(conn, uid, entry["row_id"], category_id, account_id)
    else:
        table = "income_entries" if entry["kind"] == "income" else "expenses"
        conn.execute(f"UPDATE {table} SET account_id = ? WHERE id = ? AND user_id = ?", (account_id, entry["row_id"], uid))
    conn.commit()
    return "Account changed"


def _set_date(conn, uid: int, entry, days_ago: int) -> str:
    day = (datetime.now().date() - timedelta(days=max(0, min(days_ago, 31)))).isoformat()
    if entry["kind"] == "shared":
        conn.execute("UPDATE shared_expenses SET spent_on = ? WHERE id = ?", (day, entry["row_id"]))
        shared.relink_expense(conn, entry["row_id"])
    elif entry["kind"] == "income":
        conn.execute("UPDATE income_entries SET received_at = ? WHERE id = ? AND user_id = ?", (day, entry["row_id"], uid))
    else:
        conn.execute("UPDATE expenses SET spent_at = ? WHERE id = ? AND user_id = ?", (day, entry["row_id"], uid))
    conn.commit()
    return "Date changed"


def undo_entry(conn, user_id: int, entry) -> str:
    """Delete the row behind a bot entry; returns a one-line description."""
    uid = int(user_id)
    text, _ = render_entry(conn, uid, entry)
    summary = text.splitlines()[0].replace("✅ ", "")
    if entry["kind"] == "shared":
        row = conn.execute("SELECT user_id FROM shared_expenses WHERE id = ?", (entry["row_id"],)).fetchone()
        if row and int(row["user_id"]) == uid:
            shared.delete_expense(conn, entry["row_id"])
        elif row:
            return "Only the person who added this bill can remove it."
    elif entry["kind"] == "income":
        conn.execute("DELETE FROM income_entries WHERE id = ? AND user_id = ?", (entry["row_id"], uid))
    else:
        conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (entry["row_id"], uid))
    conn.execute("DELETE FROM telegram_entries WHERE id = ?", (entry["id"],))
    conn.commit()
    return f"↩️ Removed: {summary}"


def undo_last_entry(conn: sqlite3.Connection, user_id: int) -> str:
    uid = int(user_id)
    for entry in conn.execute(
        "SELECT * FROM telegram_entries WHERE user_id = ? ORDER BY id DESC LIMIT 20", (uid,)
    ).fetchall():
        text, _ = render_entry(conn, uid, entry)
        if text.startswith("✅"):
            return undo_entry(conn, uid, entry)
        conn.execute("DELETE FROM telegram_entries WHERE id = ?", (entry["id"],))
    conn.commit()
    return "Nothing to undo."


def format_balances(conn: sqlite3.Connection, user_id: int, balance_fn) -> str:
    return telegram_ask.format_balances(conn, int(user_id), balance_fn)


# --- Telegram transport ----------------------------------------------------------------


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


def send_message(chat_id: str | int, text: str, *, bot_token: str, reply_markup: dict | None = None) -> int | None:
    payload = {"chat_id": str(chat_id), "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data, _err = _telegram_api("sendMessage", payload, bot_token)
    if data and data.get("ok"):
        return int(data["result"]["message_id"])
    return None


def edit_message(chat_id, message_id: int, text: str, *, bot_token: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": str(chat_id), "message_id": int(message_id), "text": text[:4000]}
    payload["reply_markup"] = reply_markup or {"inline_keyboard": []}
    _telegram_api("editMessageText", payload, bot_token)


def edit_keyboard(chat_id, message_id: int, reply_markup: dict | None, *, bot_token: str) -> None:
    _telegram_api(
        "editMessageReplyMarkup",
        {"chat_id": str(chat_id), "message_id": int(message_id), "reply_markup": reply_markup or {"inline_keyboard": []}},
        bot_token,
    )


def answer_callback(callback_id: str, text: str = "", *, bot_token: str) -> None:
    _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]}, bot_token)


@contextmanager
def typing(chat_id, bot_token: str):
    """Show "typing…" in the chat while the AI works (Telegram clears it after ~5s)."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            _telegram_api("sendChatAction", {"chat_id": str(chat_id), "action": "typing"}, bot_token)
            stop.wait(4)

    thread = threading.Thread(target=loop, name="telegram-typing", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()


def download_file(file_id: str, bot_token: str) -> bytes | None:
    data, _err = _telegram_api("getFile", {"file_id": file_id}, bot_token)
    if not data or not data.get("ok"):
        return None
    info = data["result"]
    if int(info.get("file_size") or 0) > MAX_PHOTO_BYTES:
        return None
    url = f"https://api.telegram.org/file/bot{bot_token}/{info['file_path']}"
    try:
        with urlopen(Request(url, method="GET"), timeout=60) as resp:
            return resp.read(MAX_PHOTO_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError):
        return None


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


def set_bot_commands(config: dict) -> None:
    _telegram_api(
        "setMyCommands",
        {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]},
        config["bot_token"],
    )


def fetch_updates(config: dict, offset: int) -> tuple[list[dict], str | None]:
    data, err = _telegram_api_get(
        "getUpdates",
        config["bot_token"],
        {"offset": offset, "timeout": POLL_LONG_TIMEOUT, "allowed_updates": json.dumps(ALLOWED_UPDATES)},
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
    commands_token = None
    last_notify = 0.0
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
            if commands_token != config["bot_token"]:
                set_bot_commands(config)
                commands_token = config["bot_token"]
            if time.monotonic() - last_notify >= NOTIFY_EVERY:
                last_notify = time.monotonic()
                _run_notifications(get_connection, config)

            updates, err = fetch_updates(config, offset)
            if err:
                print(f"[telegram] Poll error: {err}", flush=True)
                time.sleep(POLL_ERROR_SLEEP)
                continue

            for update in updates:
                update_id = int(update.get("update_id", 0))
                if update_id >= offset:
                    offset = update_id + 1
                conn = get_connection()
                try:
                    handle_update(update, conn, balance_fn, config)
                except Exception:  # one bad update must not stop the bot
                    print(f"[telegram] Update {update_id} failed:\n{traceback.format_exc()}", flush=True)
                    _report_failure(update, config)
                finally:
                    conn.close()
    finally:
        if lock_fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass


def _run_notifications(get_connection: Callable, config: dict) -> None:
    conn = get_connection()
    try:
        telegram_notify.run(conn, _sender(config["bot_token"]))
    except Exception:  # a failing reminder must not stop the bot
        print(f"[telegram] Notifications failed:\n{traceback.format_exc()}", flush=True)
    finally:
        conn.close()


def _sender(bot_token: str):
    return lambda chat_id, text: send_message(chat_id, text, bot_token=bot_token) is not None


def _report_failure(update: dict, config: dict) -> None:
    chat = ((update.get("message") or {}).get("chat") or {}).get("id")
    if chat is None:
        chat = (((update.get("callback_query") or {}).get("message") or {}).get("chat") or {}).get("id")
    if chat is not None:
        send_message(chat, "Something went wrong with that message. Nothing was saved.", bot_token=config["bot_token"])


def start_poller(get_connection: Callable, balance_fn: Callable, lock_path: str) -> None:
    thread = threading.Thread(
        target=_poll_loop,
        args=(get_connection, balance_fn, lock_path),
        name="telegram-poller",
        daemon=True,
    )
    thread.start()


# --- handling updates -------------------------------------------------------------

HELP_TEXT = """Write what you spent or received, in Greek, Greeklish or English:
• καφές 3.50 · χθες σούπερ μάρκετ 42,80
• efood 18 ticket · βενζίνη 50 revolut
• καφές 3, σουβλάκια 12 (several at once)
• μισθός 1500 (income)
• σούπερ μάρκετ 60 μοιρασμένο (shared, split with your group)
• η Άννα πλήρωσε πίτσα 30 μισά (someone else paid)

Send a photo of a receipt to record its total.

Ask questions:
• πόσα ξόδεψα αυτόν τον μήνα;
• πόσα έδωσα σε delivery τον προηγούμενο μήνα;
• υπόλοιπο revolut · τι χρωστάω;

Each entry has buttons to change category, account or date, or undo it.

The bot also writes on its own: budget alerts, card and bill reminders,
a Sunday summary and a monthly one. /notifications turns each on or off.

Commands: /month /lastmonth /balance /owe /account /notifications /undo /unlink"""

START_TEXT = """Vibe Budgeting bot

1) In the app: Settings → Integrations → Generate link code
2) Here: /link YOURCODE
3) Then just write: καφές 3.50

/help shows everything the bot understands."""


def _linked_user(conn, chat_id) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT user_id, default_account_id FROM telegram_links WHERE chat_id = ?", (str(chat_id),)
    ).fetchone()


def handle_update(update: dict, conn: sqlite3.Connection, balance_fn, config: dict) -> None:
    if update.get("callback_query"):
        handle_callback(update["callback_query"], conn, config)
        return
    message = update.get("message")
    if not message:
        return
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return
    bot_token = config["bot_token"]
    text = str(message.get("text") or "").strip()
    username = str(message.get("from", {}).get("username") or "")

    if text.startswith("/start"):
        send_message(chat_id, START_TEXT, bot_token=bot_token)
        return
    if text.startswith("/help"):
        send_message(chat_id, HELP_TEXT, bot_token=bot_token)
        return
    if text.startswith("/link"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            send_message(chat_id, "Usage: /link AB12CD34", bot_token=bot_token)
            return
        send_message(chat_id, _link_account(conn, parts[1], str(chat_id), username), bot_token=bot_token)
        return

    link = _linked_user(conn, chat_id)
    if not link:
        send_message(chat_id, "Not linked. Generate a code in Settings → Integrations, then /link CODE", bot_token=bot_token)
        return
    user_id = int(link["user_id"])

    if text.startswith("/"):
        _handle_command(text, conn, user_id, chat_id, balance_fn, bot_token)
        return

    image_id = _image_file_id(message)
    if image_id:
        _handle_receipt(image_id, str(message.get("caption") or ""), conn, user_id, chat_id, bot_token)
        return
    if not text:
        return
    _handle_text(text, conn, user_id, chat_id, balance_fn, bot_token)


def _handle_command(text: str, conn, user_id: int, chat_id, balance_fn, bot_token: str) -> None:
    command = text.split()[0].split("@")[0].lower()
    if command == "/unlink":
        unlink_telegram(conn, user_id)
        send_message(chat_id, "Telegram unlinked.", bot_token=bot_token)
    elif command == "/balance":
        send_message(chat_id, format_balances(conn, user_id, balance_fn), bot_token=bot_token)
    elif command == "/undo":
        send_message(chat_id, undo_last_entry(conn, user_id), bot_token=bot_token)
    elif command == "/month":
        send_message(chat_id, telegram_ask.month_summary(conn, user_id), bot_token=bot_token)
    elif command == "/lastmonth":
        send_message(chat_id, telegram_ask.month_summary(conn, user_id, last=True), bot_token=bot_token)
    elif command in ("/owe", "/shared"):
        send_message(chat_id, telegram_ask.format_shared(conn, user_id), bot_token=bot_token)
    elif command == "/account":
        link = get_telegram_link(conn, user_id)
        current = link["default_account_name"] if link and link["default_account_name"] else None
        accounts = _user_accounts(conn, user_id)
        current = current or (accounts[0]["name"] + " (first by name)" if accounts else "none")
        buttons = [{"text": a["name"][:30], "callback_data": f"da:{a['id']}"} for a in accounts]
        send_message(
            chat_id,
            f"Default account for new entries: {current}\nPick another:",
            bot_token=bot_token,
            reply_markup={"inline_keyboard": _grid(buttons, 2)},
        )
    elif command in ("/notifications", "/notify"):
        send_message(chat_id, "Notifications — tap to turn on or off:", bot_token=bot_token,
                     reply_markup=_notify_keyboard(conn, user_id))
    else:
        send_message(chat_id, "Unknown command. /help lists what the bot understands.", bot_token=bot_token)


def _notify_keyboard(conn, user_id: int) -> dict:
    prefs = telegram_notify.get_prefs(conn, user_id)
    return {"inline_keyboard": [
        [{"text": ("✅ " if prefs[kind] else "▫️ ") + label.split(" (")[0], "callback_data": f"nt:{kind}"}]
        for kind, label in telegram_notify.KINDS.items()
    ]}


def _image_file_id(message: dict) -> str | None:
    photos = message.get("photo") or []
    if photos:
        return max(photos, key=lambda p: p.get("width", 0) * p.get("height", 0))["file_id"]
    doc = message.get("document") or {}
    if str(doc.get("mime_type") or "").startswith("image/"):
        return doc.get("file_id")
    return None


def _send_entry(conn, user_id: int, chat_id, entry_id: int, bot_token: str, message_id: int | None = None) -> None:
    entry = _entry(conn, user_id, entry_id)
    text, keyboard = render_entry(conn, user_id, entry)
    if message_id:
        edit_message(chat_id, message_id, text, bot_token=bot_token, reply_markup=keyboard)
    else:
        send_message(chat_id, text, bot_token=bot_token, reply_markup=keyboard)


def _record(conn, user_id: int, chat_id, txn: tp.Txn, plan, default_account_id, bot_token: str, message_id: int | None = None) -> None:
    if plan is not None and txn.type == "expense":
        entry_id, err = save_shared(conn, user_id, txn, plan, default_account_id=default_account_id)
    else:
        entry_id, err = save_transaction(conn, user_id, txn, default_account_id=default_account_id)
    if entry_id is None:
        if message_id:
            edit_message(chat_id, message_id, f"❌ {err}", bot_token=bot_token)
        else:
            send_message(chat_id, f"❌ {err}", bot_token=bot_token)
        return
    _send_entry(conn, user_id, chat_id, entry_id, bot_token, message_id)
    if txn.type == "expense":
        telegram_notify.after_entry(conn, user_id, chat_id, _sender(bot_token))


def _handle_text(text: str, conn, user_id: int, chat_id, balance_fn, bot_token: str) -> None:
    ctx, extra = _user_context(conn, user_id)
    if tp.looks_like_question(text):
        groups = shared.groups_for_bot(conn, user_id)
        with typing(chat_id, bot_token):
            plan = tp.plan_question(text, ctx, [g["name"] for g in groups])
            if plan.get("account"):
                plan["account_id"] = next((a["id"] for a in ctx.accounts if a["name"] == plan["account"]), None)
            reply = telegram_ask.answer(conn, user_id, plan, balance_fn, ctx.today)
        send_message(chat_id, reply, bot_token=bot_token)
        return

    with typing(chat_id, bot_token):
        txns = tp.parse_transactions(text, ctx)
    if not txns:
        send_message(chat_id, "I couldn't find an amount in that. Try: καφές 3.50 — or /help", bot_token=bot_token)
        return
    plan = None
    if len(txns) == 1 and txns[0].type == "expense":
        plan = tp.detect_shared(text, shared.groups_for_bot(conn, user_id))
        if isinstance(plan, str):
            send_message(chat_id, plan, bot_token=bot_token)
            return
    for txn in txns:
        _record(conn, user_id, chat_id, txn, plan, extra["default_account_id"], bot_token)


def _handle_receipt(file_id: str, caption: str, conn, user_id: int, chat_id, bot_token: str) -> None:
    ctx, extra = _user_context(conn, user_id)
    ai = ctx.ai_settings
    ocr_model = (ai.get("ai_ocr_model") or "").strip()
    if not (ai.get("ai_enabled") and ai.get("ai_base_url") and ocr_model):
        send_message(
            chat_id,
            "Receipt photos are off. Set a Receipt OCR model (e.g. glm-ocr) in Settings → Integrations.",
            bot_token=bot_token,
        )
        return
    status_id = send_message(chat_id, "🧾 Reading the receipt…", bot_token=bot_token)

    def finish(text: str) -> None:
        if status_id:
            edit_message(chat_id, status_id, text, bot_token=bot_token)
        else:
            send_message(chat_id, text, bot_token=bot_token)

    with typing(chat_id, bot_token):
        image = download_file(file_id, bot_token)
        if not image or len(image) > MAX_PHOTO_BYTES:
            finish("Couldn't download that photo (max 10 MB).")
            return
        ocr_text = integrations.ollama_ocr(ai["ai_base_url"], ocr_model, base64.b64encode(image).decode())
        if not ocr_text:
            finish("The OCR model didn't answer. Check the Receipt OCR model in Settings → Integrations.")
            return
        txn = tp.parse_receipt(ocr_text, caption, ctx)
    if not txn:
        finish("Couldn't find a total on that receipt. Send it as text instead, e.g. σούπερ μάρκετ 19.01")
        return
    plan = tp.detect_shared(caption, shared.groups_for_bot(conn, user_id)) if caption else None
    if isinstance(plan, str):
        finish(plan)
        return
    _record(conn, user_id, chat_id, txn, plan, extra["default_account_id"], bot_token, message_id=status_id)


def handle_callback(cb: dict, conn, config: dict) -> None:
    bot_token = config["bot_token"]
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    data = str(cb.get("data") or "")
    link = _linked_user(conn, chat_id) if chat_id is not None else None
    if not link or not message_id:
        answer_callback(cb["id"], "Not linked.", bot_token=bot_token)
        return
    uid = int(link["user_id"])
    parts = data.split(":")

    if parts[0] == "da" and len(parts) == 2 and parts[1].isdigit():
        ok = int(parts[1]) in {a["id"] for a in _user_accounts(conn, uid)} and set_default_account(conn, uid, int(parts[1]))
        name = next((a["name"] for a in _user_accounts(conn, uid) if a["id"] == int(parts[1])), "?")
        answer_callback(cb["id"], "Saved" if ok else "Unknown account", bot_token=bot_token)
        if ok:
            edit_message(chat_id, message_id, f"Default account for new entries: {name}", bot_token=bot_token)
        return

    if parts[0] == "nt" and len(parts) == 2 and parts[1] in telegram_notify.KINDS:
        prefs = telegram_notify.get_prefs(conn, uid)
        prefs[parts[1]] = not prefs[parts[1]]
        telegram_notify.save_prefs(conn, uid, prefs)
        edit_keyboard(chat_id, message_id, _notify_keyboard(conn, uid), bot_token=bot_token)
        answer_callback(cb["id"], "On" if prefs[parts[1]] else "Off", bot_token=bot_token)
        return

    if parts[0] != "e" or len(parts) < 3 or not parts[1].isdigit():
        answer_callback(cb["id"], bot_token=bot_token)
        return
    entry = _entry(conn, uid, int(parts[1]))
    if not entry:
        answer_callback(cb["id"], "This entry is gone.", bot_token=bot_token)
        edit_keyboard(chat_id, message_id, None, bot_token=bot_token)
        return
    op, arg = parts[2], (parts[3] if len(parts) > 3 else "")
    toast = ""
    if op == "c":
        edit_keyboard(chat_id, message_id, _category_keyboard(conn, uid, entry), bot_token=bot_token)
    elif op == "a":
        edit_keyboard(chat_id, message_id, _account_keyboard(conn, uid, entry), bot_token=bot_token)
    elif op == "d":
        edit_keyboard(chat_id, message_id, _date_keyboard(entry["id"]), bot_token=bot_token)
    elif op == "u":
        edit_message(chat_id, message_id, undo_entry(conn, uid, entry), bot_token=bot_token)
        toast = "Removed"
    elif op in ("C", "A", "D", "b"):
        if op == "C" and arg.isdigit():
            toast = _set_category(conn, uid, entry, int(arg))
        elif op == "A" and arg.isdigit():
            toast = _set_account(conn, uid, entry, int(arg))
        elif op == "D" and arg.isdigit():
            toast = _set_date(conn, uid, entry, int(arg))
        text, keyboard = render_entry(conn, uid, entry)
        edit_message(chat_id, message_id, text, bot_token=bot_token, reply_markup=keyboard)
    answer_callback(cb["id"], toast, bot_token=bot_token)
