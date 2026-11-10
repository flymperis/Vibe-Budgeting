"""Tags: #hashtags written in a transaction's notes ("καφές #διακοπές2026").

Tags live in the notes text itself, so they need no extra tables and travel
with every row that has notes: expenses, income, recurring rules (their
posted rows copy the notes), shared bills (the personal share copies the
description) and Excel export/import. This module finds them, groups rows by
tag and adds them up, whatever category each row is in.

Tags compare case- and accent-insensitively: #Διακοπές2026 and #διακοπες2026
are the same tag; the most recent spelling is the one shown.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlencode

from markupsafe import Markup, escape

# A tag starts after a space or the start of the text, is up to 50 word
# characters (letters in any script, digits, _ and -), and needs a letter.
TAG_RE = re.compile(r"(?<![\w&#/])#(\w[\w-]{0,49})")


def tag_key(tag: str) -> str:
    text = unicodedata.normalize("NFD", str(tag or "").lstrip("#").lower())
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _valid(tag: str) -> bool:
    return any(ch.isalpha() for ch in tag)


def find_tags(text: str) -> list[str]:
    """Tags in the text, without '#', first spelling of each, in order."""
    seen, out = set(), []
    for m in TAG_RE.finditer(text or ""):
        tag = m.group(1).rstrip("-")
        k = tag_key(tag)
        if _valid(tag) and k not in seen:
            seen.add(k)
            out.append(tag)
    return out


def strip_tags(text: str) -> str:
    """The text without its tags (for descriptions and category lookups)."""
    cleaned = TAG_RE.sub(lambda m: "" if _valid(m.group(1)) else m.group(0), text or "")
    return " ".join(cleaned.split())


def add_tags(notes: str, tags: list[str]) -> str:
    """notes with each tag appended unless it is already there."""
    present = {tag_key(t) for t in find_tags(notes)}
    extra = [f"#{t.lstrip('#')}" for t in tags if t and tag_key(t) not in present]
    return " ".join(part for part in [(notes or "").strip(), *extra] if part)


def clean_tag(raw: str) -> str | None:
    """A tag typed on its own ("διακοπές 2026", "#trip") → "διακοπές2026"."""
    text = re.sub(r"[^\w-]", "", str(raw or "").lstrip("#").replace(" ", ""))[:50]
    return text if text and _valid(text) else None


# --- totals ------------------------------------------------------------------------


def _tagged_rows(conn, user_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT 'expense' AS kind, e.id AS id, date(e.spent_at) AS day, e.amount AS amount, e.notes AS notes,
                   c.name AS category, a.name AS account
            FROM expenses e JOIN categories c ON c.id = e.category_id LEFT JOIN accounts a ON a.id = e.account_id
            WHERE e.user_id = ? AND e.notes LIKE '%#%'
            UNION ALL
            SELECT 'income', i.id, date(i.received_at), i.amount, i.notes, c.name, a.name
            FROM income_entries i JOIN income_categories c ON c.id = i.category_id
            LEFT JOIN accounts a ON a.id = i.account_id
            WHERE i.user_id = ? AND i.notes LIKE '%#%'
        )
        ORDER BY day DESC, id DESC
        """,
        (int(user_id), int(user_id)),
    ).fetchall()
    out = []
    for r in rows:
        found = find_tags(r["notes"])
        if found:
            out.append({**dict(r), "tags": found})
    return out


def summary(conn, user_id: int) -> list[dict]:
    """Every tag with its totals, most recently used first:
    [{tag, key, spent, income, net, count, first, last}]. spent nets refunds."""
    tags: dict[str, dict] = {}
    for r in _tagged_rows(conn, user_id):
        for tag in r["tags"]:
            k = tag_key(tag)
            t = tags.setdefault(k, {"tag": tag, "key": k, "spent": 0.0, "income": 0.0, "count": 0,
                                    "first": r["day"], "last": r["day"]})
            if r["kind"] == "expense":
                t["spent"] -= float(r["amount"])
            else:
                t["income"] += float(r["amount"])
            t["count"] += 1
            t["first"] = min(t["first"], r["day"])
            t["last"] = max(t["last"], r["day"])
    for t in tags.values():
        t["spent"] = round(t["spent"], 2)
        t["income"] = round(t["income"], 2)
        t["net"] = round(t["income"] - t["spent"], 2)
    return sorted(tags.values(), key=lambda t: (t["last"], t["count"]), reverse=True)


def detail(conn, user_id: int, tag: str) -> dict | None:
    """One tag: totals, spending per category and every transaction, or None
    when no row carries it."""
    k = tag_key(tag)
    rows = [r for r in _tagged_rows(conn, user_id) if k in {tag_key(t) for t in r["tags"]}]
    if not rows:
        return None
    spent = -sum(float(r["amount"]) for r in rows if r["kind"] == "expense")
    income = sum(float(r["amount"]) for r in rows if r["kind"] == "income")
    by_category: dict[str, float] = {}
    for r in rows:
        if r["kind"] == "expense":
            by_category[r["category"]] = by_category.get(r["category"], 0.0) - float(r["amount"])
    categories = [
        {"name": name, "total": round(total, 2), "share": (total / spent * 100) if spent > 0 else 0.0}
        for name, total in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    ]
    display = next(t for t in rows[0]["tags"] if tag_key(t) == k)
    return {
        "tag": display,
        "key": k,
        "spent": round(spent, 2),
        "income": round(income, 2),
        "net": round(income - spent, 2),
        "count": len(rows),
        "first": min(r["day"] for r in rows),
        "last": max(r["day"] for r in rows),
        "categories": categories,
        "rows": rows,
    }


def recent(conn, user_id: int, limit: int = 8) -> list[str]:
    return [t["tag"] for t in summary(conn, user_id)[:limit]]


# --- display ------------------------------------------------------------------------


def tag_url(tag: str) -> str:
    return "/?" + urlencode({"panel": "reports", "reports_section": "tags", "tag": tag_key(tag)})


def linkify(notes: str) -> Markup:
    """Notes as HTML with each tag a link to its report (everything escaped)."""
    text = str(notes or "")
    out, pos = [], 0
    for m in TAG_RE.finditer(text):
        if not _valid(m.group(1)):
            continue
        out.append(escape(text[pos:m.start()]))
        out.append(Markup('<a class="tag-chip" href="{}">#{}</a>').format(tag_url(m.group(1)), m.group(1)))
        pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("").join(out)
