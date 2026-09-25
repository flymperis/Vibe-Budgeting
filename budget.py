"""Per-category monthly expense budgets.

Each expense category can carry a default monthly amount, and any single month
can override it (a bigger December, a quiet August). There is no rollover: every
month is measured against its own effective budget only.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date

from helpers import month_bounds_dates

# Sane ceiling for a single budget amount; guards against inf/1e999-style junk
# reaching the database (parse_amount already rejects non-finite values).
MAX_AMOUNT = 1_000_000_000.0


def _money(value) -> float:
    """Round to the cent. Sums of stored REALs pick up float noise (e.g. a
    -SUM(amount) of 0.30000000000000004) that must not leak into a status
    comparison or an on-screen total."""
    return round(float(value), 2)

# Share of the budget spent at which a category is flagged as close to its limit.
WARN_RATIO = 0.8


def migrate_budgets(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS budget_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS category_budgets (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            amount REAL NOT NULL CHECK (amount >= 0),
            PRIMARY KEY (user_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS category_budget_overrides (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            ym TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount >= 0),
            PRIMARY KEY (user_id, category_id, ym)
        );

        CREATE TABLE IF NOT EXISTS category_budget_flags (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
            fixed INTEGER NOT NULL DEFAULT 1 CHECK (fixed IN (0, 1)),
            PRIMARY KEY (user_id, category_id)
        );

        CREATE TABLE IF NOT EXISTS budget_month_cleared (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ym TEXT NOT NULL,
            PRIMARY KEY (user_id, ym)
        );
        """
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(budget_settings)")}
    if "other_amount" not in columns:
        conn.execute(
            "ALTER TABLE budget_settings ADD COLUMN other_amount REAL CHECK (other_amount IS NULL OR other_amount >= 0)"
        )


def is_enabled(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute(
        "SELECT enabled FROM budget_settings WHERE user_id = ?", (int(user_id),)
    ).fetchone()
    return bool(row and row["enabled"])


def set_enabled(conn: sqlite3.Connection, user_id: int, enabled: bool) -> None:
    conn.execute(
        """
        INSERT INTO budget_settings (user_id, enabled) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled
        """,
        (int(user_id), 1 if enabled else 0),
    )


def get_other_amount(conn: sqlite3.Connection, user_id: int) -> float | None:
    row = conn.execute(
        "SELECT other_amount FROM budget_settings WHERE user_id = ?", (int(user_id),)
    ).fetchone()
    return None if row is None or row["other_amount"] is None else float(row["other_amount"])


def set_other_amount(conn: sqlite3.Connection, user_id: int, amount: float | None) -> None:
    """Set the default budget for categories with no budget of their own; None clears it.

    Upserts without disturbing `enabled` for users who have no budget_settings row yet.
    """
    conn.execute(
        """
        INSERT INTO budget_settings (user_id, enabled, other_amount) VALUES (?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET other_amount = excluded.other_amount
        """,
        (int(user_id), None if amount is None else float(amount)),
    )


def parse_amount(raw) -> float | None:
    """Form value → amount, None for blank. Raises ValueError on junk, negatives,
    non-finite values (NaN/inf), or anything past a sane ceiling."""
    text = str(raw if raw is not None else "").strip().replace(",", ".")
    if not text:
        return None
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Budget amounts must be finite numbers.")
    value = round(value, 2)
    if value < 0:
        raise ValueError("Budget amounts cannot be negative.")
    if value > MAX_AMOUNT:
        raise ValueError(f"Budget amounts cannot exceed {MAX_AMOUNT:.0f}.")
    return value


def category_owned(conn: sqlite3.Connection, user_id: int, category_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM categories WHERE id = ? AND user_id = ?",
            (int(category_id), int(user_id)),
        ).fetchone()
        is not None
    )


def set_default(conn, user_id, category_id, amount: float | None) -> None:
    """Set a category's default monthly budget; None removes it."""
    if amount is None:
        conn.execute(
            "DELETE FROM category_budgets WHERE user_id = ? AND category_id = ?",
            (int(user_id), int(category_id)),
        )
        return
    conn.execute(
        """
        INSERT INTO category_budgets (user_id, category_id, amount) VALUES (?, ?, ?)
        ON CONFLICT(user_id, category_id) DO UPDATE SET amount = excluded.amount
        """,
        (int(user_id), int(category_id), float(amount)),
    )


