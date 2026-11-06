"""Shared expenses: split maths, debt simplification, and the link from a
shared bill into the user's own expenses, transfers and Shared balances."""

import pytest

import app as vb_app
import finance
import shared

from .conftest import csrf_token, register_and_login


# --- pure functions -------------------------------------------------------------


def test_allocate_adds_up_and_spreads_leftover_cents():
    assert shared.allocate(1000, [1, 1, 1]) == [334, 333, 333]
    assert shared.allocate(1, [1, 1, 1]) == [1, 0, 0]
    assert shared.allocate(10000, [2, 1, 1]) == [5000, 2500, 2500]
    assert sum(shared.allocate(9999, [3, 7, 11])) == 9999


def test_parse_cents_accepts_comma_and_rejects_garbage():
    assert shared.parse_cents("12,5") == 1250
    assert shared.parse_cents("0.005") == 1
    for bad in ("", "abc", "-3", "nan"):
        with pytest.raises(ValueError):
            shared.parse_cents(bad)


def test_compute_splits_every_method():
    entries = [(1, True, ""), (2, True, ""), (3, False, "")]
    assert shared.compute_splits("equal", 1000, entries) == [(1, 500, ""), (2, 500, "")]

    exact = [(1, True, "6"), (2, True, "4"), (3, True, "")]
    assert [(p, c) for p, c, _ in shared.compute_splits("exact", 1000, exact)] == [(1, 600), (2, 400)]
    with pytest.raises(ValueError, match="add up"):
        shared.compute_splits("exact", 1000, [(1, True, "6"), (2, True, "3")])

    pct = [(1, True, "50"), (2, True, "25"), (3, True, "25")]
    assert [c for _, c, _ in shared.compute_splits("percent", 1001, pct)] == [501, 250, 250]
    with pytest.raises(ValueError, match="100%"):
        shared.compute_splits("percent", 1000, [(1, True, "50"), (2, True, "40")])

    parts = shared.compute_splits("shares", 900, [(1, True, "2"), (2, True, "1"), (3, True, "0")])
    assert [(p, c) for p, c, _ in parts] == [(1, 600), (2, 300)]

    with pytest.raises(ValueError):
        shared.compute_splits("equal", 1000, [(1, False, "")])


def test_simplify_debts_needs_at_most_n_minus_one_payments():
    net = {1: 3000, 2: -1000, 3: -1000, 4: -1000}
    payments = shared.simplify_debts(net)
    assert len(payments) == 3
    assert all(to == 1 for _, to, _ in payments)
    # A owes B, B owes C collapses into A paying C directly.
    assert shared.simplify_debts({1: -500, 2: 0, 3: 500}) == [(1, 3, 500)]
    assert shared.simplify_debts({1: 0, 2: 0}) == []


# --- routes ----------------------------------------------------------------------


def _uid(username):
    conn = vb_app.get_connection()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    conn.close()
    return uid


def _one(sql, *params):
    conn = vb_app.get_connection()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def _setup_group(client, username):
    register_and_login(client, username)
    uid = _uid(username)
    resp = client.post(
        "/shared/groups/add",
        data={"name": "Trip", "new_members": "Anna, Nikos", "_csrf_token": csrf_token(client)},
    )
    assert resp.status_code == 302
    gid = _one("SELECT id FROM shared_groups WHERE user_id = ?", uid)["id"]
    people = {
        r["name"]: r["id"]
        for r in vb_app.get_connection().execute(
            "SELECT id, name FROM shared_people WHERE user_id = ?", (uid,)
        )
    }
    main = _one("SELECT id FROM accounts WHERE user_id = ? AND name = 'Main'", uid)["id"]
    cat = _one("SELECT id FROM categories WHERE user_id = ? AND name = 'General'", uid)["id"]
    return uid, gid, people, main, cat


def _balance(uid, account_id):
    for row in finance.fetch_account_balances_through(vb_app.get_connection(), "2100-01-01", uid):
        if int(row["id"]) == int(account_id):
            return round(float(row["current_balance"]), 2)
    raise AssertionError("account not found")


