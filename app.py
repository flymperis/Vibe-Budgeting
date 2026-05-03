from flask import Flask, redirect, render_template, request, url_for
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

app = Flask(__name__)
DB_PATH = "database.db"
ALLOWED_PANELS = {"expenses", "income", "summary", "settings"}
SETTINGS_SECTIONS = {"general", "expenses", "income"}


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


def to_datetime_local_value(raw):
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    try:
        normalized = text.replace(" ", "T", 1)
        if len(normalized) == 10:
            normalized = f"{normalized}T00:00"
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return ""
    return parsed.replace(microsecond=0).isoformat(timespec="minutes")


@app.template_filter("dt_local")
def dt_local_filter(raw):
    return to_datetime_local_value(raw)


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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


def init_db():
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
            amount REAL NOT NULL CHECK (amount >= 0),
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
        }
        if path in mapping:
            return mapping[path]

        for prefix, mapped in (
            ("/categories/", "settings"),
            ("/income-categories/", "settings"),
            ("/accounts/", "settings"),
            ("/expenses/", "expenses"),
            ("/income/", "income"),
        ):
            if path.startswith(prefix):
                return mapped

    return "expenses"


def redirect_home(panel=None):
    target = panel if panel in ALLOWED_PANELS else resolve_active_panel()
    month = normalize_month(request.form.get("month") or request.args.get("month"))
    settings_section = normalize_settings_section(
        request.form.get("settings_section") or request.args.get("settings_section")
    )
    return redirect(
        url_for(
            "index",
            panel=target,
            month=month,
            settings_section=settings_section if target == "settings" else None,
        )
    )


