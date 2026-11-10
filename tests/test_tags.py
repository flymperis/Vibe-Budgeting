"""Tags (#hashtags in notes): parsing, totals across categories, the web
report and the Telegram bot."""

import app as vb_app
import tags
import telegram_parse as tp

from .conftest import csrf_token, register_and_login
from .test_telegram_bot import _button, _linked_user, _rows, _send, tg  # noqa: F401  (tg is a fixture)


# --- parsing ------------------------------------------------------------------------


def test_tags_are_found_in_any_script_and_compared_without_case_or_accents():
    assert tags.find_tags("καφές #Διακοπές2026 #trip-ny #διακοπες2026") == ["Διακοπές2026", "trip-ny"]
    assert tags.tag_key("#Διακοπές2026") == tags.tag_key("διακοπες2026") == "διακοπες2026"
    assert tags.find_tags("table #3, mail a#b, url x.com/#top") == []  # no letters / not a word start
    assert tags.strip_tags("καφές #trip 3") == "καφές 3"
    assert tags.add_tags("καφές #trip", ["Trip", "party"]) == "καφές #trip #party"
    assert tags.clean_tag("#διακοπές 2026") == "διακοπές2026"
    assert tags.clean_tag("2026") is None


def test_linkify_escapes_html_and_links_tags():
    html = str(tags.linkify('<b>x</b> & #trip2026'))
    assert html.startswith("&lt;b&gt;x&lt;/b&gt; &amp; ")
    assert '<a class="tag-chip" href="/?panel=reports&amp;reports_section=tags&amp;tag=trip2026">#trip2026</a>' in html


def test_tag_numbers_are_not_amounts():
    assert [a.value for a in tp.find_amounts("#2026 καφές 3")] == [3.0]


# --- totals ---------------------------------------------------------------------------


def _uid(name):
    conn = vb_app.get_connection()
    try:
        return conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()["id"]
    finally:
        conn.close()