def set_override(conn, user_id, category_id, ym: str, amount: float | None) -> None:
    """Set one month's budget for a category; None falls back to the default."""
    if amount is None:
        conn.execute(
            "DELETE FROM category_budget_overrides WHERE user_id = ? AND category_id = ? AND ym = ?",
            (int(user_id), int(category_id), ym),
        )
        return
    conn.execute(
        """
        INSERT INTO category_budget_overrides (user_id, category_id, ym, amount) VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, category_id, ym) DO UPDATE SET amount = excluded.amount
        """,
        (int(user_id), int(category_id), ym, float(amount)),
    )


def is_month_cleared(conn: sqlite3.Connection, user_id: int, ym: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM budget_month_cleared WHERE user_id = ? AND ym = ?",
            (int(user_id), ym),
        ).fetchone()
        is not None
    )


def clear_month(conn: sqlite3.Connection, user_id: int, ym: str) -> None:
    """Wipe one month's budget entirely: mark it cleared (so defaults and
    "everything else" stop applying to it) and drop its existing overrides.
    The user can then set individual overrides for `ym`, or restore_month()
    to go back to the normal monthly budgets."""
    uid = int(user_id)
    conn.execute(
        "INSERT OR IGNORE INTO budget_month_cleared (user_id, ym) VALUES (?, ?)",
        (uid, ym),
    )
    conn.execute(
        "DELETE FROM category_budget_overrides WHERE user_id = ? AND ym = ?",
        (uid, ym),
    )


def restore_month(conn: sqlite3.Connection, user_id: int, ym: str) -> None:
    """Undo clear_month(): `ym` goes back to using defaults/other_amount as
    usual. Any overrides set for `ym` after it was cleared are kept."""
    conn.execute(
        "DELETE FROM budget_month_cleared WHERE user_id = ? AND ym = ?",
        (int(user_id), ym),
    )


def fixed_categories(conn: sqlite3.Connection, user_id: int) -> set[int]:
    """Category ids the user has marked "fixed" (e.g. rent) — never auto-adjusted
    by the frontend's distribute helper."""
    return {
        int(row["category_id"])
        for row in conn.execute(
            "SELECT category_id FROM category_budget_flags WHERE user_id = ? AND fixed = 1",
            (int(user_id),),
        )
    }


def set_fixed(conn: sqlite3.Connection, user_id: int, category_id: int, fixed: bool) -> None:
    """Mark a category fixed/not-fixed; works whether or not it has a default amount yet."""
    if not fixed:
        conn.execute(
            "DELETE FROM category_budget_flags WHERE user_id = ? AND category_id = ?",
            (int(user_id), int(category_id)),
        )
        return
    conn.execute(
        """
        INSERT INTO category_budget_flags (user_id, category_id, fixed) VALUES (?, ?, 1)
        ON CONFLICT(user_id, category_id) DO UPDATE SET fixed = 1
        """,
        (int(user_id), int(category_id)),
    )


def default_budgets(conn, user_id) -> dict[int, float]:
    return {
        int(row["category_id"]): float(row["amount"])
        for row in conn.execute(
            "SELECT category_id, amount FROM category_budgets WHERE user_id = ?",
            (int(user_id),),
        )
    }


def _status(spent: float, budget: float) -> str:
    if spent > budget:
        return "over"
    if budget > 0 and spent >= budget * WARN_RATIO:
        return "warn"
    return "ok"


