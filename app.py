from flask import Flask, flash, g, redirect, render_template, request, send_file, session, url_for
import calendar
import os
import sys
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlparse

from openpyxl import Workbook, load_workbook
from werkzeug.security import check_password_hash, generate_password_hash

def _sqlite_db_path():
    raw = os.environ.get("DATABASE_PATH") or os.environ.get("VB_DATABASE_PATH") or "database.db"
    return os.path.abspath(raw)


DB_PATH = _sqlite_db_path()


def _prepare_sqlite_storage():
    """Ensure parent dir exists; catch Docker bind-mount mistakes (path is a directory)."""
    parent = os.path.dirname(DB_PATH)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, mode=0o755, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Cannot create database directory {parent!r}: {exc}"
            ) from exc

    if os.path.exists(DB_PATH) and os.path.isdir(DB_PATH):
        raise RuntimeError(
            f"DATABASE_PATH {DB_PATH!r} is a directory, not a SQLite file. "
            "Docker often creates a directory when a single-file bind mount source was missing."
        ) from None


EXPORT_FORMAT_VERSION = 1
LIST_PAGE_SIZE = 75
TRANSFER_LOG_LIMIT = 10
ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "true").lower() in ("1", "true", "yes")
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")

SHEET_META = "_meta"
SHEET_ACCOUNTS = "Accounts"
SHEET_EXPENSE_CATEGORIES = "ExpenseCategories"
SHEET_INCOME_CATEGORIES = "IncomeCategories"
SHEET_EXPENSES = "Expenses"
SHEET_INCOME = "Income"
ALLOWED_PANELS = {
    "home",
    "expenses",
    "income",
    "recurring",
    "transfer",
    "summary",
    "yearly",
    "reports",
    "settings",
}
SETTINGS_SECTIONS = {"general", "banks", "expenses", "income", "export", "migration"}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")


@app.before_request
def _require_login():
    if request.endpoint in ("login", "register", "static", None):
        return None
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("login", next=request.path))
    try:
        g.user_id = int(uid)
    except (TypeError, ValueError):
        session.pop("user_id", None)
        session.pop("username", None)
        return redirect(url_for("login", next=request.path))
    g.username = session.get("username") or ""
    return None


def normalize_expense_amount(raw):
    """Signed expense movements: negative = spending, positive = refund/credit."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        value = 0.0
    return value


def normalize_income_amount(raw):
    """Income amounts are stored positive."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        value = 0.0
    return abs(value)


def normalize_list_page(value):
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return 1


def normalize_day_of_month(value):
    try:
        d = int(str(value).strip())
        return min(31, max(1, d))
    except (TypeError, ValueError):
        return 1


def _parse_row_created_date(row):
    raw = row["created_at"]
    if raw is None:
        return datetime.now().date()
    s = str(raw)
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return datetime.now().date()


def _due_date_in_month(ym: str, day_of_month: int):
    y, m = map(int, ym.split("-", 1))
    last = calendar.monthrange(y, m)[1]
    d = min(day_of_month, last)
    return datetime(y, m, d).date()


def _month_iter(start_ym: str, end_ym: str):
    y, m = map(int, start_ym.split("-", 1))
    ey, em = map(int, end_ym.split("-", 1))
    while (y, m) <= (ey, em):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            m = 1
            y += 1


def apply_recurring_entries(conn, user_id):
    """Insert expense/income rows for due recurring rules (catch-up through current month)."""
    uid = int(user_id)
    today = datetime.now().date()
    today_ym = f"{today.year:04d}-{today.month:02d}"
    rules = conn.execute(
        """
        SELECT id, entry_type, amount, category_id, account_id, day_of_month, notes, created_at
        FROM recurring_entries
        WHERE enabled = 1 AND user_id = ?
        """,
        (uid,),
    ).fetchall()
    posted = 0
    for rule in rules:
        created_d = _parse_row_created_date(rule)
        start_ym = f"{created_d.year:04d}-{created_d.month:02d}"
        note_text = (rule["notes"] or "").strip()
        prefix = "[Recurring] "
        full_notes = f"{prefix}{note_text}" if note_text else prefix.strip()

        for ym in _month_iter(start_ym, today_ym):
            exists = conn.execute(
                "SELECT 1 FROM recurring_applied WHERE recurring_id = ? AND ym = ?",
                (rule["id"], ym),
            ).fetchone()
            if exists:
                continue
            due = _due_date_in_month(ym, rule["day_of_month"])
            if due < created_d:
                continue
            if today < due:
                continue
            et = rule["entry_type"]
            if et == "expense":
                conn.execute(
                    """
                    INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        full_notes,
                        -abs(float(rule["amount"])),
                        int(rule["category_id"]),
                        int(rule["account_id"]),
                        due.isoformat(),
                    ),
                )
            elif et == "income":
                conn.execute(
                    """
                    INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        full_notes,
                        abs(float(rule["amount"])),
                        int(rule["category_id"]),
                        int(rule["account_id"]),
                        due.isoformat(),
                    ),
                )
            else:
                continue
            conn.execute(
                "INSERT INTO recurring_applied (recurring_id, ym) VALUES (?, ?)",
                (rule["id"], ym),
            )
            posted += 1
    return posted


def normalize_year(value):
    raw = (value or "").strip()
    if not raw:
        return datetime.now().year
    try:
        year_num = int(raw)
        if year_num < 2000 or year_num > 2100:
            raise ValueError
        return year_num
    except ValueError:
        return datetime.now().year


def resolve_month_filter_from_request():
    """Primary UI month: ?month=YYYY-MM or ?cal_year=&cal_month= (from dropdowns)."""
    raw = (request.args.get("month") or "").strip()
    if raw:
        return normalize_month(raw)
    y_raw = (request.args.get("cal_year") or "").strip()
    m_raw = (request.args.get("cal_month") or "").strip()
    if y_raw and m_raw:
        try:
            y, m = int(y_raw), int(m_raw)
            if 2000 <= y <= 2100 and 1 <= m <= 12:
                return f"{y:04d}-{m:02d}"
        except ValueError:
            pass
    return normalize_month("")


def resolve_list_month_filter(legacy_key, year_key, month_key):
    """Expense/income list month: legacy ?exp_month= or dropdown ?exp_cal_year=&exp_cal_month=."""
    m_raw = (request.args.get(month_key) or "").strip()
    if not m_raw:
        return parse_optional_month(request.args.get(legacy_key))
    y_raw = (request.args.get(year_key) or "").strip()
    if not y_raw:
        return parse_optional_month(request.args.get(legacy_key))
    try:
        formed = f"{int(y_raw):04d}-{int(m_raw):02d}"
        return parse_optional_month(formed)
    except ValueError:
        return parse_optional_month(request.args.get(legacy_key))


def parse_optional_month(value):
    """YYYY-MM for list filters, or None when absent / invalid."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parts = raw.split("-", 1)
        if len(parts) != 2:
            return None
        year_num, month_num = int(parts[0]), int(parts[1])
        if year_num < 2000 or year_num > 2100 or month_num < 1 or month_num > 12:
            return None
        return f"{year_num:04d}-{month_num:02d}"
    except ValueError:
        return None


def month_bounds_dates(ym_str):
    """Half-open calendar month as YYYY-MM-DD dates (start inclusive, end exclusive)."""
    ys, ms = ym_str.split("-", 1)
    month_start = datetime(int(ys), int(ms), 1)
    month_end = month_start + timedelta(days=32)
    month_end = month_end.replace(day=1)
    return (month_start.strftime("%Y-%m-%d"), month_end.strftime("%Y-%m-%d"))


def normalize_month(value):
    raw = (value or "").strip()
    if not raw:
        return datetime.now().strftime("%Y-%m")
    try:
        year_str, month_str = raw.split("-", 1)
        month_year = int(year_str)
        month_num = int(month_str)
        if month_num < 1 or month_num > 12:
            raise ValueError
        return f"{month_year:04d}-{month_num:02d}"
    except ValueError:
        return datetime.now().strftime("%Y-%m")


def normalize_settings_section(value):
    section = (value or "").strip().lower()
    return section if section in SETTINGS_SECTIONS else "general"


def coerce_txn_day(raw):
    """Normalize stored/form raw values to YYYY-MM-DD, or '' if empty."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        head = text[:10]
        try:
            datetime.strptime(head, "%Y-%m-%d")
            return head
        except ValueError:
            pass
    try:
        normalized = text.replace(" ", "T", 1)
        if len(normalized) == 10:
            normalized = f"{normalized}T00:00:00"
        parsed = datetime.fromisoformat(normalized)
        return parsed.date().isoformat()
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    return text[:10] if len(text) >= 10 else ""


