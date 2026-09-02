from flask import Blueprint, flash, g, request
from datetime import datetime, timedelta, timezone

from db import get_connection
from helpers import _user_owns_account, _user_owns_category, normalize_income_amount, normalize_txn_day_from_form, redirect_home

bp = Blueprint("income", __name__)


@bp.route("/income/<int:income_id>/delete", methods=["POST"])
def delete_income(income_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM income_entries WHERE id = ? AND user_id = ?",
        (income_id, g.user_id),
    )
    conn.commit()
    conn.close()
    return redirect_home()

@bp.route("/income/add", methods=["POST"])
def add_income():
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    account_raw = request.form.get("account_id", "").strip()
    category_raw = request.form.get("category_id", "").strip()

    if category_raw and account_raw:
        try:
            category_id = int(category_raw)
            account_id = int(account_raw)
        except (TypeError, ValueError):
            flash("Invalid category or account.", "error")
            return redirect_home()
        received_at = datetime.now().date().isoformat()
        conn = get_connection()
        if not _user_owns_category(conn, category_id, g.user_id, expense=False) or not _user_owns_account(
            conn, account_id, g.user_id
        ):
            conn.close()
            flash("Invalid category or account.", "error")
            return redirect_home()
        conn.execute(
            """
            INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                g.user_id,
                notes,
                normalize_income_amount(amount),
                category_id,
                account_id,
                received_at,
            ),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/income/<int:income_id>/edit", methods=["POST"])
def edit_income(income_id):
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    account_raw = request.form.get("account_id", "").strip()
    category_raw = request.form.get("category_id", "").strip()
    received_at_raw = request.form.get("received_at", "").strip()

    if category_raw and account_raw:
        try:
            category_id = int(category_raw)
            account_id = int(account_raw)
        except (TypeError, ValueError):
            flash("Invalid category or account.", "error")
            return redirect_home()
        received_at = normalize_txn_day_from_form(received_at_raw)
        conn = get_connection()
        if not _user_owns_category(conn, category_id, g.user_id, expense=False) or not _user_owns_account(
            conn, account_id, g.user_id
        ):
            conn.close()
            flash("Invalid category or account.", "error")
            return redirect_home()
        conn.execute(
            """
            UPDATE income_entries
            SET notes = ?, amount = ?, category_id = ?, account_id = ?, received_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                notes,
                normalize_income_amount(amount),
                category_id,
                account_id,
                received_at,
                income_id,
                g.user_id,
            ),
        )
        conn.commit()
        conn.close()

    return redirect_home()
