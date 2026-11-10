"""Messages the Telegram bot sends on its own: budget alerts, credit-card and
bill reminders, and weekly/monthly summaries.

Each message is keyed (e.g. "budget:2026-11:12:warn") and logged in
telegram_notifications once sent, so it goes out once however often run()
is called. Users switch each kind on or off in Settings or with
/notifications. Times are the server's local time (set TZ in the container).
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Callable

import budget
import finance
import telegram_ask

KINDS = {
    "budget": "Budget alerts (80% and 100% of a category)",
    "cards": "Credit-card payment reminders",
    "bills": "Recurring bills due tomorrow",
    "weekly": "Weekly summary (Sunday evening)",
    "monthly": "Monthly summary (1st of the month)",
}
DEFAULT_CARD_DAYS = 3
QUIET_UNTIL_HOUR = 8  # budget alerts wait until 08:00
QUIET_FROM_HOUR = 23  # ... and stop at 23:00
DAILY_HOUR = 9  # card and bill reminders, monthly summary
WEEKLY_HOUR = 20  # Sunday summary
MONTHLY_CATCH_UP_DAYS = 3  # monthly summary still goes out on the 2nd/3rd if missed


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_notify_prefs (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            budget INTEGER NOT NULL DEFAULT 1,
            cards INTEGER NOT NULL DEFAULT 1,
            bills INTEGER NOT NULL DEFAULT 1,
            weekly INTEGER NOT NULL DEFAULT 1,
            monthly INTEGER NOT NULL DEFAULT 1,
            card_days INTEGER NOT NULL DEFAULT 3 CHECK (card_days BETWEEN 1 AND 14)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_notifications (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, key)
        )
        """
    )


# --- preferences ------------------------------------------------------------------


def get_prefs(conn, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM telegram_notify_prefs WHERE user_id = ?", (int(user_id),)).fetchone()
    prefs = {kind: True for kind in KINDS}
    prefs["card_days"] = DEFAULT_CARD_DAYS
    if row:
        prefs.update({kind: bool(row[kind]) for kind in KINDS})
        prefs["card_days"] = int(row["card_days"])
    return prefs


def save_prefs(conn, user_id: int, prefs: dict) -> None:
    days = max(1, min(14, int(prefs.get("card_days") or DEFAULT_CARD_DAYS)))
    conn.execute(
        """
        INSERT INTO telegram_notify_prefs (user_id, budget, cards, bills, weekly, monthly, card_days)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            budget = excluded.budget, cards = excluded.cards, bills = excluded.bills,
            weekly = excluded.weekly, monthly = excluded.monthly, card_days = excluded.card_days
        """,
        (int(user_id), *(1 if prefs.get(kind) else 0 for kind in KINDS), days),
    )
    conn.commit()


def parse_prefs_form(form) -> dict:
    prefs = {kind: form.get(f"notify_{kind}") == "1" for kind in KINDS}
    try:
        prefs["card_days"] = int(str(form.get("card_days", DEFAULT_CARD_DAYS)).strip())
    except ValueError:
        raise ValueError("Days before the card payment must be a number.") from None
    if not 1 <= prefs["card_days"] <= 14:
        raise ValueError("Days before the card payment must be between 1 and 14.")
    return prefs


# --- the log ------------------------------------------------------------------------


def _sent(conn, uid: int, key: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM telegram_notifications WHERE user_id = ? AND key = ?", (uid, key)
    ).fetchone())


def _mark(conn, uid: int, keys) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO telegram_notifications (user_id, key) VALUES (?, ?)",
        [(uid, k) for k in keys],
    )
    conn.commit()


class Note:
    """One message and the keys it settles once delivered."""

    def __init__(self, text: str, keys: list[str]):
        self.text = text
        self.keys = keys


def _eur(value: float) -> str:
    return f"{value:,.2f}€".replace(",", " ")


def _day(d: date, today: date) -> str:
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return d.strftime("%a %d %b").replace(" 0", " ")


# --- budget alerts -------------------------------------------------------------------