def normalize_txn_day_from_form(raw):
    """Form submission: blank date defaults to today (calendar day only)."""
    return coerce_txn_day(raw) or datetime.now().date().isoformat()


@app.template_filter("txn_day")
def txn_day_filter(raw):
    return coerce_txn_day(raw)


def get_connection():
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"SQLite cannot open {DB_PATH!r}: {exc}. "
            "Prefer mounting a folder (not one file), especially if the host path is on SMB/NFS: "
            "volumes: ['./budget-data:/app/data'] and env DATABASE_PATH=/app/data/database.db"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def fetch_account_balances_through(conn, balance_cutoff_d, user_id):
    """Per-account balance with income, expenses, and transfers strictly before balance_cutoff_d (YYYY-MM-DD)."""
    uid = int(user_id)
    return conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.opening_balance
                + COALESCE(income_totals.total_income, 0)
                + COALESCE(expense_totals.total_expenses, 0)
                + COALESCE(transfer_in.t_in, 0)
                - COALESCE(transfer_out.t_out, 0) AS current_balance
        FROM accounts a
        LEFT JOIN (
            SELECT account_id, SUM(amount) AS total_income
            FROM income_entries
            WHERE user_id = ? AND date(received_at) < date(?)
            GROUP BY account_id
        ) income_totals ON income_totals.account_id = a.id
        LEFT JOIN (
            SELECT account_id, SUM(amount) AS total_expenses
            FROM expenses
            WHERE user_id = ? AND date(spent_at) < date(?)
            GROUP BY account_id
        ) expense_totals ON expense_totals.account_id = a.id
        LEFT JOIN (
            SELECT to_account_id AS account_id, SUM(amount) AS t_in
            FROM account_transfers
            WHERE user_id = ? AND date(transferred_at) < date(?)
            GROUP BY to_account_id
        ) transfer_in ON transfer_in.account_id = a.id
        LEFT JOIN (
            SELECT from_account_id AS account_id, SUM(amount) AS t_out
            FROM account_transfers
            WHERE user_id = ? AND date(transferred_at) < date(?)
            GROUP BY from_account_id
        ) transfer_out ON transfer_out.account_id = a.id
        WHERE a.user_id = ?
        ORDER BY a.name
        """,
        (
            uid,
            balance_cutoff_d,
            uid,
            balance_cutoff_d,
            uid,
            balance_cutoff_d,
            uid,
            balance_cutoff_d,
            uid,
        ),
    ).fetchall()


def monthly_total_balances_for_year(conn, year: int, today_d, user_id) -> list[float]:
    """Sum of all account balances after each calendar month (selected year's current month through today)."""
    balances = []
    for month_num in range(1, 13):
        month_start = datetime(year, month_num, 1)
        month_end_exclusive = month_start + timedelta(days=32)
        month_end_exclusive = month_end_exclusive.replace(day=1)
        month_end_cutoff = month_end_exclusive.strftime("%Y-%m-%d")
        if year == today_d.year and month_num == today_d.month:
            snap_cutoff = (today_d + timedelta(days=1)).isoformat()
        else:
            snap_cutoff = month_end_cutoff
        rows_m = fetch_account_balances_through(conn, snap_cutoff, user_id)
        balances.append(sum(float(r["current_balance"]) for r in rows_m))
    return balances


def balance_line_chart_spec(rows: list, *, width: float = 720, height: float = 300) -> dict:
    """SVG line chart: rows need total_balance, month_label, change."""
    ml, mr, mt, mb = 58.0, 18.0, 24.0, 46.0
    pw = width - ml - mr
    ph = height - mt - mb
    vals = [float(r["total_balance"]) for r in rows]
    month_label_y = height - 12.0
    if not vals:
        return {
            "w": width,
            "h": height,
            "points": "",
            "dots": [],
            "plot_ml": ml,
            "plot_mt": mt,
            "plot_pw": pw,
            "plot_ph": ph,
            "y_ticks": [],
            "month_label_y": month_label_y,
        }
    y_lo = min(vals)
    y_hi = max(vals)
    span = y_hi - y_lo
    if span <= 0:
        eps = max(abs(y_lo) * 0.02, 1.0) if y_lo != 0 else 1.0
        y_min = y_lo - eps
        y_max = y_hi + eps
    else:
        pad = span * 0.05
        y_min = y_lo - pad
        y_max = y_hi + pad
    y_rng = y_max - y_min or 1.0
    y_mid = y_min + y_rng / 2
    n = len(vals)
    dots = []
    pts = []
    for i, r in enumerate(rows):
        v = float(r["total_balance"])
        ch = float(r.get("change", 0.0))
        x = ml + (i / (n - 1)) * pw if n > 1 else ml + pw / 2
        yi = mt + ph - ((v - y_min) / y_rng) * ph
        pts.append(f"{x:.1f},{yi:.1f}")
        label = r["month_label"]
        dots.append(
            {
                "cx": round(x, 1),
                "cy": round(yi, 1),
                "lx": round(x, 1),
                "short": label[:3],
                "title": f"{label}: {v:.2f} (Δ {ch:+.2f})",
            }
        )
    y_ticks = [
        {"x": 4, "y": mt + 8, "text": f"{y_max:.2f}"},
        {"x": 4, "y": mt + ph / 2, "text": f"{y_mid:.2f}"},
        {"x": 4, "y": mt + ph - 4, "text": f"{y_min:.2f}"},
    ]
    return {
        "w": width,
        "h": height,
        "points": " ".join(pts),
        "dots": dots,
        "plot_ml": ml,
        "plot_mt": mt,
        "plot_pw": pw,
        "plot_ph": ph,
        "y_ticks": y_ticks,
        "month_label_y": month_label_y,
    }


def _column_names(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_schema(conn):
    columns = _column_names(conn, "expenses")
    if columns and "notes" not in columns and "item" in columns:
        try:
            conn.execute("ALTER TABLE expenses RENAME COLUMN item TO notes")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE expenses ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE expenses SET notes = item WHERE notes = '' OR notes IS NULL")

    columns = _column_names(conn, "expenses")
    if columns and "account_id" not in columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN account_id INTEGER REFERENCES accounts(id)")
        expense_account_id = conn.execute(
            "SELECT id FROM accounts WHERE name = 'Expense Cash' LIMIT 1"
        ).fetchone()
        if expense_account_id is None:
            conn.execute(
                "INSERT OR IGNORE INTO accounts(name, opening_balance) VALUES ('Expense Cash', 0)"
            )
            expense_account_id = conn.execute(
                "SELECT id FROM accounts WHERE name = 'Expense Cash' LIMIT 1"
            ).fetchone()
        fallback_account_id = expense_account_id["id"]
        conn.execute(
            "UPDATE expenses SET account_id = ? WHERE account_id IS NULL",
            (fallback_account_id,),
        )

    columns = _column_names(conn, "expenses")
    if columns and "spent_at" not in columns:
        conn.execute("ALTER TABLE expenses ADD COLUMN spent_at TIMESTAMP")
        conn.execute("UPDATE expenses SET spent_at = created_at WHERE spent_at IS NULL")

    columns = _column_names(conn, "income_entries")
    if columns and "notes" not in columns and "source" in columns:
        try:
            conn.execute("ALTER TABLE income_entries RENAME COLUMN source TO notes")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE income_entries ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE income_entries SET notes = source WHERE notes = '' OR notes IS NULL")

    columns = _column_names(conn, "income_entries")
    if columns and "category_id" not in columns:
        conn.execute("ALTER TABLE income_entries ADD COLUMN category_id INTEGER REFERENCES income_categories(id)")
        income_category_id = conn.execute(
            "SELECT id FROM income_categories WHERE name = 'General' LIMIT 1"
        ).fetchone()
        if income_category_id is None:
            conn.execute(
                "INSERT OR IGNORE INTO income_categories(name) VALUES ('General')"
            )
            income_category_id = conn.execute(
                "SELECT id FROM income_categories WHERE name = 'General' LIMIT 1"
            ).fetchone()
        fallback_category_id = income_category_id["id"]
        conn.execute(
            "UPDATE income_entries SET category_id = ? WHERE category_id IS NULL",
            (fallback_category_id,),
        )

    columns = _column_names(conn, "income_entries")
    if columns and "received_at" not in columns:
        conn.execute("ALTER TABLE income_entries ADD COLUMN received_at TIMESTAMP")
        conn.execute("UPDATE income_entries SET received_at = created_at WHERE received_at IS NULL")

    migrate_account_transfers(conn)
    migrate_expenses_signed_amounts(conn)
    migrate_txn_dates_to_day(conn)
    migrate_recurring_entries(conn)
    migrate_users_multitenancy(conn)


def migrate_users_multitenancy(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cat_cols = _column_names(conn, "categories") or []
    if "user_id" in cat_cols:
        return

    uid_row = conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
    if uid_row:
        uid = int(uid_row["id"])
    else:
        legacy_pw = os.environ.get("VB_LEGACY_ADMIN_PASSWORD", "changeme")
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("admin", generate_password_hash(legacy_pw)),
        )
        uid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

    conn.execute(
        """
        CREATE TABLE categories_new (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        )
        """
    )
    conn.execute(
        "INSERT INTO categories_new (id, user_id, name) SELECT id, ?, name FROM categories",
        (uid,),
    )
    conn.execute("DROP TABLE categories")
    conn.execute("ALTER TABLE categories_new RENAME TO categories")

    conn.execute(
        """
        CREATE TABLE income_categories_new (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        )
        """
    )
    conn.execute(
        "INSERT INTO income_categories_new (id, user_id, name) SELECT id, ?, name FROM income_categories",
        (uid,),
    )
    conn.execute("DROP TABLE income_categories")
    conn.execute("ALTER TABLE income_categories_new RENAME TO income_categories")

    conn.execute(
        """
        CREATE TABLE accounts_new (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            opening_balance REAL NOT NULL DEFAULT 0,
            UNIQUE(user_id, name)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO accounts_new (id, user_id, name, opening_balance)
        SELECT id, ?, name, opening_balance FROM accounts
        """,
        (uid,),
    )
    conn.execute("DROP TABLE accounts")
    conn.execute("ALTER TABLE accounts_new RENAME TO accounts")

    conn.execute(f"ALTER TABLE expenses ADD COLUMN user_id INTEGER NOT NULL DEFAULT {uid}")
    conn.execute(f"ALTER TABLE income_entries ADD COLUMN user_id INTEGER NOT NULL DEFAULT {uid}")
    conn.execute(f"ALTER TABLE account_transfers ADD COLUMN user_id INTEGER NOT NULL DEFAULT {uid}")
    conn.execute(f"ALTER TABLE recurring_entries ADD COLUMN user_id INTEGER NOT NULL DEFAULT {uid}")


def seed_user_defaults(conn, user_id):
    uid = int(user_id)
    conn.execute(
        "INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)",
        (uid, "General"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO income_categories (user_id, name) VALUES (?, ?)",
        (uid, "General"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO accounts (user_id, name, opening_balance) VALUES (?, ?, ?)",
        (uid, "Main", 0.0),
    )


def migrate_recurring_entries(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recurring_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL CHECK (entry_type IN ('expense', 'income')),
            amount REAL NOT NULL CHECK (amount > 0),
            category_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            day_of_month INTEGER NOT NULL CHECK (day_of_month >= 1 AND day_of_month <= 31),
            notes TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS recurring_applied (
            recurring_id INTEGER NOT NULL,
            ym TEXT NOT NULL,
            PRIMARY KEY (recurring_id, ym),
            FOREIGN KEY (recurring_id) REFERENCES recurring_entries(id) ON DELETE CASCADE
        )
        """
    )


def migrate_account_transfers(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_account_id INTEGER NOT NULL,
            to_account_id INTEGER NOT NULL,
            amount REAL NOT NULL CHECK (amount > 0),
            transferred_at TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (from_account_id) REFERENCES accounts(id),
            FOREIGN KEY (to_account_id) REFERENCES accounts(id),
            CHECK (from_account_id != to_account_id)
        )
        """
    )


def migrate_txn_dates_to_day(conn):
    """Store movement dates as calendar days only (YYYY-MM-DD)."""
    conn.execute(
        """
        UPDATE expenses
        SET spent_at = date(spent_at)
        WHERE spent_at IS NOT NULL AND trim(spent_at) != '' AND date(spent_at) IS NOT NULL
        """
    )
    conn.execute(
        """
        UPDATE income_entries
        SET received_at = date(received_at)
        WHERE received_at IS NOT NULL AND trim(received_at) != '' AND date(received_at) IS NOT NULL
        """
    )


def migrate_expenses_signed_amounts(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='expenses'"
    ).fetchone()
    if not row or not row["sql"]:
        return
    create_sql = row["sql"]
    if not re.search(r"CHECK\s*\(\s*amount\s*>=\s*0\s*\)", create_sql, re.I):
        return
    cols = _column_names(conn, "expenses") or []
    has_uid = "user_id" in cols
    if has_uid:
        conn.executescript(
            """
            CREATE TABLE expenses_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL,
                category_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                spent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories(id),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            INSERT INTO expenses_new (id, user_id, notes, amount, category_id, account_id, spent_at, created_at)
            SELECT id, user_id, notes, -ABS(amount), category_id, account_id, spent_at, created_at FROM expenses;
            DROP TABLE expenses;
            ALTER TABLE expenses_new RENAME TO expenses;
            """
        )
    else:
        conn.executescript(
            """
            CREATE TABLE expenses_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notes TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL,
                category_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                spent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories(id),
                FOREIGN KEY (account_id) REFERENCES accounts(id)
            );
            INSERT INTO expenses_new (id, notes, amount, category_id, account_id, spent_at, created_at)
            SELECT id, notes, -ABS(amount), category_id, account_id, spent_at, created_at FROM expenses;
            DROP TABLE expenses;
            ALTER TABLE expenses_new RENAME TO expenses;
            """
        )


def init_db():
    print(f"[vibe-budgeting] SQLite database path: {DB_PATH!r}", file=sys.stderr)
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS income_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            opening_balance REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notes TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL,
            category_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            spent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS income_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notes TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL CHECK (amount >= 0),
            category_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES income_categories(id),
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );
        """
    )

    conn.execute("INSERT OR IGNORE INTO categories(name) VALUES ('General')")
    conn.execute("INSERT OR IGNORE INTO income_categories(name) VALUES ('General')")
    conn.execute("INSERT OR IGNORE INTO accounts(name, opening_balance) VALUES ('Main', 0)")
    migrate_schema(conn)
    conn.commit()
    conn.close()


def resolve_active_panel():
    panel = request.args.get("panel", "").strip()
    if panel in ALLOWED_PANELS:
        return panel

    next_panel = request.form.get("next_panel", "").strip()
    if next_panel in ALLOWED_PANELS:
        return next_panel

    referer = request.headers.get("Referer", "")
    if referer:
        path = urlparse(referer).path.rstrip("/") or "/"
        mapping = {
            "/categories/add": "settings",
            "/expenses/add": "expenses",
            "/income/add": "income",
            "/transfers/add": "transfer",
            "/import/excel": "settings",
        }
        if path in mapping:
            return mapping[path]

        for prefix, mapped in (
            ("/categories/", "settings"),
            ("/income-categories/", "settings"),
            ("/accounts/", "settings"),
            ("/expenses/", "expenses"),
            ("/income/", "income"),
            ("/transfers/", "transfer"),
            ("/recurring/", "recurring"),
        ):
            if path.startswith(prefix):
                return mapped

    return "home"


def redirect_home(panel=None, settings_section=None):
    target = panel if panel in ALLOWED_PANELS else resolve_active_panel()
    month = normalize_month(request.form.get("month") or request.args.get("month"))
    year_for_redirect = normalize_year(request.form.get("year") or request.args.get("year"))
    raw_section = (
        settings_section
        if settings_section is not None
        else (request.form.get("settings_section") or request.args.get("settings_section"))
    )
    sec = normalize_settings_section(raw_section)
    query = {"panel": target, "month": month}
    if target == "settings":
        query["settings_section"] = sec
    if target == "yearly":
        query["year"] = year_for_redirect
    report_year_redirect = normalize_year(
        request.form.get("report_year") or request.args.get("report_year")
    )
    if target == "reports":
        query["report_year"] = report_year_redirect
    exp_pg = normalize_list_page(request.form.get("exp_page") or request.args.get("exp_page") or 1)
    inc_pg = normalize_list_page(request.form.get("inc_page") or request.args.get("inc_page") or 1)
    if target == "expenses" and exp_pg > 1:
        query["exp_page"] = exp_pg
    if target == "income" and inc_pg > 1:
        query["inc_page"] = inc_pg
    exp_fm = parse_optional_month(request.form.get("exp_month") or request.args.get("exp_month"))
    inc_fm = parse_optional_month(request.form.get("inc_month") or request.args.get("inc_month"))
    if target == "expenses" and exp_fm:
        query["exp_month"] = exp_fm
    if target == "income" and inc_fm:
        query["inc_month"] = inc_fm
    return redirect(url_for("index", **query))


def _normalize_header_key(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _sheet_as_dicts(ws):
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers_raw = [_normalize_header_key(cell) for cell in rows[0]]
    headers = []
    for raw in headers_raw:
        headers.append(raw if raw else "")
    out = []
    for row in rows[1:]:
        if row is None:
            continue
        cells = list(row)
        if not cells:
            continue
        if all(cell is None or str(cell).strip() == "" for cell in cells):
            continue
        row_dict = {}
        for idx, header in enumerate(headers):
            if not header:
                continue
            row_dict[header] = cells[idx] if idx < len(cells) else None
        out.append(row_dict)
    return out


def _parse_excel_expense_amount(value, sheet, row_num):
    """Excel import: sign is kept. Negative = spending; positive = refund/credit (adds back to balance)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{sheet} row {row_num}: missing amount")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{sheet} row {row_num}: invalid amount") from exc
    return amount


def _parse_excel_income_amount(value, sheet, row_num):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{sheet} row {row_num}: missing amount")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{sheet} row {row_num}: invalid amount") from exc
    return abs(amount)


def _parse_excel_datetime(value, sheet, row_num, column_label):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{sheet} row {row_num}: missing {column_label}")
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    text = str(value).strip()
    try:
        normalized = text.replace(" ", "T", 1)
        if len(normalized) == 10:
            normalized = f"{normalized}T00:00:00"
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"{sheet} row {row_num}: invalid {column_label}")
    return parsed.replace(microsecond=0)


