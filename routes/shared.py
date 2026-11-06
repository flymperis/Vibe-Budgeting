from flask import Blueprint, flash, g, redirect, request, url_for

import shared
from db import get_connection
from helpers import _user_owns_account, _user_owns_category, normalize_month, normalize_txn_day_from_form

bp = Blueprint("shared", __name__)


def _back(group_id=None):
    query = {"panel": "shared", "month": normalize_month(request.form.get("month"))}
    if group_id:
        query["group"] = int(group_id)
    return redirect(url_for("dashboard.index", **query))


def _int_or_none(raw):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _new_names(raw):
    """'Anna, Nikos' -> ['Anna', 'Nikos'] (blank and duplicate names dropped)."""
    seen, names = set(), []
    for part in str(raw or "").replace("\n", ",").split(","):
        name = shared.clean_name(part)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return names


def _own_account(conn, uid, account_id):
    """A real account of this user (not their Shared balances), or None."""
    if not account_id or not _user_owns_account(conn, account_id, uid):
        return None
    if account_id == shared.ledger_account_id(conn, uid):
        return None
    return account_id


def _members_from_form(conn, uid, form):
    """Person ids picked or typed on a group/member form: contact checkboxes
    or a contact select, new plain names, and server usernames to invite."""
    ids = []
    for raw in form.getlist("member_ids") + [form.get("person_id")]:
        pid = _int_or_none(raw)
        if pid and shared.person_owned(conn, uid, pid) and pid not in ids:
            ids.append(pid)
    for name in _new_names(form.get("new_members")):
        ids.append(shared.find_or_create_person(conn, uid, name))
    for login in _new_names(form.get("usernames")):
        ids.append(shared.contact_for_username(conn, uid, login))
    return ids


# --- people ------------------------------------------------------------------


@bp.route("/shared/people/add", methods=["POST"])
def add_person():
    names = _new_names(request.form.get("name"))
    if not names:
        flash("Enter a name.", "error")
        return _back()
    conn = get_connection()
    try:
        for name in names:
            shared.find_or_create_person(conn, g.user_id, name)
        conn.commit()
    finally:
        conn.close()
    flash(f"Added {', '.join(names)}.", "success")
    return _back()


@bp.route("/shared/people/<int:person_id>/rename", methods=["POST"])
def rename_person(person_id):
    name = shared.clean_name(request.form.get("name"))
    if not name:
        flash("Enter a name.", "error")
        return _back()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT is_self FROM shared_people WHERE id = ? AND user_id = ?", (person_id, g.user_id)
        ).fetchone()
        if not row or row["is_self"]:
            flash("That person can't be renamed.", "error")
            return _back()
        if conn.execute(
            "SELECT 1 FROM shared_people WHERE user_id = ? AND name = ? AND id != ?",
            (g.user_id, name, person_id),
        ).fetchone():
            flash(f"There is already someone called {name}.", "error")
            return _back()
        conn.execute("UPDATE shared_people SET name = ? WHERE id = ?", (name, person_id))
        conn.commit()
    finally:
        conn.close()
    flash("Name saved.", "success")
    return _back()


@bp.route("/shared/people/<int:person_id>/link", methods=["POST"])
def link_person(person_id):
    conn = get_connection()
    try:
        try:
            shared.link_contact(conn, g.user_id, person_id, request.form.get("username"))
        except ValueError as exc:
            flash(str(exc), "error")
            return _back()
        conn.commit()
    finally:
        conn.close()
    flash("Linked. Groups they are in now show up as invitations for them.", "success")
    return _back()


@bp.route("/shared/people/<int:person_id>/unlink", methods=["POST"])
def unlink_person(person_id):
    conn = get_connection()
    try:
        try:
            shared.unlink_contact(conn, g.user_id, person_id)
        except ValueError as exc:
            flash(str(exc), "error")
            return _back()
        conn.commit()
    finally:
        conn.close()
    flash("Unlinked.", "success")
    return _back()