def budget_for_month(conn: sqlite3.Connection, user_id: int, ym: str) -> dict:
    """Budget vs actual spending, per expense category, for one YYYY-MM month.

    rows holds every category with an effective budget (default or override),
    even when nothing was spent. Categories with spending but no budget go in
    unbudgeted, and are kept out of the totals so the headline figure compares
    like with like.

    When `ym` has been cleared (clear_month), category defaults and
    "everything else" are ignored for this month only — only an explicit
    override for `ym` counts as a budget, so right after clearing every
    category with spending shows up as unbudgeted.
    """
    uid = int(user_id)
    cleared = is_month_cleared(conn, uid, ym)
    start_d, end_d = month_bounds_dates(ym)
    categories = conn.execute(
        """
        SELECT c.id, c.name, b.amount AS default_amount, o.amount AS override_amount
        FROM categories c
        LEFT JOIN category_budgets b ON b.user_id = c.user_id AND b.category_id = c.id
        LEFT JOIN category_budget_overrides o
            ON o.user_id = c.user_id AND o.category_id = c.id AND o.ym = ?
        WHERE c.user_id = ?
        ORDER BY c.name
        """,
        (ym, uid),
    ).fetchall()
    spent_by_category = {
        int(row["category_id"]): float(row["spent"])
        for row in conn.execute(
            """
            SELECT category_id, COALESCE(-SUM(amount), 0) AS spent
            FROM expenses
            WHERE user_id = ? AND date(spent_at) >= date(?) AND date(spent_at) < date(?)
            GROUP BY category_id
            """,
            (uid, start_d, end_d),
        )
    }

    rows = []
    unbudgeted = []
    for cat in categories:
        cid = int(cat["id"])
        spent = _money(spent_by_category.get(cid, 0.0))
        default_amount = None if cleared else cat["default_amount"]
        override_amount = cat["override_amount"]
        if default_amount is None and override_amount is None:
            if spent:
                unbudgeted.append({"category_id": cid, "name": cat["name"], "spent": spent})
            continue
        budget = float(override_amount if override_amount is not None else default_amount)
        rows.append(
            {
                "category_id": cid,
                "name": cat["name"],
                "default": None if default_amount is None else float(default_amount),
                "override": None if override_amount is None else float(override_amount),
                "budget": budget,
                "spent": spent,
                "remaining": _money(budget - spent),
                "pct": (spent / budget * 100) if budget > 0 else (100.0 if spent > 0 else 0.0),
                "status": _status(spent, budget),
            }
        )

    unbudgeted_spent = _money(sum(r["spent"] for r in unbudgeted))

    other_amount = None if cleared else get_other_amount(conn, uid)
    other = None
    if other_amount is not None:
        other = {
            "name": "Everything else",
            "budget": other_amount,
            "spent": unbudgeted_spent,
            "remaining": _money(other_amount - unbudgeted_spent),
            "pct": (unbudgeted_spent / other_amount * 100) if other_amount > 0 else (100.0 if unbudgeted_spent > 0 else 0.0),
            "status": _status(unbudgeted_spent, other_amount),
        }

    total_budget = _money(sum(r["budget"] for r in rows) + (other["budget"] if other else 0.0))
    total_spent = _money(sum(r["spent"] for r in rows) + (other["spent"] if other else 0.0))
    over = [r for r in rows if r["status"] == "over"]
    if other and other["status"] == "over":
        over.append(other)
    return {
        "ym": ym,
        "cleared": cleared,
        "rows": rows,
        "unbudgeted": unbudgeted,
        "unbudgeted_spent": unbudgeted_spent,
        "other": other,
        "total_budget": total_budget,
        "total_spent": total_spent,
        "total_remaining": _money(total_budget - total_spent),
        "total_pct": (total_spent / total_budget * 100) if total_budget > 0 else (100.0 if total_spent > 0 else 0.0),
        "total_status": _status(total_spent, total_budget),
        "over": over,
        "by_category": {r["category_id"]: r for r in rows},
    }