def budget_alerts(conn, uid: int, today: date) -> Note | None:
    """Categories that crossed 80% or 100% of their budget this month and
    were not reported yet, in one message."""
    if not budget.is_enabled(conn, uid):
        return None
    ym = today.strftime("%Y-%m")
    data = budget.budget_for_month(conn, uid, ym)
    items = [(r["category_id"], r) for r in data["rows"]]
    if data["other"]:
        items.append(("other", data["other"]))
    lines, keys = [], []
    for cid, r in items:
        if r["budget"] <= 0:
            continue
        if r["spent"] > r["budget"]:
            level = "over"
        elif r["spent"] >= r["budget"] * budget.WARN_RATIO:
            level = "warn"
        else:
            continue
        wanted = [f"budget:{ym}:{cid}:warn"] + ([f"budget:{ym}:{cid}:over"] if level == "over" else [])
        if all(_sent(conn, uid, k) for k in wanted):
            continue
        keys += wanted
        if level == "over":
            lines.append(f"🔴 {r['name']}: {_eur(r['spent'])} of {_eur(r['budget'])} — {_eur(r['spent'] - r['budget'])} over")
        else:
            lines.append(f"🟠 {r['name']}: {_eur(r['spent'])} of {_eur(r['budget'])} ({r['pct']:.0f}%) — {_eur(r['remaining'])} left")
    if not lines:
        return None
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_left = (next_month - today).days
    return Note(f"Budget alert · {today.strftime('%B')}\n" + "\n".join(lines) + f"\n{days_left} days left this month.", keys)


# --- credit cards ---------------------------------------------------------------------


def _card_statements(conn, uid: int, today: date) -> list[tuple[dict, dict]]:
    rows = finance.fetch_account_balances_through(conn, (today + timedelta(days=1)).isoformat(), uid)
    out = []
    for row in rows:
        if not finance.is_credit_card(row) or not row["statement_day"] or not row["due_day"]:
            continue
        statement = finance.credit_card_status(conn, uid, row, today)["statement"]
        if statement and statement["due_on"] and statement["remaining"] > 0.005:
            out.append((row, statement))
    return out


def card_reminders(conn, uid: int, today: date, days_before: int) -> list[Note]:
    notes = []
    for row, st in _card_statements(conn, uid, today):
        due = st["due_on"]
        left = (due - today).days
        key = f"card:{row['id']}:{due.isoformat()}:{left}"
        if left not in (days_before, 0, -1) or _sent(conn, uid, key):
            continue
        amount = _eur(st["remaining"])
        if left == -1:
            text = f"💳 {row['name']}: the {amount} statement was due yesterday and isn't marked as paid."
        elif left == 0:
            text = f"💳 {row['name']}: {amount} is due today."
        else:
            text = f"💳 {row['name']}: {amount} due {_day(due, today)} (in {left} days)."
        notes.append(Note(text + "\nRecord the payment as a transfer into the card.", [key]))
    return notes


# --- recurring bills -----------------------------------------------------------------


def bills_due(conn, uid: int, on: date) -> list[dict]:
    """Enabled recurring expenses that fall due on `on` and are not posted yet."""
    rules = conn.execute(
        """
        SELECT r.*, a.name AS account_name, c.name AS category_name
        FROM recurring_entries r
        LEFT JOIN accounts a ON a.id = r.account_id
        LEFT JOIN categories c ON c.id = r.category_id
        WHERE r.user_id = ? AND r.enabled = 1 AND r.entry_type = 'expense'
        """,
        (uid,),
    ).fetchall()
    due = []
    for rule in rules:
        start_d = finance._parse_iso_date(rule["start_date"])
        if start_d is None:
            continue
        not_before = finance._parse_iso_date(rule["not_before"])
        unit = rule["interval_unit"] if rule["interval_unit"] in finance.RECURRING_UNITS else "month"
        times = finance.normalize_optional_positive_int(rule["months_to_run"])
        for when, key in finance.recurring_occurrences(unit, rule["interval_count"], start_d, rule["day_of_month"], on, times):
            if when != on or (not_before and when < not_before):
                continue
            posted = conn.execute(
                "SELECT 1 FROM recurring_applied WHERE recurring_id = ? AND ym = ?", (rule["id"], key)
            ).fetchone()
            if not posted:
                due.append(rule)
    return due