def _add_expense(client, gid, people, **fields):
    data = {
        "description": "Dinner",
        "amount": "90",
        "spent_on": "2026-11-01",
        "paid_by": people["You"],
        "split_method": "equal",
        "_csrf_token": csrf_token(client),
    }
    for pid in people.values():
        data[f"include_{pid}"] = "1"
    data.update(fields)
    return client.post(f"/shared/groups/{gid}/expenses/add", data=data, follow_redirects=True)


def test_paying_a_shared_bill_books_only_my_share_as_spending(client):
    uid, gid, people, main, cat = _setup_group(client, "share_payer")
    resp = _add_expense(client, gid, people, record_personal="1", category_id=cat, account_id=main)
    assert resp.status_code == 200

    exp = _one("SELECT amount, account_id, notes FROM expenses WHERE user_id = ?", uid)
    assert exp["amount"] == -30.0 and exp["account_id"] == main
    assert exp["notes"].startswith("[Shared] Dinner")
    ledger = shared.ledger_account_id(vb_app.get_connection(), uid)
    # 90 left Main: 30 spent, 60 lent and now owed back via Shared balances.
    assert _balance(uid, main) == -90.0
    assert _balance(uid, ledger) == 60.0

    net = shared.group_net_balances(vb_app.get_connection(), gid)
    assert net[people["You"]] == 6000
    assert net[people["Anna"]] == -3000

    page = client.get(f"/?panel=shared&group={gid}").get_data(as_text=True)
    assert "Suggested payments" in page and "Dinner" in page


def test_someone_else_paying_books_my_share_against_shared_balances(client):
    uid, gid, people, main, cat = _setup_group(client, "share_borrower")
    _add_expense(client, gid, people, paid_by=people["Anna"], record_personal="1", category_id=cat)
    ledger = shared.ledger_account_id(vb_app.get_connection(), uid)
    assert _balance(uid, ledger) == -30.0
    assert _balance(uid, main) == 0.0

    # Settling up moves real money to clear what Shared balances says I owe.
    client.post(
        f"/shared/groups/{gid}/settlements/add",
        data={
            "from_person_id": people["You"],
            "to_person_id": people["Anna"],
            "amount": "30",
            "paid_on": "2026-11-02",
            "record_personal": "1",
            "account_id": main,
            "_csrf_token": csrf_token(client),
        },
    )
    assert _balance(uid, ledger) == 0.0
    assert _balance(uid, main) == -30.0
    net = shared.group_net_balances(vb_app.get_connection(), gid)
    assert net[people["You"]] == 0
    assert net[people["Anna"]] == 3000

    sid = _one("SELECT id FROM shared_settlements WHERE user_id = ?", uid)["id"]
    client.post(f"/shared/settlements/{sid}/delete", data={"_csrf_token": csrf_token(client)})
    assert _balance(uid, ledger) == -30.0
    assert _one("SELECT COUNT(*) AS n FROM account_transfers WHERE user_id = ?", uid)["n"] == 0


def test_editing_and_deleting_rebuild_the_linked_rows(client):
    uid, gid, people, main, cat = _setup_group(client, "share_editor")
    _add_expense(client, gid, people, record_personal="1", category_id=cat, account_id=main)
    eid = _one("SELECT id FROM shared_expenses WHERE user_id = ?", uid)["id"]

    data = {
        "description": "Dinner",
        "amount": "100",
        "spent_on": "2026-11-01",
        "paid_by": people["You"],
        "split_method": "exact",
        f"value_{people['You']}": "50",
        f"value_{people['Anna']}": "50",
        "record_personal": "1",
        "category_id": cat,
        "account_id": main,
        "_csrf_token": csrf_token(client),
    }
    client.post(f"/shared/expenses/{eid}/edit", data=data)
    assert _one("SELECT COUNT(*) AS n FROM expenses WHERE user_id = ?", uid)["n"] == 1
    assert _one("SELECT amount FROM expenses WHERE user_id = ?", uid)["amount"] == -50.0
    assert _balance(uid, main) == -100.0
    net = shared.group_net_balances(vb_app.get_connection(), gid)
    assert net[people["Nikos"]] == 0

    client.post(f"/shared/expenses/{eid}/delete", data={"_csrf_token": csrf_token(client)})
    assert _one("SELECT COUNT(*) AS n FROM expenses WHERE user_id = ?", uid)["n"] == 0
    assert _one("SELECT COUNT(*) AS n FROM account_transfers WHERE user_id = ?", uid)["n"] == 0
    assert _balance(uid, main) == 0.0