@bp.route("/shared/people/<int:person_id>/delete", methods=["POST"])
def delete_person(person_id):
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT is_self FROM shared_people WHERE id = ? AND user_id = ?", (person_id, g.user_id)
        ).fetchone()
        if not row or row["is_self"]:
            flash("That person can't be removed.", "error")
            return _back()
        if conn.execute(
            "SELECT 1 FROM shared_group_members WHERE person_id = ? LIMIT 1", (person_id,)
        ).fetchone():
            flash("Remove them from their groups first.", "error")
            return _back()
        conn.execute("DELETE FROM shared_people WHERE id = ?", (person_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Person removed.", "success")
    return _back()


# --- groups ------------------------------------------------------------------


@bp.route("/shared/groups/add", methods=["POST"])
def add_group():
    name = shared.clean_name(request.form.get("name"))
    if not name:
        flash("Give the group a name.", "error")
        return _back()
    conn = get_connection()
    try:
        uid = g.user_id
        try:
            people = _members_from_form(conn, uid, request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return _back()
        gid = shared.create_group(conn, uid, name)
        invited = sum(1 for pid in people if shared.add_member(conn, gid, pid) == "invited")
        conn.commit()
    finally:
        conn.close()
    msg = f"Group “{name}” created."
    if invited:
        msg += f" {invited} user{'s were' if invited != 1 else ' was'} invited."
    flash(msg, "success")
    return _back(gid)


@bp.route("/shared/groups/<int:group_id>/rename", methods=["POST"])
def rename_group(group_id):
    name = shared.clean_name(request.form.get("name"))
    conn = get_connection()
    try:
        if not shared.group_for_user(conn, g.user_id, group_id):
            flash("Group not found.", "error")
            return _back()
        if not name:
            flash("Give the group a name.", "error")
            return _back(group_id)
        conn.execute("UPDATE shared_groups SET name = ? WHERE id = ?", (name, group_id))
        conn.commit()
    finally:
        conn.close()
    flash("Group renamed.", "success")
    return _back(group_id)


@bp.route("/shared/groups/<int:group_id>/delete", methods=["POST"])
def delete_group(group_id):
    conn = get_connection()
    try:
        grp = shared.group_for_user(conn, g.user_id, group_id)
        if not grp:
            flash("Group not found.", "error")
            return _back()
        if int(grp["user_id"]) != g.user_id:
            flash("Only the person who created the group can delete it.", "error")
            return _back(group_id)
        shared.delete_group(conn, group_id)
        conn.commit()
    finally:
        conn.close()
    flash("Group deleted, along with the entries it had added to everyone's accounts.", "success")
    return _back()


@bp.route("/shared/groups/<int:group_id>/accept", methods=["POST"])
def accept_invite(group_id):
    conn = get_connection()
    try:
        ok = shared.respond_to_invite(conn, g.user_id, group_id, accept=True)
        conn.commit()
    finally:
        conn.close()
    if not ok:
        flash("Invitation not found.", "error")
        return _back()
    flash("You joined the group. Use “Add to my budget” on bills you want in your own accounts.", "success")
    return _back(group_id)


@bp.route("/shared/groups/<int:group_id>/decline", methods=["POST"])
def decline_invite(group_id):
    conn = get_connection()
    try:
        ok = shared.respond_to_invite(conn, g.user_id, group_id, accept=False)
        conn.commit()
    finally:
        conn.close()
    flash("Invitation declined." if ok else "Invitation not found.", "success" if ok else "error")
    return _back()


@bp.route("/shared/groups/<int:group_id>/members/add", methods=["POST"])
def add_member(group_id):
    conn = get_connection()
    try:
        uid = g.user_id
        if not shared.group_for_user(conn, uid, group_id):
            flash("Group not found.", "error")
            return _back()
        try:
            people = _members_from_form(conn, uid, request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return _back(group_id)
        if not people:
            flash("Choose someone to add.", "error")
            return _back(group_id)
        results = [shared.add_member(conn, group_id, pid) for pid in people]
        conn.commit()
    finally:
        conn.close()
    if all(r == "already" for r in results):
        flash("They are already in this group.", "error")
    elif "invited" in results:
        flash("Invitation sent. They'll see the group once they accept.", "success")
    else:
        flash("Member added.", "success")
    return _back(group_id)


@bp.route("/shared/groups/<int:group_id>/members/<int:person_id>/remove", methods=["POST"])
def remove_member(group_id, person_id):
    conn = get_connection()
    try:
        uid = g.user_id
        grp = shared.group_for_user(conn, uid, group_id)
        if not grp:
            flash("Group not found.", "error")
            return _back()
        person = conn.execute("SELECT linked_user_id FROM shared_people WHERE id = ?", (person_id,)).fetchone()
        if person and person["linked_user_id"] == grp["user_id"]:
            flash("The group's creator can't be removed.", "error")
            return _back(group_id)
        if shared.person_involved_in_group(conn, group_id, person_id):
            flash("They already appear in this group's expenses or payments, so they can't be removed.", "error")
            return _back(group_id)
        conn.execute(
            "DELETE FROM shared_group_members WHERE group_id = ? AND person_id = ?", (group_id, person_id)
        )
        conn.commit()
        leaving = person and person["linked_user_id"] == uid
    finally:
        conn.close()
    flash("You left the group." if leaving else "Member removed.", "success")
    return _back(None if leaving else group_id)


# --- expenses ----------------------------------------------------------------


def _expense_from_form(conn, uid, group_id, form):
    """Validated fields, splits and the submitting user's own recording
    choice for a shared bill; raises ValueError with a user-facing message."""
    member_ids = shared.group_member_ids(conn, group_id)
    description = shared.clean_name(form.get("description")) or "Expense"
    try:
        amount_cents = shared.parse_cents(form.get("amount"))
    except ValueError:
        raise ValueError("Enter the bill amount.") from None
    if amount_cents <= 0:
        raise ValueError("Enter an amount above zero.")
    paid_by = _int_or_none(form.get("paid_by"))
    if paid_by not in member_ids:
        raise ValueError("Choose who paid from the group's members.")
    method = (form.get("split_method") or "equal").strip()
    entries = [
        (pid, form.get(f"include_{pid}") == "1", form.get(f"value_{pid}", ""))
        for pid in member_ids
    ]
    splits = shared.compute_splits(method, amount_cents, entries)

    me = shared.member_person(conn, group_id, uid)
    my_share = sum(c for pid, c, _ in splits if pid == me)
    i_paid = paid_by == me
    record = form.get("record_personal") == "1" and (i_paid or my_share > 0)
    link = None
    if record:
        category_id = _int_or_none(form.get("category_id")) if my_share > 0 else None
        account_id = _int_or_none(form.get("account_id")) if i_paid else None
        if my_share > 0 and not (category_id and _user_owns_category(conn, category_id, uid, expense=True)):
            raise ValueError("Choose the category for your share.")
        if i_paid and not _own_account(conn, uid, account_id):
            raise ValueError("Choose the account the money actually left (not Shared balances).")
        link = (category_id, account_id)
    return {
        "description": description,
        "amount_cents": amount_cents,
        "paid_by": paid_by,
        "split_method": method,
        "spent_on": normalize_txn_day_from_form(form.get("spent_on", "")),
        "splits": splits,
        "link": link,
    }


def _save_splits(conn, expense_id, splits):
    conn.execute("DELETE FROM shared_expense_splits WHERE expense_id = ?", (expense_id,))
    conn.executemany(
        "INSERT INTO shared_expense_splits (expense_id, person_id, share_cents, input_value) VALUES (?, ?, ?, ?)",
        [(expense_id, pid, cents, value) for pid, cents, value in splits],
    )


def _apply_links(conn, uid, expense_id, link):
    """The submitter's own choice, then everyone else's rows follow the bill."""
    if link:
        shared.set_expense_link(conn, uid, expense_id, *link)
    else:
        shared.clear_expense_link(conn, uid, expense_id)
    shared.relink_expense(conn, expense_id)


def _expense_group(conn, uid, expense_id):
    """The bill's group id when this user is an active member there."""
    row = conn.execute("SELECT group_id FROM shared_expenses WHERE id = ?", (expense_id,)).fetchone()
    if not row or not shared.group_for_user(conn, uid, row["group_id"]):
        return None
    return int(row["group_id"])


@bp.route("/shared/groups/<int:group_id>/expenses/add", methods=["POST"])
def add_expense(group_id):
    conn = get_connection()
    try:
        uid = g.user_id
        if not shared.group_for_user(conn, uid, group_id):
            flash("Group not found.", "error")
            return _back()
        try:
            data = _expense_from_form(conn, uid, group_id, request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return _back(group_id)
        cur = conn.execute(
            """
            INSERT INTO shared_expenses (user_id, group_id, description, amount_cents, paid_by, split_method, spent_on)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, group_id, data["description"], data["amount_cents"], data["paid_by"],
             data["split_method"], data["spent_on"]),
        )
        expense_id = int(cur.lastrowid)
        _save_splits(conn, expense_id, data["splits"])
        _apply_links(conn, uid, expense_id, data["link"])
        conn.commit()
    finally:
        conn.close()
    flash(f"Added “{data['description']}”.", "success")
    return _back(group_id)


@bp.route("/shared/expenses/<int:expense_id>/edit", methods=["POST"])
def edit_expense(expense_id):
    conn = get_connection()
    try:
        uid = g.user_id
        group_id = _expense_group(conn, uid, expense_id)
        if group_id is None:
            flash("Expense not found.", "error")
            return _back()
        try:
            data = _expense_from_form(conn, uid, group_id, request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return _back(group_id)
        conn.execute(
            """
            UPDATE shared_expenses
            SET description = ?, amount_cents = ?, paid_by = ?, split_method = ?, spent_on = ?
            WHERE id = ?
            """,
            (data["description"], data["amount_cents"], data["paid_by"], data["split_method"],
             data["spent_on"], expense_id),
        )
        _save_splits(conn, expense_id, data["splits"])
        _apply_links(conn, uid, expense_id, data["link"])
        conn.commit()
    finally:
        conn.close()
    flash("Expense saved.", "success")
    return _back(group_id)


@bp.route("/shared/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id):
    conn = get_connection()
    try:
        group_id = _expense_group(conn, g.user_id, expense_id)
        if group_id is None:
            flash("Expense not found.", "error")
            return _back()
        shared.delete_expense(conn, expense_id)
        conn.commit()
    finally:
        conn.close()
    flash("Expense deleted.", "success")
    return _back(group_id)


@bp.route("/shared/expenses/<int:expense_id>/record", methods=["POST"])
def record_expense(expense_id):
    """Put one bill into the current user's own books (their category and,
    if they paid, the account it came from)."""
    conn = get_connection()
    try:
        uid = g.user_id
        group_id = _expense_group(conn, uid, expense_id)
        if group_id is None:
            flash("Expense not found.", "error")
            return _back()
        me = shared.member_person(conn, group_id, uid)
        my_share, i_paid = shared.expense_involvement(conn, expense_id, me)
        if not (my_share or i_paid):
            flash("You aren't part of this bill.", "error")
            return _back(group_id)
        category_id = _int_or_none(request.form.get("category_id")) if my_share else None
        account_id = _int_or_none(request.form.get("account_id")) if i_paid else None
        if my_share and not (category_id and _user_owns_category(conn, category_id, uid, expense=True)):
            flash("Choose the category for your share.", "error")
            return _back(group_id)
        if i_paid and not _own_account(conn, uid, account_id):
            flash("Choose the account you paid from.", "error")
            return _back(group_id)
        shared.set_expense_link(conn, uid, expense_id, category_id, account_id)
        conn.commit()
    finally:
        conn.close()
    flash("Added to your budget.", "success")
    return _back(group_id)


@bp.route("/shared/expenses/<int:expense_id>/unrecord", methods=["POST"])
def unrecord_expense(expense_id):
    conn = get_connection()
    try:
        group_id = _expense_group(conn, g.user_id, expense_id)
        if group_id is None:
            flash("Expense not found.", "error")
            return _back()
        shared.clear_expense_link(conn, g.user_id, expense_id)
        conn.commit()
    finally:
        conn.close()
    flash("Removed from your budget.", "success")
    return _back(group_id)


# --- settling up ---------------------------------------------------------------


def _settlement_group(conn, uid, settlement_id):
    row = conn.execute("SELECT group_id FROM shared_settlements WHERE id = ?", (settlement_id,)).fetchone()
    if not row or not shared.group_for_user(conn, uid, row["group_id"]):
        return None
    return int(row["group_id"])


@bp.route("/shared/groups/<int:group_id>/settlements/add", methods=["POST"])
def add_settlement(group_id):
    conn = get_connection()
    try:
        uid = g.user_id
        if not shared.group_for_user(conn, uid, group_id):
            flash("Group not found.", "error")
            return _back()
        member_ids = shared.group_member_ids(conn, group_id)
        from_id = _int_or_none(request.form.get("from_person_id"))
        to_id = _int_or_none(request.form.get("to_person_id"))
        if from_id not in member_ids or to_id not in member_ids:
            flash("Choose who paid and who received from the group's members.", "error")
            return _back(group_id)
        if from_id == to_id:
            flash("Someone can't pay themselves.", "error")
            return _back(group_id)
        try:
            cents = shared.parse_cents(request.form.get("amount"))
        except ValueError:
            cents = 0
        if cents <= 0:
            flash("Enter an amount above zero.", "error")
            return _back(group_id)
        me = shared.member_person(conn, group_id, uid)
        record = request.form.get("record_personal") == "1" and me in (from_id, to_id)
        account_id = _own_account(conn, uid, _int_or_none(request.form.get("account_id"))) if record else None
        if record and not account_id:
            flash("Choose the account the money went from or to (not Shared balances).", "error")
            return _back(group_id)
        cur = conn.execute(
            """
            INSERT INTO shared_settlements (user_id, group_id, from_person_id, to_person_id, amount_cents, paid_on, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, group_id, from_id, to_id, cents,
             normalize_txn_day_from_form(request.form.get("paid_on", "")),
             shared.clean_name(request.form.get("notes"))),
        )
        if record:
            shared.set_settlement_link(conn, uid, int(cur.lastrowid), account_id)
        conn.commit()
    finally:
        conn.close()
    flash("Payment recorded.", "success")
    return _back(group_id)


@bp.route("/shared/settlements/<int:settlement_id>/delete", methods=["POST"])
def delete_settlement(settlement_id):
    conn = get_connection()
    try:
        group_id = _settlement_group(conn, g.user_id, settlement_id)
        if group_id is None:
            flash("Payment not found.", "error")
            return _back()
        shared.delete_settlement(conn, settlement_id)
        conn.commit()
    finally:
        conn.close()
    flash("Payment deleted.", "success")
    return _back(group_id)


@bp.route("/shared/settlements/<int:settlement_id>/record", methods=["POST"])
def record_settlement(settlement_id):
    conn = get_connection()
    try:
        uid = g.user_id
        group_id = _settlement_group(conn, uid, settlement_id)
        if group_id is None:
            flash("Payment not found.", "error")
            return _back()
        account_id = _own_account(conn, uid, _int_or_none(request.form.get("account_id")))
        if not account_id:
            flash("Choose a real account (not Shared balances).", "error")
            return _back(group_id)
        shared.set_settlement_link(conn, uid, settlement_id, account_id)
        conn.commit()
    finally:
        conn.close()
    flash("Payment added to your accounts.", "success")
    return _back(group_id)


@bp.route("/shared/settlements/<int:settlement_id>/unrecord", methods=["POST"])
def unrecord_settlement(settlement_id):
    conn = get_connection()
    try:
        group_id = _settlement_group(conn, g.user_id, settlement_id)
        if group_id is None:
            flash("Payment not found.", "error")
            return _back()
        shared.clear_settlement_link(conn, g.user_id, settlement_id)
        conn.commit()
    finally:
        conn.close()
    flash("Payment removed from your accounts.", "success")
    return _back(group_id)
