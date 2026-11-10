"""Answers to questions sent to the Telegram bot ("πόσα ξόδεψα σε delivery
αυτόν τον μήνα;"). The AI only turns the question into a plan
(telegram_parse.plan_question); every number here comes from the database."""

from __future__ import annotations

from datetime import date, timedelta

import finance
import shared
import tags
from telegram_parse import period_presets


def _post_recurring(conn, uid: int) -> None:
    """Post due recurring entries first, as the dashboard does, so answers match it."""
    if finance.apply_recurring_entries(conn, uid):
        conn.commit()


def _eur(value: float) -> str:
    return f"{value:,.2f}€".replace(",", " ")


def _period_label(start: date, end: date, today: date) -> str:
    for name, (a, b) in period_presets(today).items():
        if (a, b) == (start, end):
            return {
                "today": "today", "yesterday": "yesterday", "this_week": "this week",
                "last_week": "last week", "this_month": "this month", "last_month": "last month",
                "last_30_days": "in the last 30 days", "this_year": "this year", "last_year": "last year",
            }[name]
    if start == end:
        return f"on {start.isoformat()}"
    return f"{start.isoformat()} → {end.isoformat()}"


def _filters(plan: dict, alias: str, cat_alias: str) -> tuple[str, list]:
    sql, args = "", []
    if plan.get("category"):
        sql += f" AND lower({cat_alias}.name) = lower(?)"
        args.append(plan["category"])
    if plan.get("account_id"):
        sql += f" AND {alias}.account_id = ?"
        args.append(plan["account_id"])
    if plan.get("search"):
        sql += f" AND lower({alias}.notes) LIKE ?"
        args.append(f"%{plan['search'].lower()}%")
    return sql, args


