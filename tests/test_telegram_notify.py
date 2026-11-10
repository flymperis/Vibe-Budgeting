"""Telegram notifications: budget alerts, card and bill reminders, weekly and
monthly summaries, sent once each, only when switched on and at sane hours."""

from datetime import date, datetime, time, timedelta

import app as vb_app
import budget
import telegram_notify as tn

from .conftest import csrf_token, register_and_login
from .test_telegram_bot import _button, _linked_user, _press, _send, tg  # noqa: F401  (tg is a fixture)


def _conn():
    return vb_app.get_connection()


def _ids(uid):
    conn = _conn()
    acc = conn.execute("SELECT id FROM accounts WHERE user_id = ? AND name = 'Revolut'", (uid,)).fetchone()["id"]
    cat = conn.execute("SELECT id FROM categories WHERE user_id = ? AND name = 'Delivery'", (uid,)).fetchone()["id"]
    conn.close()
    return acc, cat


def _expense(uid, amount, day, account_id, category_id):
    conn = _conn()
    conn.execute(
        "INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at) VALUES (?, 'x', ?, ?, ?, ?)",
        (uid, -abs(amount), category_id, account_id, day.isoformat()),
    )
    conn.commit()
    conn.close()


def _budget(uid, category_id, amount):
    conn = _conn()
    budget.set_enabled(conn, uid, True)
    budget.set_default(conn, uid, category_id, amount)
    conn.commit()
    conn.close()


class Outbox:
    def __init__(self):
        self.messages = []

    def __call__(self, chat_id, text):
        self.messages.append(text)
        return True


def _run(at: datetime):
    out = Outbox()
    conn = _conn()
    try:
        tn.run(conn, out, now=at)
    finally:
        conn.close()
    return out.messages


def _mine(messages, needle):
    return [m for m in messages if needle in m]


def test_budget_alerts_once_per_threshold_and_not_at_night(client):
    uid = _linked_user(client, "nt_budget")
    acc, cat = _ids(uid)
    _budget(uid, cat, 100)
    today = date.today()
    _expense(uid, 85, today, acc, cat)

    assert _mine(_run(datetime.combine(today, time(23, 30))), "Budget alert") == []  # quiet hours
    first = _mine(_run(datetime.combine(today, time(8, 5))), "Budget alert")
    assert len(first) == 1 and "🟠 Delivery: 85.00€ of 100.00€ (85%)" in first[0]
    assert _mine(_run(datetime.combine(today, time(8, 6))), "Budget alert") == []  # sent once

    _expense(uid, 20, today, acc, cat)
    over = _mine(_run(datetime.combine(today, time(12, 0))), "Budget alert")
    assert len(over) == 1 and "🔴 Delivery: 105.00€ of 100.00€ — 5.00€ over" in over[0]


def test_no_budget_alerts_when_budgets_are_off_or_alerts_disabled(client):
    uid = _linked_user(client, "nt_budget_off")
    acc, cat = _ids(uid)
    _budget(uid, cat, 50)
    _expense(uid, 60, date.today(), acc, cat)
    conn = _conn()
    prefs = tn.get_prefs(conn, uid)
    tn.save_prefs(conn, uid, {**prefs, "budget": False})
    conn.close()
    assert _mine(_run(datetime.combine(date.today(), time(10, 0))), "Budget alert") == []
    conn = _conn()
    tn.save_prefs(conn, uid, {**prefs, "budget": True})
    budget.set_enabled(conn, uid, False)
    conn.commit()
    conn.close()
    assert _mine(_run(datetime.combine(date.today(), time(10, 0))), "Budget alert") == []


def test_bot_entry_that_crosses_a_budget_gets_an_alert_right_away(client, tg):
    uid = _linked_user(client, "nt_after_entry")
    _, cat = _ids(uid)
    _budget(uid, cat, 20)
    _send("efood 18")
    assert "Budget alert" in tg.last_text() and "Delivery: 18.00€ of 20.00€" in tg.last_text()