def bill_reminders(conn, uid: int, today: date) -> Note | None:
    tomorrow = today + timedelta(days=1)
    rules = [r for r in bills_due(conn, uid, tomorrow) if not _sent(conn, uid, f"bill:{r['id']}:{tomorrow.isoformat()}")]
    if not rules:
        return None
    balances = {int(r["id"]): float(r["current_balance"])
                for r in finance.fetch_account_balances_through(conn, tomorrow.isoformat(), uid)}
    needed: dict[int, float] = {}
    for r in rules:
        needed[int(r["account_id"])] = needed.get(int(r["account_id"]), 0.0) + abs(float(r["amount"]))
    lines = []
    for r in rules:
        label = (r["notes"] or "").strip() or r["category_name"] or "Bill"
        lines.append(f"• {label}: {_eur(abs(float(r['amount'])))} from {r['account_name'] or '?'}")
    for account_id, amount in needed.items():
        have = balances.get(account_id)
        if have is not None and have < amount:
            name = next((r["account_name"] for r in rules if int(r["account_id"]) == account_id), "?")
            lines.append(f"⚠️ {name} has {_eur(have)}, {_eur(amount - have)} short.")
    if len(rules) == 1:
        text = "📅 Due tomorrow: " + lines[0][2:] + "".join("\n" + extra for extra in lines[1:])
    else:
        text = "📅 Due tomorrow:\n" + "\n".join(lines)
    return Note(text, [f"bill:{r['id']}:{tomorrow.isoformat()}" for r in rules])


# --- summaries ----------------------------------------------------------------------


def _spent(conn, uid: int, start: date, end: date) -> float:
    row = conn.execute(
        "SELECT COALESCE(-SUM(amount), 0) AS t FROM expenses WHERE user_id = ? AND date(spent_at) BETWEEN date(?) AND date(?)",
        (uid, start.isoformat(), end.isoformat()),
    ).fetchone()
    return float(row["t"] or 0)


def _change(now: float, before: float, label: str = "") -> str:
    """" (▲12% vs …)", or "" when there is nothing to compare with."""
    if before <= 0.005:
        return ""
    pct = (now - before) / before * 100
    arrow = "▲" if pct > 0 else "▼"
    return f" ({arrow}{abs(pct):.0f}%{' ' + label if label else ''})"


def weekly_summary(conn, uid: int, today: date) -> Note:
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    spent = _spent(conn, uid, start, end)
    previous = [_spent(conn, uid, start - timedelta(weeks=w), end - timedelta(weeks=w)) for w in range(1, 9)]
    average = sum(previous) / len(previous)
    lines = [f"🗓 Week {start.strftime('%d %b')} – {end.strftime('%d %b')}: {_eur(spent)} spent"]
    if average > 0.005:
        lines[0] += _change(spent, average, "vs your 8-week average")
    top = telegram_ask._breakdown(conn, uid, start, end)[:3]
    if top:
        lines.append("Top: " + ", ".join(f"{name} {_eur(v)}" for name, v in top))
    if budget.is_enabled(conn, uid):
        data = budget.budget_for_month(conn, uid, today.strftime("%Y-%m"))
        if data["total_budget"] > 0:
            lines.append(f"Month so far: {_eur(data['total_spent'])} of {_eur(data['total_budget'])} budget ({data['total_pct']:.0f}%)")
            over = [r["name"] for r in data["over"]]
            if over:
                lines.append("Over budget: " + ", ".join(over))
    for row, st in _card_statements(conn, uid, today):
        if (st["due_on"] - today).days <= 7:
            lines.append(f"💳 {row['name']}: {_eur(st['remaining'])} due {_day(st['due_on'], today)}")
    upcoming = []
    for offset in range(1, 8):
        for r in bills_due(conn, uid, today + timedelta(days=offset)):
            label = (r["notes"] or "").strip() or r["category_name"] or "Bill"
            upcoming.append(f"{label} {_eur(abs(float(r['amount'])))} ({_day(today + timedelta(days=offset), today)})")
    if upcoming:
        lines.append("Coming up: " + ", ".join(upcoming))
    return Note("\n".join(lines), [f"weekly:{start.isoformat()}"])