def _total(conn, uid: int, plan: dict, kind: str) -> tuple[float, int]:
    if kind == "expense":
        where, args = _filters(plan, "e", "c")
        row = conn.execute(
            f"""
            SELECT COALESCE(-SUM(e.amount), 0) AS total, COUNT(*) AS n
            FROM expenses e JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ? AND date(e.spent_at) BETWEEN date(?) AND date(?){where}
            """,
            (uid, plan["from"].isoformat(), plan["to"].isoformat(), *args),
        ).fetchone()
    else:
        where, args = _filters(plan, "i", "c")
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(i.amount), 0) AS total, COUNT(*) AS n
            FROM income_entries i JOIN income_categories c ON c.id = i.category_id
            WHERE i.user_id = ? AND date(i.received_at) BETWEEN date(?) AND date(?){where}
            """,
            (uid, plan["from"].isoformat(), plan["to"].isoformat(), *args),
        ).fetchone()
    return float(row["total"] or 0), int(row["n"] or 0)


def _breakdown(conn, uid: int, start: date, end: date, account_id=None) -> list[tuple[str, float]]:
    extra, args = "", []
    if account_id:
        extra, args = " AND e.account_id = ?", [account_id]
    rows = conn.execute(
        f"""
        SELECT c.name AS name, -SUM(e.amount) AS total
        FROM expenses e JOIN categories c ON c.id = e.category_id
        WHERE e.user_id = ? AND date(e.spent_at) BETWEEN date(?) AND date(?){extra}
        GROUP BY c.id HAVING total > 0.004 ORDER BY total DESC
        """,
        (uid, start.isoformat(), end.isoformat(), *args),
    ).fetchall()
    return [(r["name"], float(r["total"])) for r in rows]


def format_breakdown(conn, uid: int, start: date, end: date, today: date, account_id=None, title=None) -> str:
    rows = _breakdown(conn, uid, start, end, account_id)
    label = title or f"Spending {_period_label(start, end, today)}"
    if not rows:
        return f"{label}: nothing recorded."
    total = sum(v for _, v in rows)
    lines = [f"{label}: {_eur(total)}"]
    for name, value in rows[:12]:
        lines.append(f"• {name}: {_eur(value)} ({value / total * 100:.0f}%)")
    if len(rows) > 12:
        rest = sum(v for _, v in rows[12:])
        lines.append(f"• {len(rows) - 12} more: {_eur(rest)}")
    income, _ = _total(conn, uid, {"from": start, "to": end}, "income")
    if income and not account_id:
        lines.append(f"Income: {_eur(income)} · Net: {_eur(income - total)}")
    return "\n".join(lines)


def month_summary(conn, uid: int, today: date | None = None, last: bool = False) -> str:
    today = today or date.today()
    _post_recurring(conn, uid)
    start, end = period_presets(today)["last_month" if last else "this_month"]
    title = f"{start.strftime('%B %Y')}"
    return format_breakdown(conn, uid, start, end, today, title=title)


def _recent(conn, uid: int, plan: dict, today: date) -> str:
    where_e, args_e = _filters(plan, "e", "c")
    where_i, args_i = _filters(plan, "i", "c")
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT date(e.spent_at) AS day, -e.amount AS amount, c.name AS category, e.notes, a.name AS account, e.id, 'e' AS kind
            FROM expenses e JOIN categories c ON c.id = e.category_id LEFT JOIN accounts a ON a.id = e.account_id
            WHERE e.user_id = ? AND date(e.spent_at) BETWEEN date(?) AND date(?){where_e}
            UNION ALL
            SELECT date(i.received_at), i.amount, c.name, i.notes, a.name, i.id, 'i'
            FROM income_entries i JOIN income_categories c ON c.id = i.category_id LEFT JOIN accounts a ON a.id = i.account_id
            WHERE i.user_id = ? AND date(i.received_at) BETWEEN date(?) AND date(?){where_i}
        ) ORDER BY day DESC, id DESC LIMIT 15
        """,
        (uid, plan["from"].isoformat(), plan["to"].isoformat(), *args_e,
         uid, plan["from"].isoformat(), plan["to"].isoformat(), *args_i),
    ).fetchall()
    if not rows:
        return f"No transactions {_period_label(plan['from'], plan['to'], today)}."
    lines = [f"Latest {_period_label(plan['from'], plan['to'], today)}:"]
    for r in rows:
        sign = "+" if r["kind"] == "i" else "−"
        note = f" {r['notes']}" if r["notes"] else ""
        lines.append(f"{r['day'][5:]} {sign}{_eur(float(r['amount']))} {r['category']}{note} ({r['account'] or '—'})")
    return "\n".join(lines)


def format_balances(conn, uid: int, balance_fn, account_id=None) -> str:
    _post_recurring(conn, uid)
    cutoff = (date.today() + timedelta(days=1)).isoformat()
    rows = balance_fn(conn, cutoff, uid)
    if account_id:
        rows = [r for r in rows if r["id"] == account_id]
    if not rows:
        return "No accounts yet."
    lines = ["Balances:"]
    total = 0.0
    for row in rows:
        balance = float(row["current_balance"])
        total += balance
        line = f"• {row['name']}: {_eur(balance)}"
        if row["account_type"] == "credit" and row["credit_limit"]:
            line += f" (card, {_eur(float(row['credit_limit']) + balance)} available)"
        lines.append(line)
    if len(rows) > 1:
        lines.append(f"Total: {_eur(total)}")
    return "\n".join(lines)


def format_shared(conn, uid: int) -> str:
    groups = shared.groups_for_bot(conn, uid)
    if not groups:
        return "You have no shared groups."
    lines = []
    for g in groups:
        net = shared.group_net_balances(conn, g["id"])
        payments = shared.simplify_debts(net)
        mine = [p for p in payments if g["me"] in (p[0], p[1])]
        if not payments:
            lines.append(f"{g['name']}: all settled.")
            continue
        parts = []
        for frm, to, cents in mine:
            if frm == g["me"]:
                parts.append(f"you owe {shared.person_display_name(conn, uid, to)} {_eur(cents / 100)}")
            else:
                parts.append(f"{shared.person_display_name(conn, uid, frm)} owes you {_eur(cents / 100)}")
        others = len(payments) - len(mine)
        tail = f" (+{others} between others)" if others else ""
        lines.append(f"{g['name']}: " + ("; ".join(parts) if parts else "you are settled") + tail)
    return "\n".join(lines)