def _parse_excel_timestamp(value, sheet, row_num, column_label):
    return _parse_excel_datetime(value, sheet, row_num, column_label).isoformat(timespec="seconds")


def _parse_excel_movement_date(value, sheet, row_num, column_label):
    return _parse_excel_datetime(value, sheet, row_num, column_label).date().isoformat()


def _optional_created_at(value, sheet, row_num):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_excel_timestamp(value, sheet, row_num, "created_at")


def _build_export_workbook(conn, user_id):
    uid = int(user_id)
    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = SHEET_META
    ws_meta.append(["key", "value"])
    ws_meta.append(["format_version", EXPORT_FORMAT_VERSION])
    ws_meta.append(["exported_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")])

    ws_accounts = wb.create_sheet(SHEET_ACCOUNTS)
    ws_accounts.append(["name", "opening_balance"])
    for row in conn.execute(
        "SELECT name, opening_balance FROM accounts WHERE user_id = ? ORDER BY name",
        (uid,),
    ):
        ws_accounts.append([row["name"], row["opening_balance"]])

    ws_ec = wb.create_sheet(SHEET_EXPENSE_CATEGORIES)
    ws_ec.append(["name"])
    for row in conn.execute(
        "SELECT name FROM categories WHERE user_id = ? ORDER BY name",
        (uid,),
    ):
        ws_ec.append([row["name"]])

    ws_ic = wb.create_sheet(SHEET_INCOME_CATEGORIES)
    ws_ic.append(["name"])
    for row in conn.execute(
        "SELECT name FROM income_categories WHERE user_id = ? ORDER BY name",
        (uid,),
    ):
        ws_ic.append([row["name"]])

    ws_exp = wb.create_sheet(SHEET_EXPENSES)
    ws_exp.append(["notes", "amount", "category_name", "account_name", "spent_at", "created_at"])
    for row in conn.execute(
        """
        SELECT e.notes, e.amount, c.name AS category_name, a.name AS account_name, e.spent_at, e.created_at
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        JOIN accounts a ON a.id = e.account_id
        WHERE e.user_id = ?
        ORDER BY e.spent_at ASC, e.id ASC
        """,
        (uid,),
    ):
        ws_exp.append(
            [
                row["notes"],
                row["amount"],
                row["category_name"],
                row["account_name"],
                row["spent_at"],
                row["created_at"],
            ]
        )

    ws_inc = wb.create_sheet(SHEET_INCOME)
    ws_inc.append(["notes", "amount", "category_name", "account_name", "received_at", "created_at"])
    for row in conn.execute(
        """
        SELECT i.notes, i.amount, c.name AS category_name, a.name AS account_name, i.received_at, i.created_at
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        JOIN accounts a ON a.id = i.account_id
        WHERE i.user_id = ?
        ORDER BY i.received_at ASC, i.id ASC
        """,
        (uid,),
    ):
        ws_inc.append(
            [
                row["notes"],
                row["amount"],
                row["category_name"],
                row["account_name"],
                row["received_at"],
                row["created_at"],
            ]
        )

    return wb