@app.route("/")
def index():
    conn = get_connection()

    active_panel = request.args.get("panel", "").strip()
    if active_panel not in ALLOWED_PANELS:
        active_panel = "expenses"

    month_filter = normalize_month(request.args.get("month"))
    settings_section = normalize_settings_section(request.args.get("settings_section"))

    categories = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    income_categories = conn.execute("SELECT id, name FROM income_categories ORDER BY name").fetchall()
    expenses = conn.execute(
        """
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
        ORDER BY e.spent_at DESC
        """
    ).fetchall()

    accounts = conn.execute("SELECT id, name, opening_balance FROM accounts ORDER BY name").fetchall()
    income_entries = conn.execute(
        """
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
        ORDER BY i.received_at DESC
        """
    ).fetchall()

    total_expenses = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE strftime('%Y-%m', spent_at) = ?
        """,
        (month_filter,),
    ).fetchone()["total"]
    total_income = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM income_entries
        WHERE strftime('%Y-%m', received_at) = ?
        """,
        (month_filter,),
    ).fetchone()["total"]

    expense_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, SUM(e.amount) AS total_amount
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE strftime('%Y-%m', e.spent_at) = ?
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (month_filter,),
    ).fetchall()

    income_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, SUM(i.amount) AS total_amount
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        WHERE strftime('%Y-%m', i.received_at) = ?
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (month_filter,),
    ).fetchall()

    account_balances = conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.opening_balance
                + COALESCE(income_totals.total_income, 0)
                - COALESCE(expense_totals.total_expenses, 0) AS current_balance
        FROM accounts a
        LEFT JOIN (
            SELECT account_id, SUM(amount) AS total_income
            FROM income_entries
            GROUP BY account_id
        ) income_totals ON income_totals.account_id = a.id
        LEFT JOIN (
            SELECT account_id, SUM(amount) AS total_expenses
            FROM expenses
            GROUP BY account_id
        ) expense_totals ON expense_totals.account_id = a.id
        ORDER BY a.name
        """
    ).fetchall()

    conn.close()

    return render_template(
        "index.html",
        categories=categories,
        income_categories=income_categories,
        expenses=expenses,
        accounts=accounts,
        income_entries=income_entries,
        active_panel=active_panel,
        month_filter=month_filter,
        settings_section=settings_section,
        total_expenses=total_expenses,
        total_income=total_income,
        net_balance=total_income - total_expenses,
        expense_breakdown=expense_breakdown,
        income_breakdown=income_breakdown,
        account_balances=account_balances,
    )


@app.route("/categories/add", methods=["POST"])
def add_category():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/categories/<int:category_id>/edit", methods=["POST"])
def edit_category(category_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
def delete_category(category_id):
    conn = get_connection()
    cursor = conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[budget-app] refused category delete {category_id} (still referenced or missing)")
    return redirect_home()


@app.route("/income-categories/add", methods=["POST"])
def add_income_category():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute("INSERT OR IGNORE INTO income_categories(name) VALUES (?)", (name,))
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/income-categories/<int:category_id>/edit", methods=["POST"])
def edit_income_category(category_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute("UPDATE income_categories SET name = ? WHERE id = ?", (name, category_id))
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/income-categories/<int:category_id>/delete", methods=["POST"])
def delete_income_category(category_id):
    conn = get_connection()
    cursor = conn.execute("DELETE FROM income_categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[budget-app] refused income category delete {category_id} (still referenced or missing)")
    return redirect_home()


@app.route("/expenses/add", methods=["POST"])
def add_expense():
    notes = request.form.get("notes", "").strip()
    amount = request.form.get("amount", "0").strip()
    category_id = request.form.get("category_id", "").strip()
    account_id = request.form.get("account_id", "").strip()

    if category_id and account_id:
        spent_at = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
        conn = get_connection()
        conn.execute(
            "INSERT INTO expenses (notes, amount, category_id, account_id, spent_at) VALUES (?, ?, ?, ?, ?)",
            (notes, float(amount), int(category_id), int(account_id), spent_at),
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
        spent_at = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
        if spent_at_raw:
            try:
                spent_at = datetime.fromisoformat(spent_at_raw).isoformat(timespec="seconds")
            except ValueError:
                spent_at = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
        conn = get_connection()
        conn.execute(
            """
            UPDATE expenses
            SET notes = ?, amount = ?, category_id = ?, account_id = ?, spent_at = ?
            WHERE id = ?
            """,
            (notes, float(amount), int(category_id), int(account_id), spent_at, expense_id),
        )
        conn.commit()
        conn.close()

    return redirect_home()


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
def delete_expense(expense_id):
    conn = get_connection()
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
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
            "INSERT OR IGNORE INTO accounts(name, opening_balance) VALUES (?, ?)",
            (name, float(opening_balance)),
        )
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/accounts/<int:account_id>/delete", methods=["POST"])
def delete_account(account_id):
    conn = get_connection()
    cursor = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        print(f"[budget-app] refused account delete {account_id} (still referenced or missing)")
    return redirect_home()


@app.route("/accounts/<int:account_id>/edit", methods=["POST"])
def edit_account(account_id):
    name = request.form.get("name", "").strip()
    opening_balance = request.form.get("opening_balance", "0").strip()
    if name:
        conn = get_connection()
        conn.execute(
            "UPDATE accounts SET name = ?, opening_balance = ? WHERE id = ?",
            (name, float(opening_balance), account_id),
        )
        conn.commit()
        conn.close()
    return redirect_home()


@app.route("/income/<int:income_id>/delete", methods=["POST"])
def delete_income(income_id):
    conn = get_connection()
    conn.execute("DELETE FROM income_entries WHERE id = ?", (income_id,))
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
        received_at = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
        conn = get_connection()
        conn.execute(
            "INSERT INTO income_entries (notes, amount, category_id, account_id, received_at) VALUES (?, ?, ?, ?, ?)",
            (notes, float(amount), int(category_id), int(account_id), received_at),
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
        received_at = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
        if received_at_raw:
            try:
                received_at = datetime.fromisoformat(received_at_raw).isoformat(timespec="seconds")
            except ValueError:
                received_at = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
        conn = get_connection()
        conn.execute(
            """
            UPDATE income_entries
            SET notes = ?, amount = ?, category_id = ?, account_id = ?, received_at = ?
            WHERE id = ?
            """,
            (notes, float(amount), int(category_id), int(account_id), received_at, income_id),
        )
        conn.commit()
        conn.close()

    return redirect_home()


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)