def answer(conn, uid: int, plan: dict, balance_fn, today: date | None = None) -> str:
    today = today or date.today()
    _post_recurring(conn, uid)
    metric = plan.get("metric")
    label = _period_label(plan["from"], plan["to"], today)
    scope = []
    if plan.get("category"):
        scope.append(plan["category"])
    if plan.get("search"):
        scope.append(f"“{plan['search']}”")
    if plan.get("account"):
        scope.append(f"from {plan['account']}")
    scope_text = (" on " + " ".join(scope)) if scope else ""

    if metric == "balance":
        return format_balances(conn, uid, balance_fn, plan.get("account_id"))
    if metric == "shared":
        return format_shared(conn, uid)
    if metric == "recent":
        return _recent(conn, uid, plan, today)
    if metric == "income":
        total, n = _total(conn, uid, plan, "income")
        return f"Income{scope_text} {label}: {_eur(total)} ({n} entries)."
    if metric == "net":
        spent, _ = _total(conn, uid, plan, "expense")
        income, _ = _total(conn, uid, plan, "income")
        return f"{label.capitalize()}: income {_eur(income)}, spending {_eur(spent)}, net {_eur(income - spent)}."
    if metric == "spent" and (plan.get("category") or plan.get("search") or plan.get("account_id")):
        total, n = _total(conn, uid, plan, "expense")
        if plan.get("category") and not n:
            income, n_income = _total(conn, uid, plan, "income")
            if n_income:
                return f"Income{scope_text} {label}: {_eur(income)} ({n_income} entries)."
        return f"Spent{scope_text} {label}: {_eur(total)} ({n} entries)."
    if metric == "spent":
        total, n = _total(conn, uid, plan, "expense")
        top = _breakdown(conn, uid, plan["from"], plan["to"])[:3]
        top_text = ("\nTop: " + ", ".join(f"{name} {_eur(v)}" for name, v in top)) if top else ""
        return f"Spent {label}: {_eur(total)} ({n} entries).{top_text}"
    return format_breakdown(conn, uid, plan["from"], plan["to"], today, plan.get("account_id"))


def format_tag(conn, uid: int, tag: str) -> str:
    detail = tags.detail(conn, uid, tag)
    if not detail:
        return f"Nothing is tagged #{tags.clean_tag(tag) or tag} yet."
    span = detail["first"] if detail["first"] == detail["last"] else f"{detail['first']} → {detail['last']}"
    lines = [f"#{detail['tag']}: {_eur(detail['spent'])} spent · {detail['count']} entries · {span}"]
    if detail["income"]:
        lines.append(f"Income {_eur(detail['income'])} · net {_eur(detail['net'])}")
    for c in detail["categories"][:8]:
        lines.append(f"• {c['name']}: {_eur(c['total'])} ({c['share']:.0f}%)")
    if len(detail["categories"]) > 8:
        lines.append(f"• {len(detail['categories']) - 8} more categories")
    return "\n".join(lines)


def format_tags(conn, uid: int) -> str:
    rows = tags.summary(conn, uid)
    if not rows:
        return "No tags yet. Add one to any entry, e.g. καφές 3 #trip2026, or /tag trip2026 to tag everything for a while."
    lines = ["Tags (most recent first):"]
    for t in rows[:15]:
        lines.append(f"• #{t['tag']}: {_eur(t['spent'])} · {t['count']} entries")
    if len(rows) > 15:
        lines.append(f"…and {len(rows) - 15} more in the app under Reports → Tags.")
    lines.append("/tags NAME for one tag by category.")
    return "\n".join(lines)