def _card(uid, statement_day=1, due_day=15):
    conn = _conn()
    cur = conn.execute(
        """INSERT INTO accounts (user_id, name, opening_balance, account_type, credit_limit, statement_day, due_day)
           VALUES (?, 'Visa', 0, 'credit', 1000, ?, ?)""",
        (uid, statement_day, due_day),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def _card_notes(uid, today, days=3):
    conn = _conn()
    try:
        notes = tn.card_reminders(conn, uid, today, days)
        for n in notes:
            tn._mark(conn, uid, n.keys)
        return [n.text for n in notes]
    finally:
        conn.close()


def test_card_reminders_before_on_and_after_the_due_day_until_paid(client):
    uid = _linked_user(client, "nt_card")
    _, cat = _ids(uid)
    card = _card(uid)
    _expense(uid, 200, date(2026, 10, 20), card, cat)  # closes on 1 Nov, due 15 Nov

    assert _card_notes(uid, date(2026, 11, 11)) == []
    assert _card_notes(uid, date(2026, 11, 12)) == ["💳 Visa: 200.00€ due Sun 15 Nov (in 3 days).\nRecord the payment as a transfer into the card."]
    assert _card_notes(uid, date(2026, 11, 12)) == []  # once
    assert "due today" in _card_notes(uid, date(2026, 11, 15))[0]
    assert "was due yesterday" in _card_notes(uid, date(2026, 11, 16))[0]

    conn = _conn()
    revolut, _ = _ids(uid)
    conn.execute(
        "INSERT INTO account_transfers (user_id, from_account_id, to_account_id, amount, transferred_at) VALUES (?, ?, ?, 200, '2026-12-01')",
        (uid, revolut, card),
    )
    conn.commit()
    conn.close()
    _expense(uid, 50, date(2026, 11, 20), card, cat)  # next statement: 50, due 15 Dec
    assert _card_notes(uid, date(2026, 12, 12)) == ["💳 Visa: 50.00€ due Tue 15 Dec (in 3 days).\nRecord the payment as a transfer into the card."]


def test_bill_reminder_the_day_before_with_a_shortfall_warning(client):
    uid = _linked_user(client, "nt_bills")
    acc, cat = _ids(uid)
    today = date(2026, 11, 10)
    conn = _conn()
    conn.execute(
        """INSERT INTO recurring_entries (user_id, entry_type, amount, category_id, account_id, day_of_month,
                                          notes, interval_unit, interval_count, start_date)
           VALUES (?, 'expense', 450, ?, ?, 11, 'Rent', 'month', 1, '2026-01-01')""",
        (uid, cat, acc),
    )
    conn.commit()
    note = tn.bill_reminders(conn, uid, today)
    assert note.text == "📅 Due tomorrow: Rent: 450.00€ from Revolut\n⚠️ Revolut has 0.00€, 450.00€ short."
    tn._mark(conn, uid, note.keys)
    assert tn.bill_reminders(conn, uid, today) is None
    assert tn.bill_reminders(conn, uid, date(2026, 11, 9)) is None
    conn.close()


def test_weekly_summary_only_on_sunday_evening(client):
    uid = _linked_user(client, "nt_weekly")
    acc, cat = _ids(uid)
    sunday = date(2026, 11, 8)
    _expense(uid, 30, sunday - timedelta(days=2), acc, cat)
    _expense(uid, 20, sunday - timedelta(days=9), acc, cat)  # previous week
    conn = _conn()
    try:
        assert not any(k.startswith("weekly") for n in tn.due_notes(conn, uid, datetime(2026, 11, 7, 21)) for k in n.keys)
        assert not any(k.startswith("weekly") for n in tn.due_notes(conn, uid, datetime(2026, 11, 8, 19)) for k in n.keys)
        notes = [n for n in tn.due_notes(conn, uid, datetime(2026, 11, 8, 20, 1)) if n.keys[0].startswith("weekly")]
        assert notes[0].text.startswith("🗓 Week 02 Nov – 08 Nov: 30.00€ spent")
        assert "Top: Delivery 30.00€" in notes[0].text
        tn._mark(conn, uid, notes[0].keys)
        assert not any(n.keys[0].startswith("weekly") for n in tn.due_notes(conn, uid, datetime(2026, 11, 8, 22)))
    finally:
        conn.close()


def test_monthly_summary_on_the_first_with_catch_up(client):
    uid = _linked_user(client, "nt_monthly")
    acc, cat = _ids(uid)
    _budget(uid, cat, 100)
    _expense(uid, 120, date(2026, 10, 5), acc, cat)
    _expense(uid, 60, date(2026, 9, 5), acc, cat)
    conn = _conn()
    try:
        early = [n for n in tn.due_notes(conn, uid, datetime(2026, 11, 1, 8)) if n.keys[0].startswith("monthly")]
        assert early == []
        notes = [n for n in tn.due_notes(conn, uid, datetime(2026, 11, 2, 9, 30)) if n.keys[0].startswith("monthly")]
        text = notes[0].text
        assert text.startswith("📊 October 2026: 120.00€")
        assert "September: 60.00€ (▲100% this month vs then)" in text
        assert "Over: Delivery +20.00€" in text
        tn._mark(conn, uid, notes[0].keys)
        assert not [n for n in tn.due_notes(conn, uid, datetime(2026, 11, 3, 9, 30)) if n.keys[0].startswith("monthly")]
        assert not [n for n in tn.due_notes(conn, uid, datetime(2026, 12, 4, 9, 30)) if n.keys[0].startswith("monthly")]
    finally:
        conn.close()


def test_notifications_command_toggles_each_kind(client, tg):
    uid = _linked_user(client, "nt_toggle")
    _send("/notifications")
    keyboard = tg.last_keyboard()
    _press(_button(keyboard, "✅ Weekly summary"))
    conn = _conn()
    assert tn.get_prefs(conn, uid)["weekly"] is False
    conn.close()
    _button(tg.last_keyboard(), "▫️ Weekly summary")


def test_notification_settings_form(client, tg):
    uid = _linked_user(client, "nt_form")
    resp = client.post("/settings/telegram/notifications", data={
        "notify_budget": "1", "notify_monthly": "1", "card_days": "5", "_csrf_token": csrf_token(client),
    })
    assert resp.status_code == 302
    conn = _conn()
    prefs = tn.get_prefs(conn, uid)
    conn.execute("UPDATE telegram_server_config SET bot_token = 'T' WHERE id = 1")  # the form shows once a bot exists
    conn.commit()
    conn.close()
    assert prefs == {"budget": True, "cards": False, "bills": False, "weekly": False, "monthly": True, "card_days": 5}
    try:
        page = client.get("/?panel=settings&settings_section=integrations").get_data(as_text=True)
    finally:
        conn = _conn()
        conn.execute("UPDATE telegram_server_config SET bot_token = '' WHERE id = 1")
        conn.commit()
        conn.close()
    assert 'name="notify_weekly"' in page and 'name="card_days"' in page and 'value="5"' in page