def monthly_summary(conn, uid: int, today: date) -> Note:
    end = today.replace(day=1) - timedelta(days=1)
    start = end.replace(day=1)
    before_end = start - timedelta(days=1)
    before_start = before_end.replace(day=1)
    text = telegram_ask.format_breakdown(conn, uid, start, end, today, title=f"📊 {start.strftime('%B %Y')}")
    spent, previous = _spent(conn, uid, start, end), _spent(conn, uid, before_start, before_end)
    extra = []
    if previous > 0.005:
        extra.append(f"{before_start.strftime('%B')}: {_eur(previous)}" + _change(spent, previous, "this month vs then"))
    if budget.is_enabled(conn, uid):
        data = budget.budget_for_month(conn, uid, start.strftime("%Y-%m"))
        if data["total_budget"] > 0:
            extra.append(f"Budget: {_eur(data['total_spent'])} of {_eur(data['total_budget'])} ({data['total_pct']:.0f}%)")
            if data["over"]:
                extra.append("Over: " + ", ".join(f"{r['name']} +{_eur(r['spent'] - r['budget'])}" for r in data["over"]))
            else:
                extra.append("Every category stayed within budget. 👏")
    return Note(text + ("\n" + "\n".join(extra) if extra else ""), [f"monthly:{start.strftime('%Y-%m')}"])


# --- scheduling -----------------------------------------------------------------------


def due_notes(conn, uid: int, now: datetime) -> list[Note]:
    """Everything this user should get at `now` and hasn't yet."""
    prefs = get_prefs(conn, uid)
    today = now.date()
    notes: list[Note] = []
    if prefs["budget"] and QUIET_UNTIL_HOUR <= now.hour < QUIET_FROM_HOUR:
        note = budget_alerts(conn, uid, today)
        if note:
            notes.append(note)
    if now.hour >= DAILY_HOUR:
        if prefs["cards"]:
            notes += card_reminders(conn, uid, today, prefs["card_days"])
        if prefs["bills"]:
            note = bill_reminders(conn, uid, today)
            if note:
                notes.append(note)
        if prefs["monthly"] and today.day <= MONTHLY_CATCH_UP_DAYS:
            month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
            if not _sent(conn, uid, f"monthly:{month}"):
                notes.append(monthly_summary(conn, uid, today))
    if prefs["weekly"] and today.weekday() == 6 and now.hour >= WEEKLY_HOUR:
        week = (today - timedelta(days=6)).isoformat()
        if not _sent(conn, uid, f"weekly:{week}"):
            notes.append(weekly_summary(conn, uid, today))
    return notes


def deliver(conn, uid: int, chat_id, notes: list[Note], send: Callable[[str, str], bool]) -> int:
    sent = 0
    for note in notes:
        if send(chat_id, note.text):
            _mark(conn, uid, note.keys)
            sent += 1
    return sent


def run(conn, send: Callable[[str, str], bool], now: datetime | None = None) -> int:
    """Send whatever is due to every linked user. send(chat_id, text) → delivered?"""
    now = now or datetime.now()
    total = 0
    for link in conn.execute("SELECT user_id, chat_id FROM telegram_links").fetchall():
        uid = int(link["user_id"])
        telegram_ask._post_recurring(conn, uid)
        total += deliver(conn, uid, link["chat_id"], due_notes(conn, uid, now), send)
    return total


def after_entry(conn, uid: int, chat_id, send: Callable[[str, str], bool]) -> None:
    """Budget alert right after the bot records an expense (the user is
    clearly awake, so no quiet hours)."""
    if get_prefs(conn, uid)["budget"]:
        note = budget_alerts(conn, uid, datetime.now().date())
        if note:
            deliver(conn, uid, chat_id, [note], send)
