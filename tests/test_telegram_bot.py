"""Telegram bot: parsing, recording, buttons, undo, shared bills, receipts and
questions. Telegram and Ollama are replaced by fakes; the database is real."""

import itertools
import json
from datetime import date, timedelta

import pytest

import app as vb_app
import integrations
import shared
import telegram_bot
import telegram_parse as tp

from .conftest import csrf_token, register_and_login

TODAY = date.today()
_chat_ids = itertools.count(555000)
_current = {"chat": None}


# --- pure parsing -----------------------------------------------------------------


def test_amounts_skip_dates_times_units_and_percentages():
    assert [a.value for a in tp.find_amounts("χθες καφές 3.50")] == [3.5]
    assert [a.value for a in tp.find_amounts("καφές 3, βενζίνη 40")] == [3.0, 40.0]
    assert [a.value for a in tp.find_amounts("15ευρώ σουβλάκια")] == [15.0]
    assert [a.value for a in tp.find_amounts("τυρί 400g 5,60")] == [5.6]
    assert [a.value for a in tp.find_amounts("10/11 18:42 ΦΠΑ 13% total 19.01")] == [19.01]
    assert tp.find_amounts("τι χρωστάω") == []


def test_dates_from_words_and_numbers():
    today = date(2026, 11, 10)
    assert tp.detect_date("χθες καφές 3", today) == date(2026, 11, 9)
    assert tp.detect_date("προχθές 5", today) == date(2026, 11, 8)
    assert tp.detect_date("yesterday coffee 3", today) == date(2026, 11, 9)
    assert tp.detect_date("5/11 ταξί 12", today) == date(2026, 11, 5)
    assert tp.detect_date("20/12 δώρο 30", today) == date(2025, 12, 20)  # no future dates
    assert tp.detect_date("σούπερ 19.01", today) is None  # an amount, not 19 January


ACCOUNTS = [{"id": i, "name": n} for i, n in enumerate(
    ["AlphaBank", "AlphaBank Bonus Visa", "Cash", "Piraeus", "Revolut", "Ticket Restaurant"]
)]


@pytest.mark.parametrize("text,expected", [
    ("καφές 3 alpha", "AlphaBank"),
    ("άλφα visa 20", "AlphaBank Bonus Visa"),
    ("μετρητά 5 περίπτερο", "Cash"),
    ("τικετ 12 σουβλάκια", "Ticket Restaurant"),
    ("ρεβολούτ 9", "Revolut"),
    ("πειραιώς 40", "Piraeus"),
    ("restaurant 30", None),
    ("bonus 500", None),
    ("καφές 3", None),
])
def test_account_detection_handles_greek_greeklish_and_generic_words(text, expected):
    found, _ = tp.detect_account(text, ACCOUNTS)
    assert (ACCOUNTS[found]["name"] if found is not None else None) == expected


def _ctx(**kw):
    defaults = dict(
        expense_categories=["Delivery", "Entertainment", "Other", "Super Market", "Gym"],
        income_categories=["Other", "Salary"],
        accounts=ACCOUNTS,
        history=tp.History(),
        ai_settings={"ai_enabled": False},
        today=date(2026, 11, 10),
    )
    defaults.update(kw)
    return tp.Context(**defaults)


def test_basic_parser_splits_several_transactions_and_finds_income():
    txns = tp.basic_parse("χθες καφές 3,50 και σούπερ μάρκετ 20 revolut", _ctx())
    assert [(t.amount, t.category, t.notes, t.date) for t in txns] == [
        (3.5, "Entertainment", "καφές", "2026-11-09"),
        (20.0, "Super Market", "σούπερ μάρκετ", "2026-11-09"),
    ]
    assert all(t.account_id == 4 and t.source == "basic" for t in txns)
    salary = tp.basic_parse("μισθός 1500", _ctx())[0]
    assert (salary.type, salary.category) == ("income", "Salary")


def test_history_beats_the_ai_and_keywords():
    hist = tp.History(lookup={("expense", tp.key("efood")): "Delivery", ("expense", tp.key("καφές")): "Gym"})
    ctx = _ctx(history=hist)
    assert tp.basic_parse("efood 18", ctx)[0].category == "Delivery"
    assert tp.basic_parse("καφές 3", ctx)[0].category == "Gym"


def _fake_ai(monkeypatch, replies):
    """replies: list of dicts returned in order; records prompts."""
    prompts = []

    def chat(base, model, prompt, **kw):
        prompts.append(prompt)
        return json.dumps(replies.pop(0)) if replies else None

    monkeypatch.setattr(integrations, "ollama_chat", chat)
    return prompts


AI_ON = {"ai_enabled": True, "ai_base_url": "http://ollama", "ai_model": "qwen3:8b", "ai_timeout": 30}