def _shift_month(ym: str, delta: int) -> str:
    """YYYY-MM shifted by delta whole months (delta may be negative)."""
    y, m = (int(p) for p in ym.split("-", 1))
    idx = (y * 12 + (m - 1)) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def spending_history(
    conn: sqlite3.Connection,
    user_id: int,
    ym: str,
    months: int = 3,
    inactive_months: int = 6,
    today: date | None = None,
    category_ids: list[int] | None = None,
    defaults: dict[int, float] | None = None,
) -> dict[int, dict]:
    """Recent net spending per expense category, used to suggest a budget.

    avg/suggested are computed over the `months` full calendar months before
    `ym` (the selected month itself is excluded), and only over the months on
    or after the user's first-ever expense and on or before the real current
    month, so a brand-new user isn't divided by a window they haven't lived
    through yet, and a `ym` planned ahead of today isn't diluted by months
    that haven't happened.

    `category_ids` and `defaults` let a caller that already has them (the
    dashboard) skip re-querying categories / category_budgets.
    """
    uid = int(user_id)
    if category_ids is None:
        category_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM categories WHERE user_id = ?", (uid,))
        ]
    if not category_ids:
        return {}

    if today is None:
        today = date.today()
    today_ym = today.strftime("%Y-%m")

    first_row = conn.execute(
        "SELECT MIN(spent_at) AS first_date FROM expenses WHERE user_id = ?",
        (uid,),
    ).fetchone()
    first_date = first_row["first_date"] if first_row else None
    first_ym = first_date[:7] if first_date else None

    window_start_ym = _shift_month(ym, -months)
    inactive_start_ym = _shift_month(ym, -inactive_months)
    window_start_d, _ = month_bounds_dates(window_start_ym)
    inactive_start_d, _ = month_bounds_dates(inactive_start_ym)
    current_start_d, current_end_d = month_bounds_dates(ym)
    # Both the avg window and the "recent activity" window stop at the start
    # of `ym` — the selected month itself is handled separately below.
    window_end_d = current_start_d
    inactive_end_d = current_start_d
    overall_start_d = min(window_start_d, inactive_start_d)

    # One scan of expenses covering the widest range needed, bucketed by CASE
    # into the 3 windows (avg / recent-activity / the selected month itself)
    # instead of running three separate queries.
    sums = {
        int(row["category_id"]): (
            float(row["avg_sum"]),
            float(row["recent_sum"]),
            float(row["current_sum"]),
        )
        for row in conn.execute(
            """
            SELECT category_id,
                COALESCE(SUM(CASE WHEN date(spent_at) >= date(?) AND date(spent_at) < date(?)
                    THEN amount ELSE 0 END), 0) AS avg_sum,
                COALESCE(SUM(CASE WHEN date(spent_at) >= date(?) AND date(spent_at) < date(?)
                    THEN amount ELSE 0 END), 0) AS recent_sum,
                COALESCE(SUM(CASE WHEN date(spent_at) >= date(?) AND date(spent_at) < date(?)
                    THEN amount ELSE 0 END), 0) AS current_sum
            FROM expenses
            WHERE user_id = ? AND date(spent_at) >= date(?) AND date(spent_at) < date(?)
            GROUP BY category_id
            """,
            (
                window_start_d, window_end_d,
                inactive_start_d, inactive_end_d,
                current_start_d, current_end_d,
                uid, overall_start_d, current_end_d,
            ),
        )
    }

    # Count only window months on/after the user's first-ever expense and
    # on/before the real current month.
    counted_months = 0
    if first_ym is not None:
        for i in range(months):
            month_ym = _shift_month(window_start_ym, i)
            if first_ym <= month_ym <= today_ym:
                counted_months += 1

    if defaults is None:
        defaults = default_budgets(conn, uid)
    override_category_ids = {
        int(row["category_id"])
        for row in conn.execute(
            "SELECT category_id FROM category_budget_overrides WHERE user_id = ? AND ym = ?",
            (uid, ym),
        )
    }

    result = {}
    for cid in category_ids:
        avg_sum, recent_sum, current_sum = sums.get(cid, (0.0, 0.0, 0.0))
        net_avg = _money(-avg_sum)
        net_recent = _money(-recent_sum)
        net_current = _money(-current_sum)
        avg = _money(max(0.0, net_avg / counted_months)) if counted_months > 0 else 0.0
        suggested = math.ceil(avg / 5) * 5 if avg > 0 else 0.0
        active = (
            net_recent > 0
            or net_current > 0  # spending in the selected month itself also counts
            or cid in defaults
            or cid in override_category_ids
        )
        result[cid] = {"avg": avg, "suggested": float(suggested), "active": active}
    return result


def month_budget_snapshots(conn: sqlite3.Connection, user_id: int, limit: int = 24) -> list[dict]:
    """Months the user actually set an override for, newest first, each with
    the *effective* budget (override if set for that month, else the
    category's default) for every category that has one or the other.
    Categories with neither are omitted. For a cleared month, defaults don't
    apply — its effective amounts are its overrides only, so a cleared month
    with no overrides left simply isn't listed. Meant to let the frontend
    offer "copy budgets from month X" without a server round-trip: the caller
    picks a snapshot and prefills the manual-budgets form with it client-side.
    """
    uid = int(user_id)
    defaults = default_budgets(conn, uid)
    cleared_yms = {
        str(row["ym"]) for row in conn.execute(
            "SELECT ym FROM budget_month_cleared WHERE user_id = ?", (uid,)
        )
    }

    overrides_by_ym: dict[str, dict[int, float]] = {}
    for row in conn.execute(
        "SELECT ym, category_id, amount FROM category_budget_overrides WHERE user_id = ? ORDER BY ym DESC",
        (uid,),
    ):
        ym = str(row["ym"])
        overrides_by_ym.setdefault(ym, {})[int(row["category_id"])] = float(row["amount"])

    result = []
    for ym in sorted(overrides_by_ym, reverse=True)[:limit]:
        if ym in cleared_yms:
            amounts = dict(overrides_by_ym[ym])
        else:
            amounts = dict(defaults)
            amounts.update(overrides_by_ym[ym])
        if amounts:
            result.append({"ym": ym, "amounts": amounts})
    return result
