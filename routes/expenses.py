from flask import Blueprint, flash, g, request
from datetime import datetime, timedelta, timezone

from db import get_connection
from helpers import _user_owns_account, _user_owns_category, normalize_expense_amount, normalize_txn_day_from_form, redirect_home

bp = Blueprint("expenses", __name__)


@bp.route("/expenses/add", methods=["POST"])
def add_expense():
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    category_raw = request.form.get("category_id", "").strip()
    account_raw = request.form.get("account_id", "").strip()

    if category_raw and account_raw:
        try:
            category_id = int(category_raw)
            account_id = int(account_raw)
        except (TypeError, ValueError):
            flash("Invalid category or account.", "error")
            return redirect_home()
        spent_at = datetime.now().date().isoformat()
        conn = get_connection()
        if not _user_owns_category(conn, category_id, g.user_id, expense=True) or not _user_owns_account(
            conn, account_id, g.user_id
        ):
            conn.close()
            flash("Invalid category or account.", "error")
            return redirect_home()
        conn.execute(
            """
            INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                g.user_id,
                notes,
                normalize_expense_amount(amount),
                category_id,
                account_id,
                spent_at,
            ),
        )
        conn.commit()
        conn.close()

    return redirect_home()

@bp.route("/expenses/<int:expense_id>/edit", methods=["POST"])
def edit_expense(expense_id):
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    category_raw = request.form.get("category_id", "").strip()
    account_raw = request.form.get("account_id", "").strip()
    spent_at_raw = request.form.get("spent_at", "").strip()

    if category_raw and account_raw:
        try:
            category_id = int(category_raw)
            account_id = int(account_raw)
        except (TypeError, ValueError):
            flash("Invalid category or account.", "error")
            return redirect_home()
        spent_at = normalize_txn_day_from_form(spent_at_raw)
        conn = get_connection()
        if not _user_owns_category(conn, category_id, g.user_id, expense=True) or not _user_owns_account(
            conn, account_id, g.user_id
        ):
            conn.close()
            flash("Invalid category or account.", "error")
            return redirect_home()
        conn.execute(
            """
            UPDATE expenses
            SET notes = ?, amount = ?, category_id = ?, account_id = ?, spent_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                notes,
                normalize_expense_amount(amount),
                category_id,
                account_id,
                spent_at,
                expense_id,
                g.user_id,
            ),
        )
        conn.commit()
        conn.close()

    return redirect_home()

@bp.route("/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM expenses WHERE id = ? AND user_id = ?",
        (expense_id, g.user_id),
    )
    conn.commit()
    conn.close()
    return redirect_home()