def test_ai_transactions_are_capped_to_the_amounts_written(monkeypatch):
    _fake_ai(monkeypatch, [{"transactions": [
        {"type": "expense", "amount": 3, "category": "Entertainment", "notes": "καφές", "date": "2026-11-10"},
        {"type": "expense", "amount": 99, "category": "Gym", "notes": "made up", "date": "2026-11-10"},
    ]}])
    txns = tp.parse_transactions("καφές 3", _ctx(ai_settings=AI_ON))
    assert [(t.amount, t.category, t.source) for t in txns] == [(3.0, "Entertainment", "ai")]


def test_unusable_ai_answer_falls_back_to_the_basic_parser(monkeypatch):
    _fake_ai(monkeypatch, [{"nonsense": True}])
    txns = tp.parse_transactions("γυμναστήριο 35", _ctx(ai_settings=AI_ON))
    assert [(t.amount, t.category, t.source) for t in txns] == [(35.0, "Gym", "basic")]


def test_receipt_total_and_date_come_from_the_text(monkeypatch):
    _fake_ai(monkeypatch, [{"merchant": "Σκλαβενίτης", "total": 1.89, "date": None, "category": "Super Market"}])
    ocr = "ΣΚΛΑBENITHΣ Α.E.\n09/11/2026 18:42\nΓΑΛΑ 1L 1.89\nΣΥNOΛO 19.01\nKAPTA 19.01\nΦΠA 13% 1.43"
    txn = tp.parse_receipt(ocr, "", _ctx(ai_settings=AI_ON))
    assert (txn.amount, txn.date, txn.category, txn.notes, txn.source) == (
        19.01, "2026-11-09", "Super Market", "Σκλαβενίτης", "receipt"
    )


def test_questions_are_told_apart_from_entries():
    assert tp.looks_like_question("πόσα ξόδεψα αυτόν τον μήνα;")
    assert tp.looks_like_question("υπόλοιπο revolut")
    assert tp.looks_like_question("τι χρωστάω")
    assert not tp.looks_like_question("καφές 3")
    assert not tp.looks_like_question("έχω 20 ευρώ βενζίνη")


# --- the bot end to end -------------------------------------------------------------


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100

    def __call__(self, method, payload, token):
        if method == "sendChatAction":
            return {"ok": True}, None
        self.calls.append((method, payload))
        self.next_id += 1
        return {"ok": True, "result": {"message_id": self.next_id}}, None

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]

    def last_text(self):
        return [p["text"] for m, p in self.calls if m in ("sendMessage", "editMessageText")][-1]

    def last_keyboard(self):
        for m, p in reversed(self.calls):
            if "reply_markup" in p:
                return p["reply_markup"]["inline_keyboard"]
        return None


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(telegram_bot, "_telegram_api", fake)
    return fake


def _linked_user(client, username, chat=None, ai=None):
    """A registered user linked to a fresh chat, which _send/_press then use."""
    chat = chat or next(_chat_ids)
    _current["chat"] = chat
    register_and_login(client, username)
    conn = vb_app.get_connection()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    conn.execute("INSERT INTO telegram_links (user_id, chat_id) VALUES (?, ?)", (uid, str(chat)))
    for name in ("Revolut", "Cash"):
        conn.execute("INSERT INTO accounts (user_id, name, opening_balance) VALUES (?, ?, 0)", (uid, name))
    for name in ("Delivery", "Entertainment", "Super Market"):
        conn.execute("INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)", (uid, name))
    integrations.save_user_integrations(conn, uid, {
        "ai_enabled": bool(ai), "ai_provider": "ollama", "ai_base_url": "http://ollama" if ai else "",
        "ai_model": "qwen3:8b" if ai else "", "ai_timeout": 30, "ai_ocr_model": "glm-ocr" if ai else "",
    })
    conn.commit()
    conn.close()
    return uid


def _send(text=None, chat=None, **message):
    chat = chat or _current["chat"]
    msg = {"chat": {"id": chat}, "from": {"username": "u"}, **message}
    if text is not None:
        msg["text"] = text
    conn = vb_app.get_connection()
    try:
        telegram_bot.handle_update({"update_id": 1, "message": msg}, conn, vb_app.fetch_account_balances_through,
                                   {"bot_token": "T"})
    finally:
        conn.close()


def _press(data, message_id=101, chat=None):
    chat = chat or _current["chat"]
    conn = vb_app.get_connection()
    try:
        telegram_bot.handle_update(
            {"update_id": 2, "callback_query": {"id": "cb", "data": data,
                                                "message": {"message_id": message_id, "chat": {"id": chat}}}},
            conn, vb_app.fetch_account_balances_through, {"bot_token": "T"},
        )
    finally:
        conn.close()


def _rows(sql, *params):
    conn = vb_app.get_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _button(keyboard, text):
    for row in keyboard:
        for b in row:
            if b["text"] == text:
                return b["callback_data"]
    raise AssertionError(f"no button {text!r} in {keyboard}")