def _build_migration_template_workbook():
    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = SHEET_META
    ws_meta.append(["key", "value"])
    ws_meta.append(["format_version", EXPORT_FORMAT_VERSION])
    ws_meta.append(["kind", "migration_template"])

    ws_accounts = wb.create_sheet(SHEET_ACCOUNTS)
    ws_accounts.append(["name", "opening_balance"])
    ws_accounts.append(["Main", 0])

    ws_ec = wb.create_sheet(SHEET_EXPENSE_CATEGORIES)
    ws_ec.append(["name"])
    ws_ec.append(["General"])

    ws_ic = wb.create_sheet(SHEET_INCOME_CATEGORIES)
    ws_ic.append(["name"])
    ws_ic.append(["General"])

    ws_exp = wb.create_sheet(SHEET_EXPENSES)
    ws_exp.append(["notes", "amount", "category_name", "account_name", "spent_at", "created_at"])

    ws_inc = wb.create_sheet(SHEET_INCOME)
    ws_inc.append(["notes", "amount", "category_name", "account_name", "received_at", "created_at"])

    return wb


def _lookup_category_id(conn, name, user_id, expense=True):
    uid = int(user_id)
    table = "categories" if expense else "income_categories"
    row = conn.execute(
        f"SELECT id FROM {table} WHERE user_id = ? AND name = ?",
        (uid, name.strip()),
    ).fetchone()
    return row["id"] if row else None