def test_unrecorded_bills_leave_my_accounts_alone(client):
    uid, gid, people, main, cat = _setup_group(client, "share_unrecorded")
    _add_expense(client, gid, people)
    assert _one("SELECT COUNT(*) AS n FROM expenses WHERE user_id = ?", uid)["n"] == 0
    assert shared.ledger_account_id(vb_app.get_connection(), uid) is None


def test_bad_split_is_rejected_without_saving(client):
    uid, gid, people, main, cat = _setup_group(client, "share_badsplit")
    resp = _add_expense(
        client, gid, people, split_method="percent",
        **{f"value_{people['You']}": "50", f"value_{people['Anna']}": "20"},
    )
    assert "not 100%" in resp.get_data(as_text=True)
    assert _one("SELECT COUNT(*) AS n FROM shared_expenses WHERE user_id = ?", uid)["n"] == 0


def test_member_with_history_cannot_be_removed(client):
    uid, gid, people, main, cat = _setup_group(client, "share_members")
    _add_expense(client, gid, people)
    client.post(
        f"/shared/groups/{gid}/members/{people['Anna']}/remove",
        data={"_csrf_token": csrf_token(client)},
    )
    assert _one(
        "SELECT 1 AS x FROM shared_group_members WHERE group_id = ? AND person_id = ?", gid, people["Anna"]
    )


def test_deleting_a_group_removes_its_personal_rows(client):
    uid, gid, people, main, cat = _setup_group(client, "share_groupdel")
    _add_expense(client, gid, people, record_personal="1", category_id=cat, account_id=main)
    client.post(f"/shared/groups/{gid}/delete", data={"_csrf_token": csrf_token(client)})
    assert _one("SELECT COUNT(*) AS n FROM shared_groups WHERE user_id = ?", uid)["n"] == 0
    assert _one("SELECT COUNT(*) AS n FROM expenses WHERE user_id = ?", uid)["n"] == 0
    assert _balance(uid, main) == 0.0


def test_groups_are_private_to_their_owner(app):
    owner = app.test_client()
    _uid_owner, gid, people, main, cat = _setup_group(owner, "share_owner")
    _add_expense(owner, gid, people)
    eid = _one("SELECT id FROM shared_expenses WHERE group_id = ?", gid)["id"]

    intruder = app.test_client()
    register_and_login(intruder, "share_intruder")
    intruder.post(f"/shared/groups/{gid}/delete", data={"_csrf_token": csrf_token(intruder)})
    intruder.post(f"/shared/expenses/{eid}/delete", data={"_csrf_token": csrf_token(intruder)})
    assert _one("SELECT 1 AS x FROM shared_expenses WHERE id = ?", eid)
    page = intruder.get(f"/?panel=shared&group={gid}").get_data(as_text=True)
    assert "Trip" not in page


# --- several users sharing one group --------------------------------------------


def _ids(uid):
    main = _one("SELECT id FROM accounts WHERE user_id = ? AND name = 'Main'", uid)["id"]
    cat = _one("SELECT id FROM categories WHERE user_id = ? AND name = 'General'", uid)["id"]
    return main, cat


def _two_users(app, owner_name, friend_name, accept=True):
    owner, friend = app.test_client(), app.test_client()
    register_and_login(friend, friend_name)
    register_and_login(owner, owner_name)
    owner.post(
        "/shared/groups/add",
        data={"name": "Flat", "usernames": friend_name, "_csrf_token": csrf_token(owner)},
    )
    o_uid, f_uid = _uid(owner_name), _uid(friend_name)
    gid = _one("SELECT id FROM shared_groups WHERE user_id = ?", o_uid)["id"]
    if accept:
        friend.post(f"/shared/groups/{gid}/accept", data={"_csrf_token": csrf_token(friend)})
    people = {
        "owner": shared.member_person(vb_app.get_connection(), gid, o_uid),
        "friend": shared.member_person(
            vb_app.get_connection(), gid, f_uid, statuses=("active", "invited", "declined")
        ),
    }
    return owner, friend, o_uid, f_uid, gid, people