def test_text_without_ai_records_with_the_named_account(client, tg):
    uid = _linked_user(client, "tg_basic")
    _send("χθες καφές 3,50 revolut")
    row = _rows("""SELECT e.amount, e.notes, date(e.spent_at) d, a.name acc, c.name cat FROM expenses e
                   JOIN accounts a ON a.id = e.account_id JOIN categories c ON c.id = e.category_id
                   WHERE e.user_id = ?""", uid)[0]
    assert (row["amount"], row["notes"], row["d"], row["acc"], row["cat"]) == (
        -3.5, "καφές", (TODAY - timedelta(days=1)).isoformat(), "Revolut", "Entertainment"
    )
    assert "basic parser" in tg.last_text() and "3.50€" in tg.last_text()
    assert _button(tg.last_keyboard(), "↩️ Undo").endswith(":u")


def test_buttons_change_category_account_and_date_then_undo(client, tg):
    uid = _linked_user(client, "tg_buttons")
    _send("σούπερ μάρκετ 20")
    first = tg.last_keyboard()
    _press(_button(first, "🏷 Category"))
    _press(_button(tg.last_keyboard(), "Delivery"))
    _press(_button(first, "🏦 Account"))
    _press(_button(tg.last_keyboard(), "Cash"))
    _press(_button(first, "📅 Date"))
    _press(_button(tg.last_keyboard(), "Yesterday"))
    row = _rows("""SELECT c.name cat, a.name acc, date(e.spent_at) d FROM expenses e
                   JOIN categories c ON c.id = e.category_id JOIN accounts a ON a.id = e.account_id
                   WHERE e.user_id = ?""", uid)[0]
    assert (row["cat"], row["acc"], row["d"]) == ("Delivery", "Cash", (TODAY - timedelta(days=1)).isoformat())
    assert "Delivery" in tg.last_text() and "Cash" in tg.last_text()

    _press(_button(first, "↩️ Undo"))
    assert _rows("SELECT * FROM expenses WHERE user_id = ?", uid) == []
    assert tg.last_text().startswith("↩️ Removed")


def test_undo_never_touches_entries_made_on_the_web(client, tg):
    uid = _linked_user(client, "tg_undo")
    conn = vb_app.get_connection()
    cat = conn.execute("SELECT id FROM categories WHERE user_id = ? LIMIT 1", (uid,)).fetchone()["id"]
    acc = conn.execute("SELECT id FROM accounts WHERE user_id = ? LIMIT 1", (uid,)).fetchone()["id"]
    conn.execute("INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at) VALUES (?, 'web', -9, ?, ?, ?)",
                 (uid, cat, acc, TODAY.isoformat()))
    conn.commit()
    conn.close()
    _send("/undo")
    assert tg.last_text() == "Nothing to undo."
    _send("καφές 3")
    _send("/undo")
    assert [r["notes"] for r in _rows("SELECT notes FROM expenses WHERE user_id = ?", uid)] == ["web"]