def _add(client, path, notes, amount, day):
    page = client.get("/").get_data(as_text=True)
    conn = vb_app.get_connection()
    uid = conn.execute("SELECT id FROM users ORDER BY id DESC LIMIT 1").fetchone()["id"]
    table = "categories" if "expenses" in path else "income_categories"
    cat = conn.execute(f"SELECT id FROM {table} WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)).fetchone()["id"]
    acc = conn.execute("SELECT id FROM accounts WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)).fetchone()["id"]
    conn.close()
    field = "spent_at" if "expenses" in path else "received_at"
    resp = client.post(path, data={"notes": notes, "amount": amount, "category_id": cat, "account_id": acc,
                                   field: day, "_csrf_token": csrf_token(client)}, follow_redirects=True)
    assert resp.status_code == 200 and page


def test_web_tag_report_adds_up_across_categories(client):
    register_and_login(client, "tag_web")
    uid = _uid("tag_web")
    conn = vb_app.get_connection()
    conn.execute("INSERT INTO categories (user_id, name) VALUES (?, 'Hotel')", (uid,))
    conn.commit()
    hotel = conn.execute("SELECT id FROM categories WHERE user_id = ? AND name = 'Hotel'", (uid,)).fetchone()["id"]
    conn.close()
    _add(client, "/expenses/add", "ferry #Trip2026", "-80", "2026-07-01")  # the form signs spending
    _add(client, "/expenses/add", "dinner #trip2026 #friends", "-45.50", "2026-07-03")
    _add(client, "/income/add", "refund #trip2026", "20", "2026-07-10")
    _add(client, "/expenses/add", "coffee", "-3", "2026-07-02")
    conn = vb_app.get_connection()
    conn.execute(
        "INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at) "
        "SELECT ?, 'hotel #TRIP2026', -300, ?, id, '2026-07-02' FROM accounts WHERE user_id = ? LIMIT 1",
        (uid, hotel, uid),
    )
    conn.commit()
    detail = tags.detail(conn, uid, "trip2026")
    summary = {t["key"]: t for t in tags.summary(conn, uid)}
    conn.close()

    assert (detail["spent"], detail["income"], detail["net"], detail["count"]) == (425.5, 20.0, -405.5, 4)
    assert (detail["first"], detail["last"]) == ("2026-07-01", "2026-07-10")
    assert [c["name"] for c in detail["categories"]] == ["Hotel", "General"]
    assert summary["trip2026"]["spent"] == 425.5 and summary["friends"]["count"] == 1

    listing = client.get("/?panel=expenses").get_data(as_text=True)
    assert 'class="tag-chip" href="/?panel=reports&amp;reports_section=tags&amp;tag=trip2026"' in listing
    assert 'data-tag-suggest' in listing and 'data-add-tag="' not in listing  # July is too long ago for chips
    assert '<script type="application/json" id="tag-names">' in listing and "trip2026" in listing.split('id="tag-names">')[1][:200]
    report = client.get("/?panel=reports&reports_section=tags").get_data(as_text=True)
    assert "#TRIP2026" in report or "#trip2026" in report
    one = client.get("/?panel=reports&reports_section=tags&tag=trip2026").get_data(as_text=True)
    section = one[one.find('id="reports-tags"'):][:6000]
    assert "425,50" in section and "-405,50" in section
    assert "Hotel" in section and "refund" in section


# --- Telegram ---------------------------------------------------------------------------


def test_telegram_keeps_tags_and_still_finds_the_category(client, tg):
    uid = _linked_user(client, "tag_bot")
    _send("χθες καφές 3 #Διακοπές2026")
    row = _rows("SELECT e.notes, c.name FROM expenses e JOIN categories c ON c.id = e.category_id WHERE e.user_id = ?", uid)[0]
    assert (row["notes"], row["name"]) == ("καφές #Διακοπές2026", "Entertainment")
    assert "#Διακοπές2026" in tg.last_text()


def test_tag_mode_tags_every_entry_until_turned_off(client, tg):
    uid = _linked_user(client, "tag_mode")
    _send("/tag Λονδίνο 2026")
    assert "#Λονδίνο2026" in tg.last_text()
    _send("καφές 3, σουβλάκια 12")
    _send("/tag off")
    assert tg.last_text().startswith("Stopped tagging #Λονδίνο2026. #Λονδίνο2026: 15.00€ spent · 2 entries")
    _send("καφές 2")
    notes = [r["notes"] for r in _rows("SELECT notes FROM expenses WHERE user_id = ? ORDER BY id", uid)]
    assert notes == ["καφές #Λονδίνο2026", "σουβλάκια #Λονδίνο2026", "καφές"]

    _send("/tag")  # menu offers recent tags as buttons
    _button(tg.last_keyboard(), "#Λονδίνο2026")


def test_questions_and_tags_command(client, tg):
    _linked_user(client, "tag_ask")
    _send("σούπερ μάρκετ 20 #πάρτι")
    _send("efood 15 #πάρτι")
    _send("πόσα κόστισε το #πάρτι;")
    assert tg.last_text().startswith("#πάρτι: 35.00€ spent · 2 entries")
    assert "• Super Market: 20.00€ (57%)" in tg.last_text()
    _send("/tags")
    assert "• #πάρτι: 35.00€ · 2 entries" in tg.last_text()
    _send("/tags άγνωστο")
    assert tg.last_text() == "Nothing is tagged #άγνωστο yet."


def test_shared_bill_keeps_its_tag_in_my_share(client, tg):
    uid = _linked_user(client, "tag_shared")
    client.post("/shared/groups/add", data={"name": "Φίλοι", "new_members": "Άννα", "_csrf_token": csrf_token(client)})
    _send("πίτσα 30 μοιρασμένο #πάρτι")
    bill = _rows("SELECT description FROM shared_expenses WHERE user_id = ?", uid)[0]
    assert bill["description"] == "πίτσα #πάρτι"
    conn = vb_app.get_connection()
    detail = tags.detail(conn, uid, "πάρτι")
    conn.close()
    assert detail["spent"] == 15.0  # only my share is my spending


def test_chips_offer_only_a_few_recently_used_tags():
    from datetime import date
    rows = [{"tag": f"t{i}", "last": f"2026-11-{10 - i:02d}"} for i in range(9)] + [{"tag": "old", "last": "2026-01-01"}]
    assert tags.chip_tags(rows, date(2026, 11, 10)) == ["t0", "t1", "t2", "t3", "t4", "t5"]
    assert tags.chip_tags([{"tag": "old", "last": "2026-08-01"}], date(2026, 11, 10)) == []