def test_invited_user_sees_group_only_after_joining(app):
    owner, friend, o_uid, f_uid, gid, people = _two_users(app, "mu_owner1", "mu_friend1", accept=False)
    page = friend.get("/?panel=shared").get_data(as_text=True)
    assert "invited you to" in page and "Flat" in page
    resp = friend.post(
        f"/shared/groups/{gid}/settlements/add",
        data={"from_person_id": people["friend"], "to_person_id": people["owner"], "amount": "5",
              "_csrf_token": csrf_token(friend)},
        follow_redirects=True,
    )
    assert "Group not found" in resp.get_data(as_text=True)
    assert "All groups" not in friend.get(f"/?panel=shared&group={gid}").get_data(as_text=True)

    friend.post(f"/shared/groups/{gid}/accept", data={"_csrf_token": csrf_token(friend)})
    page = friend.get(f"/?panel=shared&group={gid}").get_data(as_text=True)
    assert "All groups" in page and "created by mu_owner1" in page


def test_each_member_records_their_own_side(app):
    owner, friend, o_uid, f_uid, gid, people = _two_users(app, "mu_owner2", "mu_friend2")
    o_main, o_cat = _ids(o_uid)
    f_main, f_cat = _ids(f_uid)
    owner.post(
        f"/shared/groups/{gid}/expenses/add",
        data={
            "description": "Rent", "amount": "800", "spent_on": "2026-11-01",
            "paid_by": people["owner"], "split_method": "equal",
            f"include_{people['owner']}": "1", f"include_{people['friend']}": "1",
            "record_personal": "1", "category_id": o_cat, "account_id": o_main,
            "_csrf_token": csrf_token(owner),
        },
    )
    eid = _one("SELECT id FROM shared_expenses WHERE group_id = ?", gid)["id"]
    assert _balance(o_uid, o_main) == -800.0
    page = friend.get(f"/?panel=shared&group={gid}").get_data(as_text=True)
    assert "Not in your budget yet" in page
    assert _one("SELECT COUNT(*) AS n FROM expenses WHERE user_id = ?", f_uid)["n"] == 0

    friend.post(f"/shared/expenses/{eid}/record", data={"category_id": f_cat, "_csrf_token": csrf_token(friend)})
    f_ledger = shared.ledger_account_id(vb_app.get_connection(), f_uid)
    assert _balance(f_uid, f_ledger) == -400.0
    assert _one("SELECT amount FROM expenses WHERE user_id = ?", f_uid)["amount"] == -400.0

    # The friend edits the bill; the owner's own rows follow.
    friend.post(
        f"/shared/expenses/{eid}/edit",
        data={
            "description": "Rent", "amount": "900", "spent_on": "2026-11-01",
            "paid_by": people["owner"], "split_method": "equal",
            f"include_{people['owner']}": "1", f"include_{people['friend']}": "1",
            "record_personal": "1", "category_id": f_cat,
            "_csrf_token": csrf_token(friend),
        },
    )
    o_ledger = shared.ledger_account_id(vb_app.get_connection(), o_uid)
    assert _one("SELECT amount FROM expenses WHERE user_id = ?", o_uid)["amount"] == -450.0
    assert _balance(o_uid, o_main) == -900.0
    assert _balance(o_uid, o_ledger) == 450.0
    assert _balance(f_uid, f_ledger) == -450.0

    # Friend pays back; both sides can put the payment in their accounts.
    friend.post(
        f"/shared/groups/{gid}/settlements/add",
        data={"from_person_id": people["friend"], "to_person_id": people["owner"], "amount": "450",
              "record_personal": "1", "account_id": f_main, "_csrf_token": csrf_token(friend)},
    )
    sid = _one("SELECT id FROM shared_settlements WHERE group_id = ?", gid)["id"]
    owner.post(f"/shared/settlements/{sid}/record", data={"account_id": o_main, "_csrf_token": csrf_token(owner)})
    assert _balance(f_uid, f_ledger) == 0.0 and _balance(f_uid, f_main) == -450.0
    assert _balance(o_uid, o_ledger) == 0.0
    assert _balance(o_uid, o_main) == -450.0

    # Deleting the bill (by either member) removes both users' rows.
    friend.post(f"/shared/expenses/{eid}/delete", data={"_csrf_token": csrf_token(friend)})
    assert _one("SELECT COUNT(*) AS n FROM expenses WHERE user_id IN (?, ?)", o_uid, f_uid)["n"] == 0