def _lookup_account_id(conn, name, user_id):
    uid = int(user_id)
    row = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? AND name = ?",
        (uid, name.strip()),
    ).fetchone()
    return row["id"] if row else None


def _safe_next_url(raw):
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.startswith("/") or text.startswith("//"):
        return None
    return text


def _collect_import_movements(expense_rows, income_rows):
    errors = []
    insert_expenses = []
    for idx, row in enumerate(expense_rows, start=2):
        notes_val = row.get("notes")
        notes = "" if notes_val is None else str(notes_val)
        try:
            amount = _parse_excel_expense_amount(row.get("amount"), SHEET_EXPENSES, idx)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        cat_name = row.get("category_name")
        acc_name = row.get("account_name")
        if cat_name is None or not str(cat_name).strip():
            errors.append(f"{SHEET_EXPENSES} row {idx}: missing category_name")
            continue
        if acc_name is None or not str(acc_name).strip():
            errors.append(f"{SHEET_EXPENSES} row {idx}: missing account_name")
            continue
        cat_name = str(cat_name).strip()
        acc_name = str(acc_name).strip()
        try:
            spent_at = _parse_excel_movement_date(row.get("spent_at"), SHEET_EXPENSES, idx, "spent_at")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        created_at = None
        try:
            if row.get("created_at") not in (None, ""):
                created_at = _optional_created_at(row.get("created_at"), SHEET_EXPENSES, idx)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        insert_expenses.append((notes, amount, cat_name, acc_name, spent_at, created_at))

    insert_income = []
    for idx, row in enumerate(income_rows, start=2):
        notes_val = row.get("notes")
        notes = "" if notes_val is None else str(notes_val)
        try:
            amount = _parse_excel_income_amount(row.get("amount"), SHEET_INCOME, idx)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        cat_name = row.get("category_name")
        acc_name = row.get("account_name")
        if cat_name is None or not str(cat_name).strip():
            errors.append(f"{SHEET_INCOME} row {idx}: missing category_name")
            continue
        if acc_name is None or not str(acc_name).strip():
            errors.append(f"{SHEET_INCOME} row {idx}: missing account_name")
            continue
        cat_name = str(cat_name).strip()
        acc_name = str(acc_name).strip()
        try:
            received_at = _parse_excel_movement_date(
                row.get("received_at"), SHEET_INCOME, idx, "received_at"
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue
        created_at = None
        try:
            if row.get("created_at") not in (None, ""):
                created_at = _optional_created_at(row.get("created_at"), SHEET_INCOME, idx)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        insert_income.append((notes, amount, cat_name, acc_name, received_at, created_at))

    return insert_expenses, insert_income, errors


def _run_import_workbook(wb, replace_movements, sync_opening_balances, user_id):
    errors = []
    required_sheets = {
        SHEET_ACCOUNTS,
        SHEET_EXPENSE_CATEGORIES,
        SHEET_INCOME_CATEGORIES,
        SHEET_EXPENSES,
        SHEET_INCOME,
    }
    missing = [name for name in required_sheets if name not in wb.sheetnames]
    if missing:
        return [f"Missing sheets: {', '.join(missing)}"]

    accounts_rows = _sheet_as_dicts(wb[SHEET_ACCOUNTS])
    expense_cat_rows = _sheet_as_dicts(wb[SHEET_EXPENSE_CATEGORIES])
    income_cat_rows = _sheet_as_dicts(wb[SHEET_INCOME_CATEGORIES])
    expense_rows = _sheet_as_dicts(wb[SHEET_EXPENSES])
    income_rows = _sheet_as_dicts(wb[SHEET_INCOME])

    insert_expenses, insert_income, parse_errors = _collect_import_movements(
        expense_rows, income_rows
    )
    if parse_errors:
        return parse_errors

    expense_cats_from_movements = {row[2] for row in insert_expenses}
    income_cats_from_movements = {row[2] for row in insert_income}
    accounts_from_movements = {row[3] for row in insert_expenses} | {
        row[3] for row in insert_income
    }

    conn = get_connection()
    uid = int(user_id)
    try:
        conn.execute("BEGIN")

        for idx, row in enumerate(accounts_rows, start=2):
            name = row.get("name")
            if name is None or not str(name).strip():
                errors.append(f"{SHEET_ACCOUNTS} row {idx}: missing name")
                continue
            name = str(name).strip()
            opening_raw = row.get("opening_balance")
            if opening_raw is None or str(opening_raw).strip() == "":
                opening_balance = 0.0
            else:
                try:
                    opening_balance = float(opening_raw)
                except (TypeError, ValueError):
                    errors.append(f"{SHEET_ACCOUNTS} row {idx}: invalid opening_balance")
                    continue
            conn.execute(
                "INSERT OR IGNORE INTO accounts(user_id, name, opening_balance) VALUES (?, ?, ?)",
                (uid, name, opening_balance),
            )
            if sync_opening_balances:
                conn.execute(
                    "UPDATE accounts SET opening_balance = ? WHERE user_id = ? AND name = ?",
                    (opening_balance, uid, name),
                )

        if errors:
            conn.rollback()
            return errors

        for idx, row in enumerate(expense_cat_rows, start=2):
            name = row.get("name")
            if name is None or not str(name).strip():
                continue
            conn.execute(
                "INSERT OR IGNORE INTO categories(user_id, name) VALUES (?, ?)",
                (uid, str(name).strip()),
            )

        for name in sorted(expense_cats_from_movements):
            conn.execute(
                "INSERT OR IGNORE INTO categories(user_id, name) VALUES (?, ?)",
                (uid, name),
            )

        for idx, row in enumerate(income_cat_rows, start=2):
            name = row.get("name")
            if name is None or not str(name).strip():
                continue
            conn.execute(
                "INSERT OR IGNORE INTO income_categories(user_id, name) VALUES (?, ?)",
                (uid, str(name).strip()),
            )

        for name in sorted(income_cats_from_movements):
            conn.execute(
                "INSERT OR IGNORE INTO income_categories(user_id, name) VALUES (?, ?)",
                (uid, name),
            )

        for acc_name in sorted(accounts_from_movements):
            conn.execute(
                "INSERT OR IGNORE INTO accounts(user_id, name, opening_balance) VALUES (?, ?, ?)",
                (uid, acc_name, 0.0),
            )

        if replace_movements:
            conn.execute("DELETE FROM expenses WHERE user_id = ?", (uid,))
            conn.execute("DELETE FROM income_entries WHERE user_id = ?", (uid,))

        for notes, amount, cat_name, acc_name, spent_at, created_at in insert_expenses:
            category_id = _lookup_category_id(conn, cat_name, uid, expense=True)
            account_id = _lookup_account_id(conn, acc_name, uid)
            if category_id is None:
                errors.append(f"{SHEET_EXPENSES}: unknown expense category {cat_name!r}")
            if account_id is None:
                errors.append(f"{SHEET_EXPENSES}: unknown account {acc_name!r}")

        for notes, amount, cat_name, acc_name, received_at, created_at in insert_income:
            category_id = _lookup_category_id(conn, cat_name, uid, expense=False)
            account_id = _lookup_account_id(conn, acc_name, uid)
            if category_id is None:
                errors.append(f"{SHEET_INCOME}: unknown income category {cat_name!r}")
            if account_id is None:
                errors.append(f"{SHEET_INCOME}: unknown account {acc_name!r}")

        if errors:
            conn.rollback()
            return errors

        for notes, amount, cat_name, acc_name, spent_at, created_at in insert_expenses:
            category_id = _lookup_category_id(conn, cat_name, uid, expense=True)
            account_id = _lookup_account_id(conn, acc_name, uid)
            if created_at:
                conn.execute(
                    """
                    INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (uid, notes, amount, category_id, account_id, spent_at, created_at),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (uid, notes, amount, category_id, account_id, spent_at),
                )

        for notes, amount, cat_name, acc_name, received_at, created_at in insert_income:
            category_id = _lookup_category_id(conn, cat_name, uid, expense=False)
            account_id = _lookup_account_id(conn, acc_name, uid)
            if created_at:
                conn.execute(
                    """
                    INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (uid, notes, amount, category_id, account_id, received_at, created_at),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (uid, notes, amount, category_id, account_id, received_at),
                )

        conn.commit()
        return []
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        nxt = _safe_next_url(request.args.get("next"))
        return redirect(nxt or url_for("index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        finally:
            conn.close()
        if row and check_password_hash(row["password_hash"], password):
            session["user_id"] = row["id"]
            session["username"] = username
            nxt = _safe_next_url(request.form.get("next")) or _safe_next_url(request.args.get("next"))
            return redirect(nxt or url_for("index"))
        flash("Invalid username or password.", "error")
    return render_template("login.html", next=request.args.get("next") or "", allow_registration=ALLOW_REGISTRATION)


@app.route("/register", methods=["GET", "POST"])
def register():
    if not ALLOW_REGISTRATION:
        flash("Registration is disabled.", "error")
        return redirect(url_for("login"))
    if session.get("user_id"):
        return redirect(url_for("index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""
        if not _USERNAME_RE.fullmatch(username):
            flash("Username must be 3–32 characters (letters, digits, . _ -).", "error")
            return render_template("register.html")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("register.html")
        if password != password2:
            flash("Passwords do not match.", "error")
            return render_template("register.html")
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password)),
            )
            uid = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            seed_user_defaults(conn, uid)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.close()
            flash("That username is already taken.", "error")
            return render_template("register.html")
        conn.close()
        session["user_id"] = uid
        session["username"] = username
        flash("Account created.", "success")
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    session.pop("username", None)
    return redirect(url_for("login"))


@app.route("/export/excel")
def export_excel():
    conn = get_connection()
    try:
        wb = _build_export_workbook(conn, g.user_id)
    finally:
        conn.close()

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"budget-export-{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )


@app.route("/export/migration-template")
def export_migration_template():
    wb = _build_migration_template_workbook()
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="budget-migration-template.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        max_age=0,
    )


@app.route("/import/excel", methods=["POST"])
def import_excel():
    upload = request.files.get("file")
    if not upload or upload.filename.strip() == "":
        flash("Choose an Excel file (.xlsx).", "error")
        return redirect_home(panel="settings", settings_section="migration")

    replace_movements = request.form.get("replace_movements") == "1"
    sync_opening_balances = request.form.get("sync_opening_balances") == "1"

    raw = upload.read()
    if not raw:
        flash("The uploaded file is empty.", "error")
        return redirect_home(panel="settings", settings_section="migration")

    try:
        workbook = load_workbook(BytesIO(raw), data_only=True)
    except Exception as exc:
        flash(f"Could not read the Excel file: {exc}", "error")
        return redirect_home(panel="settings", settings_section="migration")

    errors = _run_import_workbook(workbook, replace_movements, sync_opening_balances, g.user_id)
    if errors:
        preview = "; ".join(errors[:8])
        extra = f" (+{len(errors) - 8} more)" if len(errors) > 8 else ""
        flash(f"Import failed. {preview}{extra}", "error")
        return redirect_home(panel="settings", settings_section="migration")

    flash(
        "Import completed. Accounts/categories from the file were merged (new names added)."
        + (
            " Existing expense and income rows were replaced by the file."
            if replace_movements
            else " Movement rows from the file were added (existing rows kept)."
        ),
        "success",
    )
    return redirect_home(panel="settings", settings_section="migration")


@app.route("/")
def index():
    conn = get_connection()
    uid = g.user_id

    posted_recurring = apply_recurring_entries(conn, uid)
    if posted_recurring > 0:
        conn.commit()
        flash(
            f"Posted {posted_recurring} recurring entr{'ies' if posted_recurring != 1 else 'y'}.",
            "success",
        )

    raw_panel = request.args.get("panel", "").strip()
    if raw_panel == "export":
        active_panel = "settings"
        section_from_legacy = "export"
    elif raw_panel == "migration":
        active_panel = "settings"
        section_from_legacy = "migration"
    elif raw_panel in ALLOWED_PANELS:
        active_panel = raw_panel
        section_from_legacy = None
    else:
        active_panel = "home"
        section_from_legacy = None

    month_filter = resolve_month_filter_from_request()
    year_filter = normalize_year(request.args.get("year"))
    report_year = normalize_year(request.args.get("report_year"))
    year_str, month_str = month_filter.split("-", 1)
    month_start = datetime(int(year_str), int(month_str), 1)
    month_end = month_start + timedelta(days=32)
    month_end = month_end.replace(day=1)
    month_start_d = month_start.strftime("%Y-%m-%d")
    month_end_d = month_end.strftime("%Y-%m-%d")
    month_heading = f"{calendar.month_name[int(month_str)]} {year_str}"
    settings_section = normalize_settings_section(
        section_from_legacy or request.args.get("settings_section")
    )

    categories = conn.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY name",
        (uid,),
    ).fetchall()
    income_categories = conn.execute(
        "SELECT id, name FROM income_categories WHERE user_id = ? ORDER BY name",
        (uid,),
    ).fetchall()

    recurring_entries = conn.execute(
        """
        SELECT
            r.id,
            r.entry_type,
            r.amount,
            r.category_id,
            r.account_id,
            r.day_of_month,
            r.notes,
            r.enabled,
            r.created_at,
            a.name AS account_name,
            COALESCE(c.name, ic.name) AS category_name
        FROM recurring_entries r
        JOIN accounts a ON a.id = r.account_id
        LEFT JOIN categories c ON r.entry_type = 'expense' AND c.id = r.category_id
        LEFT JOIN income_categories ic ON r.entry_type = 'income' AND ic.id = r.category_id
        WHERE r.user_id = ?
        ORDER BY r.day_of_month, r.id
        """,
        (uid,),
    ).fetchall()

    expense_filter_month = resolve_list_month_filter("exp_month", "exp_cal_year", "exp_cal_month")
    expense_where = "WHERE e.user_id = ?"
    expense_where_params = [uid]
    if expense_filter_month:
        eb = month_bounds_dates(expense_filter_month)
        expense_where += " AND date(e.spent_at) >= date(?) AND date(e.spent_at) < date(?)"
        expense_where_params.extend([eb[0], eb[1]])

    expense_total = conn.execute(
        f"SELECT COUNT(*) AS n FROM expenses e {expense_where}",
        expense_where_params,
    ).fetchone()["n"]
    expense_num_pages = max(1, (expense_total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    expense_page = min(normalize_list_page(request.args.get("exp_page")), expense_num_pages)
    expense_offset = (expense_page - 1) * LIST_PAGE_SIZE
    expenses = conn.execute(
        f"""
        SELECT
            e.id,
            e.category_id,
            e.account_id,
            e.notes,
            e.amount,
            e.spent_at,
            e.created_at,
            c.name AS category_name,
            a.name AS account_name
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        JOIN accounts a ON a.id = e.account_id
        {expense_where}
        ORDER BY e.spent_at DESC
        LIMIT ? OFFSET ?
        """,
        (*expense_where_params, LIST_PAGE_SIZE, expense_offset),
    ).fetchall()

    accounts = conn.execute(
        "SELECT id, name, opening_balance FROM accounts WHERE user_id = ? ORDER BY name",
        (uid,),
    ).fetchall()

    income_filter_month = resolve_list_month_filter("inc_month", "inc_cal_year", "inc_cal_month")
    income_where = "WHERE i.user_id = ?"
    income_where_params = [uid]
    if income_filter_month:
        ib = month_bounds_dates(income_filter_month)
        income_where += " AND date(i.received_at) >= date(?) AND date(i.received_at) < date(?)"
        income_where_params.extend([ib[0], ib[1]])

    income_total = conn.execute(
        f"SELECT COUNT(*) AS n FROM income_entries i {income_where}",
        income_where_params,
    ).fetchone()["n"]
    income_num_pages = max(1, (income_total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    income_page = min(normalize_list_page(request.args.get("inc_page")), income_num_pages)
    income_offset = (income_page - 1) * LIST_PAGE_SIZE
    income_entries = conn.execute(
        f"""
        SELECT
            i.id,
            i.category_id,
            i.account_id,
            i.notes,
            i.amount,
            i.received_at,
            i.created_at,
            c.name AS category_name,
            a.name AS account_name
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        JOIN accounts a ON a.id = i.account_id
        {income_where}
        ORDER BY i.received_at DESC
        LIMIT ? OFFSET ?
        """,
        (*income_where_params, LIST_PAGE_SIZE, income_offset),
    ).fetchall()

    total_expenses = conn.execute(
        """
        SELECT COALESCE(-SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ? AND date(spent_at) >= date(?) AND date(spent_at) < date(?)
        """,
        (uid, month_start_d, month_end_d),
    ).fetchone()["total"]
    total_income = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM income_entries
        WHERE user_id = ? AND date(received_at) >= date(?) AND date(received_at) < date(?)
        """,
        (uid, month_start_d, month_end_d),
    ).fetchone()["total"]

    expense_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, COALESCE(-SUM(e.amount), 0) AS total_amount
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.user_id = ? AND date(e.spent_at) >= date(?) AND date(e.spent_at) < date(?)
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (uid, month_start_d, month_end_d),
    ).fetchall()

    income_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, SUM(i.amount) AS total_amount
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        WHERE i.user_id = ? AND date(i.received_at) >= date(?) AND date(i.received_at) < date(?)
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (uid, month_start_d, month_end_d),
    ).fetchall()

    today_d = datetime.now().date()
    sel_y, sel_m = int(year_str), int(month_str)
    if (today_d.year, today_d.month) == (sel_y, sel_m):
        balance_cutoff_d = (today_d + timedelta(days=1)).isoformat()
        balance_scope_hint = (
            "Account balances use opening balance plus movements through today "
            "(only while this month is selected)."
        )
    else:
        balance_cutoff_d = month_end_d
        balance_scope_hint = (
            f"Account balances are through the end of {month_heading} "
            "(opening balance plus movements up to then)."
        )

    account_balances = fetch_account_balances_through(conn, balance_cutoff_d, uid)
    account_balance_by_id = {
        int(row["id"]): float(row["current_balance"]) for row in account_balances
    }
    account_transfers = conn.execute(
        """
        SELECT
            t.id,
            t.amount,
            t.transferred_at,
            t.notes,
            fa.name AS from_account_name,
            ta.name AS to_account_name
        FROM account_transfers t
        JOIN accounts fa ON fa.id = t.from_account_id
        JOIN accounts ta ON ta.id = t.to_account_id
        WHERE t.user_id = ?
        ORDER BY date(t.transferred_at) DESC, t.id DESC
        LIMIT ?
        """,
        (uid, TRANSFER_LOG_LIMIT),
    ).fetchall()
    accounts_total_balance = sum(float(r["current_balance"]) for r in account_balances)

    year_start_d = f"{year_filter:04d}-01-01"
    year_end_d = f"{year_filter + 1:04d}-01-01"
    expense_by_month = {
        row["m"]: float(row["total"])
        for row in conn.execute(
            """
            SELECT CAST(strftime('%m', spent_at) AS INTEGER) AS m,
                   COALESCE(-SUM(amount), 0) AS total
            FROM expenses
            WHERE user_id = ? AND date(spent_at) >= date(?) AND date(spent_at) < date(?)
            GROUP BY m
            """,
            (uid, year_start_d, year_end_d),
        )
    }
    income_by_month = {
        row["m"]: float(row["total"])
        for row in conn.execute(
            """
            SELECT CAST(strftime('%m', received_at) AS INTEGER) AS m,
                   COALESCE(SUM(amount), 0) AS total
            FROM income_entries
            WHERE user_id = ? AND date(received_at) >= date(?) AND date(received_at) < date(?)
            GROUP BY m
            """,
            (uid, year_start_d, year_end_d),
        )
    }

    month_names = calendar.month_name
    yearly_month_balances = monthly_total_balances_for_year(conn, year_filter, today_d, uid)
    yearly_rows = []
    yearly_total_income = 0.0
    yearly_total_expenses = 0.0
    for month_num in range(1, 13):
        inc = income_by_month.get(month_num, 0.0)
        exp = expense_by_month.get(month_num, 0.0)
        delta = inc - exp
        yearly_total_income += inc
        yearly_total_expenses += exp
        yearly_rows.append(
            {
                "month_label": month_names[month_num],
                "income": inc,
                "expenses": exp,
                "delta": delta,
                "total_balance": yearly_month_balances[month_num - 1],
            }
        )
    yearly_total_delta = yearly_total_income - yearly_total_expenses
    transfer_default_date = datetime.now().date().isoformat()

    jan1_this_year = f"{report_year:04d}-01-01"
    rows_before_jan = fetch_account_balances_through(conn, jan1_this_year, uid)
    prev_balance_total = sum(float(r["current_balance"]) for r in rows_before_jan)
    report_balances_list = monthly_total_balances_for_year(conn, report_year, today_d, uid)
    report_balance_rows = []
    for month_num in range(1, 13):
        total_bal = report_balances_list[month_num - 1]
        change = total_bal - prev_balance_total
        prev_balance_total = total_bal
        report_balance_rows.append(
            {
                "month_label": calendar.month_name[month_num],
                "month_num": month_num,
                "total_balance": total_bal,
                "change": change,
            }
        )
    report_chart_spec = balance_line_chart_spec(report_balance_rows)

    conn.close()

    return render_template(
        "index.html",
        session_username=g.username,
        categories=categories,
        income_categories=income_categories,
        expenses=expenses,
        accounts=accounts,
        income_entries=income_entries,
        active_panel=active_panel,
        month_filter=month_filter,
        year_filter=year_filter,
        settings_section=settings_section,
        total_expenses=total_expenses,
        total_income=total_income,
        net_balance=total_income - total_expenses,
        accounts_total_balance=accounts_total_balance,
        expense_breakdown=expense_breakdown,
        income_breakdown=income_breakdown,
        account_balances=account_balances,
        account_balance_by_id=account_balance_by_id,
        account_transfers=account_transfers,
        yearly_rows=yearly_rows,
        yearly_total_income=yearly_total_income,
        yearly_total_expenses=yearly_total_expenses,
        yearly_total_delta=yearly_total_delta,
        expense_page=expense_page,
        expense_total=expense_total,
        expense_num_pages=expense_num_pages,
        income_page=income_page,
        income_total=income_total,
        income_num_pages=income_num_pages,
        list_page_size=LIST_PAGE_SIZE,
        expense_filter_month=expense_filter_month,
        income_filter_month=income_filter_month,
        month_heading=month_heading,
        balance_scope_hint=balance_scope_hint,
        cal=calendar,
        transfer_default_date=transfer_default_date,
        transfer_log_limit=TRANSFER_LOG_LIMIT,
        report_year=report_year,
        report_chart_spec=report_chart_spec,
        recurring_entries=recurring_entries,
    )


def _parse_category_choice(raw):
    """Return ('expense'|'income', category_id) or (None, None)."""
    s = (raw or "").strip()
    if s.startswith("e-"):
        try:
            return "expense", int(s[2:])
        except ValueError:
            return None, None
    if s.startswith("i-"):
        try:
            return "income", int(s[2:])
        except ValueError:
            return None, None
    return None, None


def _recurring_category_ok(conn, entry_type, category_id, user_id):
    uid = int(user_id)
    if entry_type == "expense":
        return conn.execute(
            "SELECT 1 FROM categories WHERE id = ? AND user_id = ?",
            (category_id, uid),
        ).fetchone()
    if entry_type == "income":
        return conn.execute(
            "SELECT 1 FROM income_categories WHERE id = ? AND user_id = ?",
            (category_id, uid),
        ).fetchone()
    return None


@app.route("/recurring/add", methods=["POST"])
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

    if amt <= 0:
        conn.close()
        flash("Amount must be positive.", "error")
        return redirect_home(panel="recurring")

    conn.execute(
        """
        INSERT INTO recurring_entries (user_id, entry_type, amount, category_id, account_id, day_of_month, notes, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (uid, entry_type, amt, category_id, account_id, dom, notes),
    )
    conn.commit()
    conn.close()
    flash("Recurring rule added.", "success")
    return redirect_home(panel="recurring")


@app.route("/recurring/<int:recurring_id>/edit", methods=["POST"])
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

    if amt <= 0:
        conn.close()
        flash("Amount must be positive.", "error")
        return redirect_home(panel="recurring")

    cur = conn.execute(
        """
        UPDATE recurring_entries
        SET entry_type = ?, amount = ?, category_id = ?, account_id = ?, day_of_month = ?, notes = ?, enabled = ?
        WHERE id = ? AND user_id = ?
        """,
        (entry_type, amt, category_id, account_id, dom, notes, enabled, recurring_id, uid),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        flash("Rule not found.", "error")
    else:
        flash("Recurring rule updated.", "success")
    return redirect_home(panel="recurring")


@app.route("/recurring/<int:recurring_id>/delete", methods=["POST"])
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


@app.route("/categories/add", methods=["POST"])
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


@app.route("/categories/<int:category_id>/edit", methods=["POST"])
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


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM categories WHERE id = ? AND user_id = ?",
        (category_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[vibe-budgeting] refused category delete {category_id} (still referenced or missing)")
    return redirect_home()


@app.route("/income-categories/add", methods=["POST"])
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


@app.route("/income-categories/<int:category_id>/edit", methods=["POST"])
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


@app.route("/income-categories/<int:category_id>/delete", methods=["POST"])
def delete_income_category(category_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM income_categories WHERE id = ? AND user_id = ?",
        (category_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[vibe-budgeting] refused income category delete {category_id} (still referenced or missing)")
    return redirect_home()


@app.route("/expenses/add", methods=["POST"])
def add_expense():
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    category_id = request.form.get("category_id", "").strip()
    account_id = request.form.get("account_id", "").strip()

    if category_id and account_id:
        spent_at = datetime.now().date().isoformat()
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                g.user_id,
                notes,
                normalize_expense_amount(amount),
                int(category_id),
                int(account_id),
                spent_at,
            ),
        )
        conn.commit()
        conn.close()

    return redirect_home()


@app.route("/expenses/<int:expense_id>/edit", methods=["POST"])
def edit_expense(expense_id):
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    category_id = request.form.get("category_id", "").strip()
    account_id = request.form.get("account_id", "").strip()
    spent_at_raw = request.form.get("spent_at", "").strip()

    if category_id and account_id:
        spent_at = normalize_txn_day_from_form(spent_at_raw)
        conn = get_connection()
        conn.execute(
            """
            UPDATE expenses
            SET notes = ?, amount = ?, category_id = ?, account_id = ?, spent_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                notes,
                normalize_expense_amount(amount),
                int(category_id),
                int(account_id),
                spent_at,
                expense_id,
                g.user_id,
            ),
        )
        conn.commit()
        conn.close()

    return redirect_home()


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM expenses WHERE id = ? AND user_id = ?",
        (expense_id, g.user_id),
    )
    conn.commit()
    conn.close()
    return redirect_home()


@app.route("/accounts/add", methods=["POST"])
def add_account():
    name = request.form.get("name", "").strip()
    opening_balance = request.form.get("opening_balance", "0").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO accounts(user_id, name, opening_balance) VALUES (?, ?, ?)",
            (g.user_id, name, float(opening_balance)),
        )
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
def delete_account(account_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM accounts WHERE id = ? AND user_id = ?",
        (account_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[vibe-budgeting] refused account delete {account_id} (still referenced or missing)")
    return redirect_home()


@app.route("/accounts/<int:account_id>/edit", methods=["POST"])
def edit_account(account_id):
    name = request.form.get("name", "").strip()
    opening_balance = request.form.get("opening_balance", "0").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "UPDATE accounts SET name = ?, opening_balance = ? WHERE id = ? AND user_id = ?",
            (name, float(opening_balance), account_id, g.user_id),
        )
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/income/<int:income_id>/delete", methods=["POST"])
def delete_income(income_id):
    conn = get_connection()
    conn.execute(
        "DELETE FROM income_entries WHERE id = ? AND user_id = ?",
        (income_id, g.user_id),
    )
    conn.commit()
    conn.close()
    return redirect_home()


@app.route("/income/add", methods=["POST"])
def add_income():
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    account_id = request.form.get("account_id", "").strip()
    category_id = request.form.get("category_id", "").strip()

    if category_id and account_id:
        received_at = datetime.now().date().isoformat()
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO income_entries (user_id, notes, amount, category_id, account_id, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                g.user_id,
                notes,
                normalize_income_amount(amount),
                int(category_id),
                int(account_id),
                received_at,
            ),
        )
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/income/<int:income_id>/edit", methods=["POST"])
def edit_income(income_id):
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    account_id = request.form.get("account_id", "").strip()
    category_id = request.form.get("category_id", "").strip()
    received_at_raw = request.form.get("received_at", "").strip()

    if category_id and account_id:
        received_at = normalize_txn_day_from_form(received_at_raw)
        conn = get_connection()
        conn.execute(
            """
            UPDATE income_entries
            SET notes = ?, amount = ?, category_id = ?, account_id = ?, received_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                notes,
                normalize_income_amount(amount),
                int(category_id),
                int(account_id),
                received_at,
                income_id,
                g.user_id,
            ),
        )
        conn.commit()
        conn.close()

    return redirect_home()


@app.route("/transfers/add", methods=["POST"])
def add_transfer():
    from_raw = request.form.get("from_account_id", "").strip()
    to_raw = request.form.get("to_account_id", "").strip()
    amount_raw = request.form.get("amount", "").strip()
    notes = request.form.get("notes", "").strip()
    transferred_at = normalize_txn_day_from_form(request.form.get("transferred_at", "").strip())

    if not from_raw or not to_raw:
        return redirect_home()

    try:
        from_id = int(from_raw)
        to_id = int(to_raw)
    except ValueError:
        flash("Invalid accounts.", "error")
        return redirect_home()

    if from_id == to_id:
        flash("Choose two different accounts.", "error")
        return redirect_home()

    try:
        amt = abs(float(amount_raw))
    except (TypeError, ValueError):
        amt = 0.0

    if amt <= 0:
        flash("Enter a positive amount.", "error")
        return redirect_home()

    conn = get_connection()
    uid = g.user_id
    if not conn.execute(
        "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
        (from_id, uid),
    ).fetchone() or not conn.execute(
        "SELECT 1 FROM accounts WHERE id = ? AND user_id = ?",
        (to_id, uid),
    ).fetchone():
        conn.close()
        flash("Invalid accounts.", "error")
        return redirect_home()

    conn.execute(
        """
        INSERT INTO account_transfers (user_id, from_account_id, to_account_id, amount, transferred_at, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (uid, from_id, to_id, amt, transferred_at, notes),
    )
    conn.commit()
    conn.close()
    flash("Transfer recorded.", "success")
    return redirect_home()


@app.route("/transfers/<int:transfer_id>/delete", methods=["POST"])
def delete_transfer(transfer_id):
    conn = get_connection()
    cursor = conn.execute(
        "DELETE FROM account_transfers WHERE id = ? AND user_id = ?",
        (transfer_id, g.user_id),
    )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        flash("Transfer not found.", "error")
    return redirect_home()


_prepare_sqlite_storage()
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)