from datetime import date, datetime, timedelta

from flask import Blueprint, flash, g, request

from db import get_connection, legacy_recurring_start
from finance import last_posted_recurring_date
from helpers import _parse_category_choice, _parse_recurring_duration, _parse_recurring_schedule, _recurring_category_ok, normalize_day_of_month, redirect_home

bp = Blueprint("recurring", __name__)


def _read_rule_form(conn, uid):
    """Validated fields shared by add and edit, or raise ValueError with the message to flash."""
    entry_type, category_id = _parse_category_choice(request.form.get("category_choice"))
    amount_raw = request.form.get("amount", "").strip()
    account_raw = request.form.get("account_id", "").strip()
    if not entry_type or category_id is None or not account_raw:
        raise ValueError("Choose type, category, and account.")
    if not _recurring_category_ok(conn, entry_type, category_id, uid):
        raise ValueError("Invalid category for that type.")
    try:
        account_id = int(account_raw)
        amt = abs(float(amount_raw))
    except (TypeError, ValueError):
        raise ValueError("Invalid amount or account.") from None
    if not conn.execute(
        "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
        (account_id, uid),
    ).fetchone():
        raise ValueError("Invalid account.")
    months_to_run = _parse_recurring_duration(request.form)
    unit, count, start_date = _parse_recurring_schedule(request.form)
    if amt <= 0:
        raise ValueError("Amount must be positive.")
    return {
        "entry_type": entry_type,
        "category_id": category_id,
        "account_id": account_id,
        "amount": amt,
        "months_to_run": months_to_run,
        "interval_unit": unit,
        "interval_count": count,
        "start_date": start_date,
        "notes": request.form.get("notes", "").strip(),
    }


@bp.route("/recurring/add", methods=["POST"])
def add_recurring():
    uid = g.user_id
    conn = get_connection()
    try:
        rule = _read_rule_form(conn, uid)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect_home(panel="recurring")

    if rule["start_date"] is None:
        # No first-date field: the original behaviour — from this month on the
        # chosen day, never back-dated before today.
        dom = normalize_day_of_month(request.form.get("day_of_month", "1"))
        start_date, not_before = legacy_recurring_start(datetime.now().date(), dom)
    else:
        # The first date is the first entry; a past date catches up from there.
        start_date, not_before = rule["start_date"], None
        dom = date.fromisoformat(start_date).day

    conn.execute(
        """
        INSERT INTO recurring_entries (
            user_id, entry_type, amount, category_id, account_id, day_of_month, months_to_run,
            interval_unit, interval_count, start_date, not_before, notes, enabled
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            uid, rule["entry_type"], rule["amount"], rule["category_id"], rule["account_id"], dom,
            rule["months_to_run"], rule["interval_unit"], rule["interval_count"], start_date,
            not_before, rule["notes"],
        ),
    )
    conn.commit()
    conn.close()
    flash("Recurring rule added.", "success")
    return redirect_home(panel="recurring")

@bp.route("/recurring/<int:recurring_id>/edit", methods=["POST"])
def edit_recurring(recurring_id):
    uid = g.user_id
    enabled = 1 if request.form.get("enabled") == "1" else 0
    conn = get_connection()
    existing = conn.execute(
        """
        SELECT day_of_month, interval_unit, interval_count, start_date, not_before
        FROM recurring_entries WHERE id = ? AND user_id = ?
        """,
        (recurring_id, uid),
    ).fetchone()
    if existing is None:
        conn.close()
        flash("Rule not found.", "error")
        return redirect_home(panel="recurring")
    try:
        rule = _read_rule_form(conn, uid)
    except ValueError as exc:
        conn.close()
        flash(str(exc), "error")
        return redirect_home(panel="recurring")

    if rule["start_date"] is None:
        start_date = existing["start_date"]
        dom = normalize_day_of_month(request.form.get("day_of_month", existing["day_of_month"]))
    elif rule["start_date"] == existing["start_date"]:
        # Unchanged first date: keep the stored day, which can be later than
        # the first date's (a rule on the 31st that started in February).
        start_date, dom = existing["start_date"], existing["day_of_month"]
    else:
        start_date = rule["start_date"]
        dom = date.fromisoformat(start_date).day

    not_before = existing["not_before"]
    schedule_changed = (rule["interval_unit"], rule["interval_count"], start_date, dom) != (
        existing["interval_unit"], existing["interval_count"], existing["start_date"], existing["day_of_month"],
    )
    if schedule_changed:
        # The new schedule's past dates are not in recurring_applied, so
        # without a floor the next page load would re-post everything the old
        # schedule already covered. Only post what falls after its last entry.
        last = last_posted_recurring_date(conn, recurring_id, existing["day_of_month"])
        if last is not None:
            floor = (last + timedelta(days=1)).isoformat()
            not_before = max(not_before or floor, floor)

    conn.execute(
        """
        UPDATE recurring_entries
        SET entry_type = ?, amount = ?, category_id = ?, account_id = ?, day_of_month = ?, months_to_run = ?,
            interval_unit = ?, interval_count = ?, start_date = ?, not_before = ?, notes = ?, enabled = ?
        WHERE id = ? AND user_id = ?
        """,
        (
            rule["entry_type"], rule["amount"], rule["category_id"], rule["account_id"], dom,
            rule["months_to_run"], rule["interval_unit"], rule["interval_count"], start_date,
            not_before, rule["notes"], enabled, recurring_id, uid,
        ),
    )
    conn.commit()
    conn.close()
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
