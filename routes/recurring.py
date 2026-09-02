from flask import Blueprint, flash, g, request

from db import get_connection
from helpers import _parse_category_choice, _parse_recurring_duration, _recurring_category_ok, normalize_day_of_month, redirect_home

bp = Blueprint("recurring", __name__)


@bp.route("/recurring/add", methods=["POST"])
def add_recurring():
    uid = g.user_id
    entry_type, category_id = _parse_category_choice(request.form.get("category_choice"))
    amount_raw = request.form.get("amount", "").strip()
    account_raw = request.form.get("account_id", "").strip()
    day_raw = request.form.get("day_of_month", "1").strip()
    notes = request.form.get("notes", "").strip()

    if not entry_type or category_id is None or not account_raw:
        flash("Choose type, category, and account.", "error")
        return redirect_home(panel="recurring")

    conn = get_connection()
    if not _recurring_category_ok(conn, entry_type, category_id, uid):
        conn.close()
        flash("Invalid category for that type.", "error")
        return redirect_home(panel="recurring")

    try:
        account_id = int(account_raw)
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
            (account_id, uid),
        ).fetchone():
            conn.close()
            flash("Invalid account.", "error")
            return redirect_home(panel="recurring")
        amt = abs(float(amount_raw))
        dom = normalize_day_of_month(day_raw)
    except (TypeError, ValueError):
        conn.close()
        flash("Invalid amount or account.", "error")
        return redirect_home(panel="recurring")
    try:
        months_to_run = _parse_recurring_duration(request.form)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect_home(panel="recurring")

    if amt <= 0:
        conn.close()
        flash("Amount must be positive.", "error")
        return redirect_home(panel="recurring")

    conn.execute(
        """
        INSERT INTO recurring_entries (
            user_id, entry_type, amount, category_id, account_id, day_of_month, months_to_run, notes, enabled
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (uid, entry_type, amt, category_id, account_id, dom, months_to_run, notes),
    )
    conn.commit()
    conn.close()
    flash("Recurring rule added.", "success")
    return redirect_home(panel="recurring")

@bp.route("/recurring/<int:recurring_id>/edit", methods=["POST"])
def edit_recurring(recurring_id):
    uid = g.user_id
    entry_type, category_id = _parse_category_choice(request.form.get("category_choice"))
    amount_raw = request.form.get("amount", "").strip()
    account_raw = request.form.get("account_id", "").strip()
    day_raw = request.form.get("day_of_month", "1").strip()
    notes = request.form.get("notes", "").strip()
    enabled = 1 if request.form.get("enabled") == "1" else 0

    if not entry_type or category_id is None or not account_raw:
        flash("Choose type, category, and account.", "error")
        return redirect_home(panel="recurring")

    conn = get_connection()
    if not _recurring_category_ok(conn, entry_type, category_id, uid):
        conn.close()
        flash("Invalid category for that type.", "error")
        return redirect_home(panel="recurring")

    try:
        account_id = int(account_raw)
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
            (account_id, uid),
        ).fetchone():
            conn.close()
            flash("Invalid account.", "error")
            return redirect_home(panel="recurring")
        amt = abs(float(amount_raw))
        dom = normalize_day_of_month(day_raw)
    except (TypeError, ValueError):
        conn.close()
        flash("Invalid amount or account.", "error")
        return redirect_home(panel="recurring")
    try:
        months_to_run = _parse_recurring_duration(request.form)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect_home(panel="recurring")

    if amt <= 0:
        conn.close()
        flash("Amount must be positive.", "error")
        return redirect_home(panel="recurring")

    cur = conn.execute(
        """
        UPDATE recurring_entries
        SET entry_type = ?, amount = ?, category_id = ?, account_id = ?, day_of_month = ?, months_to_run = ?, notes = ?, enabled = ?
        WHERE id = ? AND user_id = ?
        """,
        (entry_type, amt, category_id, account_id, dom, months_to_run, notes, enabled, recurring_id, uid),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        flash("Rule not found.", "error")
    else:
        flash("Recurring rule updated.", "success")
    return redirect_home(panel="recurring")

@bp.route("/recurring/<int:recurring_id>/delete", methods=["POST"])
def delete_recurring(recurring_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM recurring_entries WHERE id = ? AND user_id = ?",
        (recurring_id, g.user_id),
    )
    conn.commit()
    conn.close()
    flash("Recurring rule removed.", "success")
    return redirect_home(panel="recurring")
