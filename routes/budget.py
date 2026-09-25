import re

from flask import Blueprint, flash, g, request

import budget
from db import get_connection
from helpers import redirect_home

bp = Blueprint("budget", __name__)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _strict_month(raw):
    """YYYY-MM with a real month number, or None — unlike normalize_month, a
    bad value is not silently swapped for the current month."""
    text = (raw or "").strip()
    return text if _MONTH_RE.match(text) else None


@bp.route("/budget/toggle", methods=["POST"])
def toggle_budget():
    enabled = request.form.get("enabled") == "1"
    conn = get_connection()
    try:
        budget.set_enabled(conn, g.user_id, enabled)
        conn.commit()
    finally:
        conn.close()
    flash("Budgeting enabled." if enabled else "Budgeting disabled.", "success")
    return redirect_home(panel="budget")

@bp.route("/budget/defaults", methods=["POST"])
def save_budget_defaults():
    """Every category's default monthly budget in one form: amount_<category_id>,
    plus an optional fixed_<category_id> checkbox ("1" when checked, absent
    when unchecked — standard HTML).

    All values are parsed before anything is written, so one bad field leaves
    the saved budgets untouched instead of half-applied. The fixed flag is
    only touched for categories whose amount field was actually submitted —
    the form always posts every row, so a present amount + absent checkbox
    unambiguously means "not fixed", rather than "field omitted".
    """
    uid = g.user_id
    conn = get_connection()
    try:
        category_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM categories WHERE user_id = ?", (uid,))
        ]
        updates = []
        fixed_updates = []
        for cid in category_ids:
            key = f"amount_{cid}"
            if key not in request.form:
                continue
            try:
                updates.append((cid, budget.parse_amount(request.form.get(key))))
            except ValueError:
                flash("Budget amounts must be numbers of zero or more.", "error")
                return redirect_home(panel="budget")
            fixed_updates.append((cid, request.form.get(f"fixed_{cid}") == "1"))
        other_amount = None
        other_provided = "other_amount" in request.form
        if other_provided:
            try:
                other_amount = budget.parse_amount(request.form.get("other_amount"))
            except ValueError:
                flash("Budget amounts must be numbers of zero or more.", "error")
                return redirect_home(panel="budget")
        for cid, amount in updates:
            budget.set_default(conn, uid, cid, amount)
        for cid, fixed in fixed_updates:
            budget.set_fixed(conn, uid, cid, fixed)
        if other_provided:
            budget.set_other_amount(conn, uid, other_amount)
        conn.commit()
    finally:
        conn.close()
    flash("Monthly budgets saved.", "success")
    return redirect_home(panel="budget")

@bp.route("/budget/override", methods=["POST"])
def save_budget_override():
    uid = g.user_id
    ym = _strict_month(request.form.get("month"))
    if ym is None:
        flash("Invalid month.", "error")
        return redirect_home(panel="budget")
    try:
        category_id = int(request.form.get("category_id", ""))
        amount = None if request.form.get("clear") == "1" else budget.parse_amount(request.form.get("amount"))
    except (TypeError, ValueError):
        flash("Invalid budget amount.", "error")
        return redirect_home(panel="budget")

    conn = get_connection()
    try:
        if not budget.category_owned(conn, uid, category_id):
            flash("Category not found.", "error")
            return redirect_home(panel="budget")
        budget.set_override(conn, uid, category_id, ym, amount)
        conn.commit()
    finally:
        conn.close()
    flash("Budget for this month reset to the default." if amount is None else "Budget for this month saved.", "success")
    return redirect_home(panel="budget")

@bp.route("/budget/copy-last-month", methods=["POST"])
def copy_last_month():
    uid = g.user_id
    ym = _strict_month(request.form.get("month"))
    if ym is None:
        flash("Invalid month.", "error")
        return redirect_home(panel="budget")
    conn = get_connection()
    info = {}
    try:
        copied = budget.copy_previous_month_actuals(conn, uid, ym, info=info)
        conn.commit()
    finally:
        conn.close()
    if copied:
        flash(f"Copied last month's spending into {copied} categor{'y' if copied == 1 else 'ies'}.", "success")
    elif info.get("had_spending") and info.get("skipped"):
        flash(
            "Nothing to copy — every category with spending last month is fixed or covered by Everything else.",
            "info",
        )
    else:
        flash("No spending last month to copy.", "info")
    return redirect_home(panel="budget")