def test_several_transactions_in_one_message_with_ai(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_multi", ai=True)
    prompts = _fake_ai(monkeypatch, [{"transactions": [
        {"type": "expense", "amount": 3, "category": "Entertainment", "notes": "καφές", "date": TODAY.isoformat()},
        {"type": "expense", "amount": 12, "category": "Delivery", "notes": "σουβλάκια", "date": TODAY.isoformat()},
    ]}])
    _send("καφές 3, σουβλάκια 12")
    rows = _rows("SELECT amount, notes FROM expenses WHERE user_id = ? ORDER BY id", uid)
    assert [(r["amount"], r["notes"]) for r in rows] == [(-3.0, "καφές"), (-12.0, "σουβλάκια")]
    assert len(tg.sent()) == 2 and "Delivery" in prompts[0]


def test_past_choices_are_offered_to_the_ai_and_override_it(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_history", ai=True)
    _send("efood 10")  # basic categories: Delivery via keyword
    _press(_button(tg.last_keyboard(), "🏷 Category"))
    _press(_button(tg.last_keyboard(), "Entertainment"))  # the user files efood under Entertainment
    prompts = _fake_ai(monkeypatch, [{"transactions": [
        {"type": "expense", "amount": 15, "category": "Delivery", "notes": "efood", "date": TODAY.isoformat()}]}])
    _send("efood 15")
    assert '"efood" → Entertainment' in prompts[0]
    cats = _rows("SELECT c.name FROM expenses e JOIN categories c ON c.id = e.category_id WHERE e.user_id = ? ORDER BY e.id", uid)
    assert [r["name"] for r in cats] == ["Entertainment", "Entertainment"]


def test_shared_bill_from_telegram_splits_and_books_my_share(client, tg):
    uid = _linked_user(client, "tg_shared")
    client.post("/shared/groups/add", data={"name": "Σπίτι", "new_members": "Άννα, Νίκος",
                                            "_csrf_token": csrf_token(client)})
    _send("σούπερ μάρκετ 60 μοιρασμένο revolut")
    bill = _rows("SELECT id, amount_cents, description FROM shared_expenses WHERE user_id = ?", uid)[0]
    assert bill["amount_cents"] == 6000 and bill["description"] == "σούπερ μάρκετ"
    mine = _rows("""SELECT e.amount, a.name acc FROM expenses e JOIN accounts a ON a.id = e.account_id
                    WHERE e.user_id = ?""", uid)[0]
    assert (mine["amount"], mine["acc"]) == (-20.0, "Revolut")
    assert "split 3 ways" in tg.last_text() and "your share 20.00€" in tg.last_text()

    _send("η Άννα πλήρωσε πίτσα 30 μισά")
    pizza = _rows("SELECT id, paid_by FROM shared_expenses WHERE description = 'πίτσα'")[0]
    anna = _rows("SELECT id FROM shared_people WHERE user_id = ? AND name = 'Άννα'", uid)[0]["id"]
    assert pizza["paid_by"] == anna
    splits = _rows("SELECT share_cents FROM shared_expense_splits WHERE expense_id = ? AND share_cents > 0", pizza["id"])
    assert sorted(r["share_cents"] for r in splits) == [1500, 1500]

    _send("/undo")
    assert _rows("SELECT * FROM shared_expenses WHERE description = 'πίτσα'") == []


def test_questions_are_answered_from_the_database(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_ask", ai=True)
    _fake_ai(monkeypatch, [
        {"transactions": [{"type": "expense", "amount": 18, "category": "Delivery", "notes": "efood", "date": TODAY.isoformat()}]},
        {"transactions": [{"type": "expense", "amount": 4, "category": "Entertainment", "notes": "καφές", "date": TODAY.isoformat()}]},
        {"metric": "spent", "category": "Delivery", "account": None, "search": None,
         "from": TODAY.replace(day=1).isoformat(), "to": TODAY.isoformat()},
    ])
    _send("efood 18")
    _send("καφές 4")
    _send("πόσα έδωσα σε delivery αυτόν τον μήνα;")
    assert tg.last_text().startswith("Spent on Delivery this month: 18.00€ (1 entries)")
    _send("/month")
    assert "22.00€" in tg.last_text() and "Delivery: 18.00€" in tg.last_text()
    _send("υπόλοιπο")  # AI has no reply left → heuristic plan
    assert tg.last_text().startswith("Balances:")


def test_receipt_photo_is_read_and_recorded(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_receipt", ai=True)
    monkeypatch.setattr(telegram_bot, "download_file", lambda file_id, token: b"jpeg-bytes")
    monkeypatch.setattr(integrations, "ollama_ocr", lambda base, model, img, **kw: (
        f"ΣΚΛΑΒΕΝΙΤΗΣ\n{TODAY.strftime('%d/%m/%Y')}\nΣΥΝΟΛΟ 19,01"))
    _fake_ai(monkeypatch, [{"merchant": "Σκλαβενίτης", "total": 19.01, "category": "Super Market"}])
    _send(photo=[{"file_id": "small", "width": 90, "height": 90}, {"file_id": "big", "width": 1280, "height": 960}],
          caption="cash")
    row = _rows("""SELECT e.amount, e.notes, a.name acc, c.name cat FROM expenses e
                   JOIN accounts a ON a.id = e.account_id JOIN categories c ON c.id = e.category_id
                   WHERE e.user_id = ?""", uid)[0]
    assert (row["amount"], row["notes"], row["acc"], row["cat"]) == (-19.01, "Σκλαβενίτης", "Cash", "Super Market")
    assert tg.sent()[0]["text"].startswith("🧾 Reading")
    assert "from receipt" in tg.last_text()


def test_receipts_need_an_ocr_model(client, tg):
    _linked_user(client, "tg_no_ocr")
    _send(photo=[{"file_id": "x", "width": 10, "height": 10}])
    assert "Receipt photos are off" in tg.last_text()


def test_default_account_command(client, tg):
    uid = _linked_user(client, "tg_default")
    _send("/account")
    _press(_button(tg.last_keyboard(), "Cash"))
    _send("καφές 2")
    acc = _rows("SELECT a.name FROM expenses e JOIN accounts a ON a.id = e.account_id WHERE e.user_id = ?", uid)
    assert acc[0]["name"] == "Cash"


def test_other_chats_cannot_press_my_buttons(client, app, tg):
    owner_chat, intruder_chat = next(_chat_ids), next(_chat_ids)
    uid = _linked_user(client, "tg_owner", chat=owner_chat)
    _linked_user(app.test_client(), "tg_intruder", chat=intruder_chat)
    _send("καφές 3", chat=owner_chat)
    undo = _button(tg.last_keyboard(), "↩️ Undo")
    _press(undo, chat=intruder_chat)
    assert len(_rows("SELECT * FROM expenses WHERE user_id = ?", uid)) == 1


def test_edited_messages_are_ignored(client, tg):
    uid = _linked_user(client, "tg_edit")
    conn = vb_app.get_connection()
    telegram_bot.handle_update({"update_id": 3, "edited_message": {"chat": {"id": _current["chat"]}, "text": "καφές 3"}},
                               conn, vb_app.fetch_account_balances_through, {"bot_token": "T"})
    conn.close()
    assert _rows("SELECT * FROM expenses WHERE user_id = ?", uid) == []


def test_ocr_model_is_saved_from_settings(client):
    register_and_login(client, "tg_settings")
    client.post("/settings/integrations/save", data={
        "ai_enabled": "1", "ai_provider": "ollama", "ai_base_url": "http://ollama:11434",
        "ai_model": "qwen3:8b", "ai_timeout": "60", "ai_ocr_model": "glm-ocr",
        "_csrf_token": csrf_token(client),
    })
    conn = vb_app.get_connection()
    uid = conn.execute("SELECT id FROM users WHERE username = 'tg_settings'").fetchone()["id"]
    assert integrations.get_user_integrations(conn, uid)["ai_ocr_model"] == "glm-ocr"
    conn.close()
    assert 'name="ai_ocr_model"' in client.get("/?panel=settings&settings_section=integrations").get_data(as_text=True)


# --- any user's own categories, in any language ---------------------------------------


def _ctx_for(expense, income):
    return tp.Context(expense, income, [], tp.History(), {"ai_enabled": False}, date(2026, 11, 10))


@pytest.mark.parametrize("message,greek,english", [
    ("σούπερ μάρκετ 42", "Σούπερ μάρκετ", "Groceries"),
    ("καφές 3", "Καφέδες", "Coffee"),
    ("βενζίνη 50", "Βενζίνη", "Car"),
    ("ρεύμα 85", "Λογαριασμοί", "Bills"),
    ("σουβλάκια 12", "Φαγητό έξω", "Eating out"),
    ("μισθός 1500", "Μισθός", "Wages"),
    ("κάτι άσχετο 7", "Λοιπά", "Misc"),
])
def test_categories_match_whatever_the_user_named_them(message, greek, english):
    greek_ctx = _ctx_for(["Σούπερ μάρκετ", "Καφέδες", "Βενζίνη", "Λογαριασμοί", "Φαγητό έξω", "Λοιπά"], ["Μισθός", "Λοιπά"])
    english_ctx = _ctx_for(["Groceries", "Coffee", "Car", "Cards", "Bills", "Eating out", "Misc"], ["Wages", "Misc"])
    assert tp.basic_parse(message, greek_ctx)[0].category == greek
    assert tp.basic_parse(message, english_ctx)[0].category == english


def test_a_category_written_in_the_message_is_used():
    ctx = _ctx_for(["Βενζίνη", "Κήπος", "Other"], ["Other"])
    assert tp.basic_parse("κήπος λιπάσματα 18", ctx)[0].category == "Κήπος"
    assert tp.basic_parse("βενζίνης 40", ctx)[0].category == "Βενζίνη"


def test_catch_all_is_the_users_own():
    assert tp.find_fallback_category(["Food", "Λοιπά"]) == "Λοιπά"
    assert tp.find_fallback_category(["Food", "General", "Other"]) == "Other"
    assert tp.find_fallback_category(["Food"]) == "Other"
    assert tp.resolve_category("Other", ["Food", "General"]) == "General"


def test_bot_uses_renamed_categories_and_creates_none(client, tg):
    """A user who replaced the seeded English "Other"/"Salary" with Greek ones."""
    uid = _linked_user(client, "tg_greek_user")
    conn = vb_app.get_connection()
    conn.execute("DELETE FROM categories WHERE user_id = ? AND name IN ('Other', 'General')", (uid,))
    conn.execute("DELETE FROM income_categories WHERE user_id = ? AND name IN ('Other', 'General', 'Salary')", (uid,))
    conn.executemany("INSERT INTO categories (user_id, name) VALUES (?, ?)", [(uid, "Λοιπά"), (uid, "Βενζίνη")])
    conn.executemany("INSERT INTO income_categories (user_id, name) VALUES (?, ?)", [(uid, "Λοιπά"), (uid, "Μισθός")])
    conn.commit()
    conn.close()
    before = {r["name"] for r in _rows("SELECT name FROM categories WHERE user_id = ?", uid)}
    before_income = {r["name"] for r in _rows("SELECT name FROM income_categories WHERE user_id = ?", uid)}

    _send("κάτι άσχετο 7")
    _send("βενζίνη 50")
    _send("μισθός 1500")

    assert {r["name"] for r in _rows("SELECT name FROM categories WHERE user_id = ?", uid)} == before
    assert {r["name"] for r in _rows("SELECT name FROM income_categories WHERE user_id = ?", uid)} == before_income
    cats = _rows("SELECT c.name FROM expenses e JOIN categories c ON c.id = e.category_id WHERE e.user_id = ? ORDER BY e.id", uid)
    assert [r["name"] for r in cats] == ["Λοιπά", "Βενζίνη"]
    income = _rows("""SELECT c.name FROM income_entries i JOIN income_categories c ON c.id = i.category_id
                      WHERE i.user_id = ?""", uid)
    assert [r["name"] for r in income] == ["Μισθός"]


# --- recurring entries ---------------------------------------------------------------


def test_counts_of_months_or_times_are_not_amounts():
    assert [a.value for a in tp.find_amounts("recurring 21.66 for 3 months")] == [21.66]
    assert [a.value for a in tp.find_amounts("γυμναστήριο 30 για 6 μήνες")] == [30.0]
    assert [a.value for a in tp.find_amounts("netflix 12,99 κάθε 2 εβδομάδες 10 φορές")] == [12.99]
    assert [a.value for a in tp.find_amounts("νοίκι 500 μηνιαία")] == [500.0]
    assert [a.value for a in tp.find_amounts("rent 500 monthly")] == [500.0]


def test_the_ais_repeat_is_kept_only_when_it_makes_sense(monkeypatch):
    item = {"type": "expense", "amount": 21.66, "category": "Entertainment", "notes": "spotify", "date": "2026-11-10"}
    cases = [
        ({"every": 1, "unit": "month", "times": 3}, tp.Repeat("month", 1, 3)),
        ({"every": "2", "unit": "weeks", "times": None}, tp.Repeat("week", 2, None)),
        ({"unit": "yearly"}, tp.Repeat("year", 1, None)),
        ({"every": 1, "unit": "day", "times": 5}, None),  # no daily rules
        ({"every": 1, "unit": "month", "times": 1}, None),  # once is not a repeat
        ({"every": -1, "unit": "month"}, None),
        (None, None),
    ]
    for raw, expected in cases:
        _fake_ai(monkeypatch, [{"transactions": [{**item, "repeat": raw}]}])
        txns = tp.parse_transactions("spotify 21.66", _ctx(ai_settings=AI_ON))
        assert txns[0].repeat == expected, raw


def test_a_repeating_entry_may_start_later_but_a_one_off_may_not(monkeypatch):
    ctx = _ctx(ai_settings=AI_ON)
    later = "2026-12-01"
    item = {"type": "expense", "amount": 500, "category": "Other", "notes": "νοίκι", "date": later}
    _fake_ai(monkeypatch, [{"transactions": [{**item, "repeat": {"every": 1, "unit": "month", "times": None}}]}])
    assert tp.parse_transactions("νοίκι 500 κάθε 1η του μήνα", ctx)[0].date == later
    _fake_ai(monkeypatch, [{"transactions": [item]}])
    assert tp.parse_transactions("νοίκι 500", ctx)[0].date == "2026-11-10"


def _rule(uid):
    return _rows("SELECT * FROM recurring_entries WHERE user_id = ?", uid)


def test_recurring_message_creates_a_rule_and_its_first_entry(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_recurring", ai=True)
    prompts = _fake_ai(monkeypatch, [{"transactions": [{
        "type": "expense", "amount": 21.66, "category": "Entertainment", "notes": "spotify",
        "date": TODAY.isoformat(), "repeat": {"every": 1, "unit": "month", "times": 3},
    }]}])
    _send("spotify recurring 21.66 for 3 months revolut")
    assert '"repeat"' in prompts[0]

    rules = _rule(uid)
    assert len(rules) == 1
    r = rules[0]
    assert (r["entry_type"], r["amount"], r["interval_unit"], r["interval_count"], r["months_to_run"],
            r["start_date"], r["day_of_month"], r["notes"]) == (
        "expense", 21.66, "month", 1, 3, TODAY.isoformat(), TODAY.day, "spotify")
    posted = _rows("""SELECT e.amount, e.notes, date(e.spent_at) d, a.name acc FROM expenses e
                      JOIN accounts a ON a.id = e.account_id WHERE e.user_id = ?""", uid)
    assert [(p["amount"], p["notes"], p["d"], p["acc"]) for p in posted] == [
        (-21.66, "[Recurring] spotify", TODAY.isoformat(), "Revolut")]

    text = tg.last_text()
    assert text.startswith("✅ Recurring expense 21.66€ · Entertainment")
    assert "every month, 3 times" in text and "1 of 3 recorded" in text
    keyboard = tg.last_keyboard()
    assert "📅 Date" not in [b["text"] for row in keyboard for b in row]

    # Category and account buttons change the rule and what it already posted.
    _press(_button(keyboard, "🏷 Category"))
    _press(_button(tg.last_keyboard(), "Delivery"))
    _press(_button(keyboard, "🏦 Account"))
    _press(_button(tg.last_keyboard(), "Cash"))
    row = _rows("""SELECT c.name cat, a.name acc FROM recurring_entries r JOIN categories c ON c.id = r.category_id
                   JOIN accounts a ON a.id = r.account_id WHERE r.user_id = ?""", uid)[0]
    assert (row["cat"], row["acc"]) == ("Delivery", "Cash")
    row = _rows("""SELECT c.name cat, a.name acc FROM expenses e JOIN categories c ON c.id = e.category_id
                   JOIN accounts a ON a.id = e.account_id WHERE e.user_id = ?""", uid)[0]
    assert (row["cat"], row["acc"]) == ("Delivery", "Cash")

    _send("/undo")
    assert _rule(uid) == []
    assert _rows("SELECT * FROM expenses WHERE user_id = ?", uid) == []
    assert tg.last_text().startswith("↩️ Removed: Recurring expense 21.66€") and "1 recorded entry" in tg.last_text()


def test_recurring_income_without_end_starting_next_month(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_recurring_income", ai=True)
    first = (TODAY.replace(day=1) + timedelta(days=32)).replace(day=1)
    _fake_ai(monkeypatch, [{"transactions": [{
        "type": "income", "amount": 1500, "category": "Salary", "notes": "μισθός",
        "date": first.isoformat(), "repeat": {"every": 1, "unit": "month", "times": None},
    }]}])
    _send("μισθός 1500 κάθε 1η του μήνα")
    r = _rule(uid)[0]
    assert (r["entry_type"], r["months_to_run"], r["start_date"], r["day_of_month"]) == ("income", None, first.isoformat(), 1)
    assert _rows("SELECT * FROM income_entries WHERE user_id = ?", uid) == []
    assert "no end date" in tg.last_text() and "0 recorded so far" in tg.last_text()
    _press(_button(tg.last_keyboard(), "🏷 Category"))
    assert "Salary" in [b["text"] for row in tg.last_keyboard() for b in row]


def test_shared_bills_cannot_repeat(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_recurring_shared", ai=True)
    client.post("/shared/groups/add", data={"name": "Σπίτι", "new_members": "Νίκος",
                                            "_csrf_token": csrf_token(client)})
    _fake_ai(monkeypatch, [{"transactions": [{
        "type": "expense", "amount": 40, "category": "Other", "notes": "internet",
        "date": TODAY.isoformat(), "repeat": {"every": 1, "unit": "month", "times": None},
    }]}])
    _send("internet 40 μοιρασμένο κάθε μήνα")
    assert "can't repeat" in tg.last_text()
    assert _rule(uid) == [] and _rows("SELECT * FROM shared_expenses WHERE user_id = ?", uid) == []


def test_old_telegram_entries_table_gains_the_recurring_kind():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
    conn.execute("""CREATE TABLE telegram_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'shared')),
        row_id INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'ai',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT INTO users (id) VALUES (1)")
    conn.execute("INSERT INTO telegram_entries (id, user_id, kind, row_id) VALUES (7, 1, 'expense', 42)")
    telegram_bot.migrate_telegram(conn)
    telegram_bot.migrate_telegram(conn)  # idempotent
    conn.execute("INSERT INTO telegram_entries (user_id, kind, row_id) VALUES (1, 'recurring', 3)")
    rows = conn.execute("SELECT id, kind, row_id FROM telegram_entries ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(7, "expense", 42), (8, "recurring", 3)]


def test_the_amount_written_beats_the_ais_reading_of_it(monkeypatch):
    item = {"type": "expense", "category": "Entertainment", "notes": "netflix", "date": "2026-11-10"}
    _fake_ai(monkeypatch, [{"transactions": [{**item, "amount": 1299}]}])
    assert [t.amount for t in tp.parse_transactions("netflix 12,99", _ctx(ai_settings=AI_ON))] == [12.99]
    _fake_ai(monkeypatch, [{"transactions": [{**item, "amount": 3}, {**item, "amount": 77}]}])
    txns = tp.parse_transactions("καφές 3, σουβλάκια 12", _ctx(ai_settings=AI_ON))
    assert [(t.amount, t.source) for t in txns] == [(3.0, "ai")]


# --- voice messages ---------------------------------------------------------------


def _fake_whisper(monkeypatch, heard):
    seen = {}

    def transcribe(audio, filename, **kw):
        seen.update(audio=audio, filename=filename, **kw)
        return heard

    monkeypatch.setenv("VB_WHISPER_URL", "http://whisper:8000/")
    monkeypatch.setattr(telegram_bot, "download_file", lambda file_id, token: b"ogg-bytes")
    monkeypatch.setattr(integrations, "transcribe_audio", transcribe)
    return seen


def test_voice_message_is_transcribed_and_recorded(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_voice")
    seen = _fake_whisper(monkeypatch, "καφές 3,50 μετρητά")
    _send(voice={"file_id": "v1", "duration": 3, "mime_type": "audio/ogg"})
    row = _rows("""SELECT e.amount, a.name acc FROM expenses e JOIN accounts a ON a.id = e.account_id
                   WHERE e.user_id = ?""", uid)[0]
    assert (row["amount"], row["acc"]) == (-3.5, "Cash")
    assert seen["audio"] == b"ogg-bytes" and seen["filename"] == "voice.ogg"
    assert "Cash" in seen["prompt"]  # account and category names help Whisper spell them
    assert seen["settings"]["url"] == "http://whisper:8000"
    texts = [p["text"] for m, p in tg.calls if m in ("sendMessage", "editMessageText")]
    assert texts[0] == "🎙 Listening…"
    assert "🎙 Heard: «καφές 3,50 μετρητά»" in texts


def test_voice_questions_are_answered(client, tg, monkeypatch):
    _linked_user(client, "tg_voice_ask")
    _fake_whisper(monkeypatch, "υπόλοιπο")
    _send(voice={"file_id": "v2", "duration": 1})
    assert tg.last_text().startswith("Balances:")


def test_voice_message_that_is_not_understood(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_voice_none")
    _fake_whisper(monkeypatch, None)
    _send(voice={"file_id": "v3", "duration": 1})
    assert "Couldn't make out" in tg.last_text()
    assert _rows("SELECT id FROM expenses WHERE user_id = ?", uid) == []


def test_voice_messages_need_a_whisper_server(client, tg, monkeypatch):
    _linked_user(client, "tg_voice_off")
    monkeypatch.delenv("VB_WHISPER_URL", raising=False)
    _send(voice={"file_id": "v4", "duration": 1})
    assert "Voice messages are off" in tg.last_text()


def test_voice_uses_the_users_own_whisper_settings(client, tg, monkeypatch):
    uid = _linked_user(client, "tg_voice_db")
    seen = _fake_whisper(monkeypatch, "καφές 2")  # env says http://whisper:8000
    conn = vb_app.get_connection()
    try:
        integrations.save_whisper_user_settings(conn, uid, integrations.parse_whisper_form(
            {"whisper_enabled": "1", "whisper_url": "http://mine:9000/", "whisper_model": "", "whisper_language": ""}))
    finally:
        conn.close()
    _send(voice={"file_id": "v5", "duration": 1})
    assert seen["settings"] == {"url": "http://mine:9000", "model": integrations.DEFAULT_WHISPER_MODEL, "language": ""}

    conn = vb_app.get_connection()
    try:  # switched off by the user: the environment no longer applies
        integrations.save_whisper_user_settings(conn, uid, integrations.parse_whisper_form(
            {"whisper_url": "http://mine:9000", "whisper_language": "el"}))
        # saving the Ollama settings leaves Whisper's alone
        integrations.save_user_integrations(conn, uid, integrations.default_integration_settings())
        assert integrations.get_whisper_user_settings(conn, uid)["whisper_url"] == "http://mine:9000"
    finally:
        conn.close()
    _send(voice={"file_id": "v6", "duration": 1})
    assert "Voice messages are off" in tg.last_text()


def test_whisper_form_validation():
    with pytest.raises(ValueError):
        integrations.parse_whisper_form({"whisper_enabled": "1", "whisper_url": ""})
    with pytest.raises(ValueError):
        integrations.parse_whisper_form({"whisper_url": "whisper:8000"})
    with pytest.raises(ValueError):
        integrations.parse_whisper_form({"whisper_language": "Greek"})
    assert integrations.parse_whisper_form({"whisper_language": " EL "})["whisper_language"] == "el"


def test_whisper_connection_test(monkeypatch):
    monkeypatch.setattr(integrations, "_fetch_json", lambda url, timeout: {"data": [{"id": "m1"}]})
    assert integrations.test_whisper_connection({"url": "http://w", "model": "m1"})[0] is True
    ok, msg = integrations.test_whisper_connection({"url": "http://w", "model": "m2"})
    assert not ok and "curl -X POST http://w/v1/models/m2" in msg
    assert integrations.test_whisper_connection({"url": "", "model": "m1"})[0] is False


def test_transcribe_audio_posts_an_openai_style_request(monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"text": " βενζίνη 50 \n"}).encode()

    def fake_urlopen(req, timeout):
        captured.update(url=req.full_url, body=req.data, ctype=req.headers["Content-type"])
        return Resp()

    monkeypatch.setattr(integrations, "urlopen", fake_urlopen)
    text = integrations.transcribe_audio(
        b"OGG", "voice.ogg", prompt="Revolut",
        settings={"url": "http://w:8000", "model": "m", "language": "el"},
    )
    assert text == "βενζίνη 50"
    assert captured["url"] == "http://w:8000/v1/audio/transcriptions"
    assert captured["ctype"].startswith("multipart/form-data; boundary=")
    body = captured["body"]
    for part in (b'name="model"\r\n\r\nm\r\n', b'name="language"\r\n\r\nel\r\n',
                 b'name="prompt"\r\n\r\nRevolut\r\n', b'filename="voice.ogg"', b"\r\n\r\nOGG\r\n"):
        assert part in body
    assert integrations.transcribe_audio(b"x", "a.ogg", settings={"url": "", "model": "m", "language": ""}) is None
