import sqlite3

from flask import Blueprint, flash, g, request

from db import get_connection
from helpers import redirect_home

bp = Blueprint("categories", __name__)


def _count_references(conn, category_id, user_id, *, entry_type):
    """How many rows would be orphaned by deleting this category.

    expenses/income_entries carry a real foreign key, so they are what raises
    IntegrityError. recurring_entries.category_id has no constraint, so those
    rows would be silently orphaned instead — worth blocking on for the same
    reason, and worth counting so the message names the true total.
    """
    ledger_table = "expenses" if entry_type == "expense" else "income_entries"
    ledger = conn.execute(
        f"SELECT COUNT(*) FROM {ledger_table} WHERE category_id = ? AND user_id = ?",
        (category_id, user_id),
    ).fetchone()[0]
    rules = conn.execute(
        "SELECT COUNT(*) FROM recurring_entries"
        " WHERE category_id = ? AND user_id = ? AND entry_type = ?",
        (category_id, user_id, entry_type),
    ).fetchone()[0]
    return int(ledger), int(rules)


def _describe_blockers(ledger_count, rule_count, noun, noun_plural):
    parts = []
    if ledger_count:
        parts.append(f"{ledger_count} {noun if ledger_count == 1 else noun_plural}")
    if rule_count:
        parts.append(f"{rule_count} recurring rule{'' if rule_count == 1 else 's'}")
    return " and ".join(parts)


def _delete_category(category_id, *, table, entry_type, noun, noun_plural):
    """Delete a category, refusing rather than 500ing when rows still point at it.

    The check and the delete share one connection so they see the same snapshot,
    and the except is not redundant with the check: another request can insert a
    referencing row in between. Both paths close in a finally — without it the
    IntegrityError escapes with the write transaction still open, and that
    leaked lock is what turns this into "database is locked" on the next write.
    """
    conn = get_connection()
    # Bound before the try so the IntegrityError handler cannot raise
    # UnboundLocalError over the top of the error it is meant to report.
    label = "That category"
    try:
        row = conn.execute(
            f"SELECT name FROM {table} WHERE id = ? AND user_id = ?",
            (category_id, g.user_id),
        ).fetchone()
        if row is None:
            flash("Category not found.", "error")
            return
        label = row["name"]

        ledger_count, rule_count = _count_references(
            conn, category_id, g.user_id, entry_type=entry_type
        )
        if ledger_count or rule_count:
            blockers = _describe_blockers(ledger_count, rule_count, noun, noun_plural)
            flash(
                f"“{label}” still has {blockers}. "
                f"Reassign or delete {'them' if ledger_count + rule_count > 1 else 'it'} first.",
                "error",
            )
            return

        cursor = conn.execute(
            f"DELETE FROM {table} WHERE id = ? AND user_id = ?",
            (category_id, g.user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            flash("Category not found.", "error")
    except sqlite3.IntegrityError:
        conn.rollback()
        flash(
            f"“{label}” is still in use, so it was not deleted. "
            "Reassign or delete its entries first.",
            "error",
        )
    finally:
        conn.close()


@bp.route("/categories/add", methods=["POST"])
def add_category():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO categories(user_id, name) VALUES (?, ?)",
            (g.user_id, name),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/categories/<int:category_id>/edit", methods=["POST"])
def edit_category(category_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "UPDATE categories SET name = ? WHERE id = ? AND user_id = ?",
            (name, category_id, g.user_id),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id):
    _delete_category(
        category_id,
        table="categories",
        entry_type="expense",
        noun="expense",
        noun_plural="expenses",
    )
    return redirect_home()

@bp.route("/income-categories/add", methods=["POST"])
def add_income_category():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO income_categories(user_id, name) VALUES (?, ?)",
            (g.user_id, name),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/income-categories/<int:category_id>/edit", methods=["POST"])
def edit_income_category(category_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "UPDATE income_categories SET name = ? WHERE id = ? AND user_id = ?",
            (name, category_id, g.user_id),
        )
        conn.commit()
        conn.close()
    return redirect_home()

@bp.route("/income-categories/<int:category_id>/delete", methods=["POST"])
def delete_income_category(category_id):
    _delete_category(
        category_id,
        table="income_categories",
        entry_type="income",
        noun="income entry",
        noun_plural="income entries",
    )
    return redirect_home()