def test_only_the_creator_can_delete_and_members_can_leave(app):
    owner, friend, o_uid, f_uid, gid, people = _two_users(app, "mu_owner3", "mu_friend3")
    friend.post(f"/shared/groups/{gid}/delete", data={"_csrf_token": csrf_token(friend)})
    assert _one("SELECT 1 AS x FROM shared_groups WHERE id = ?", gid)
    friend.post(
        f"/shared/groups/{gid}/members/{people['friend']}/remove", data={"_csrf_token": csrf_token(friend)}
    )
    assert shared.member_person(vb_app.get_connection(), gid, f_uid, statuses=("active", "invited")) is None
    owner.post(f"/shared/groups/{gid}/delete", data={"_csrf_token": csrf_token(owner)})
    assert not _one("SELECT 1 AS x FROM shared_groups WHERE id = ?", gid)


def test_declined_member_stays_a_name_without_access(app):
    owner, friend, o_uid, f_uid, gid, people = _two_users(app, "mu_owner4", "mu_friend4", accept=False)
    friend.post(f"/shared/groups/{gid}/decline", data={"_csrf_token": csrf_token(friend)})
    assert "invited you to" not in friend.get("/?panel=shared").get_data(as_text=True)
    assert shared.group_for_user(vb_app.get_connection(), f_uid, gid) is None
    page = owner.get(f"/?panel=shared&group={gid}").get_data(as_text=True)
    assert "declined" in page


def test_friend_balance_adds_up_across_groups_by_login(app):
    owner, friend, o_uid, f_uid, gid, people = _two_users(app, "mu_owner5", "mu_friend5")
    owner.post(
        f"/shared/groups/{gid}/expenses/add",
        data={"description": "A", "amount": "20", "paid_by": people["owner"], "split_method": "equal",
              f"include_{people['owner']}": "1", f"include_{people['friend']}": "1",
              "_csrf_token": csrf_token(owner)},
    )
    # A second group, created by the friend, with the owner invited back.
    friend.post("/shared/groups/add", data={"name": "Gym", "usernames": "mu_owner5", "_csrf_token": csrf_token(friend)})
    gid2 = _one("SELECT id FROM shared_groups WHERE user_id = ?", f_uid)["id"]
    owner.post(f"/shared/groups/{gid2}/accept", data={"_csrf_token": csrf_token(owner)})
    conn = vb_app.get_connection()
    me2 = shared.member_person(conn, gid2, o_uid)
    fr2 = shared.member_person(conn, gid2, f_uid)
    owner.post(
        f"/shared/groups/{gid2}/expenses/add",
        data={"description": "B", "amount": "30", "paid_by": me2, "split_method": "equal",
              f"include_{me2}": "1", f"include_{fr2}": "1", "_csrf_token": csrf_token(owner)},
    )
    ctx = shared.panel_context(vb_app.get_connection(), o_uid)
    assert ctx["friend_balances"] == [{"name": "mu_friend5", "amount": 25.0}]
    ctx_friend = shared.panel_context(vb_app.get_connection(), f_uid)
    assert ctx_friend["friend_balances"] == [{"name": "mu_owner5", "amount": -25.0}]


def test_linking_a_contact_invites_them_to_existing_groups(app):
    owner, friend = app.test_client(), app.test_client()
    register_and_login(friend, "mu_friend6")
    register_and_login(owner, "mu_owner6")
    o_uid = _uid("mu_owner6")
    owner.post("/shared/groups/add", data={"name": "Old", "new_members": "Zoe", "_csrf_token": csrf_token(owner)})
    zoe = _one("SELECT id FROM shared_people WHERE user_id = ? AND name = 'Zoe'", o_uid)["id"]
    resp = owner.post(
        f"/shared/people/{zoe}/link", data={"username": "nobody_here", "_csrf_token": csrf_token(owner)},
        follow_redirects=True,
    )
    assert "no user called" in resp.get_data(as_text=True)
    owner.post(f"/shared/people/{zoe}/link", data={"username": "mu_friend6", "_csrf_token": csrf_token(owner)})
    assert "invited you to" in friend.get("/?panel=shared").get_data(as_text=True)
