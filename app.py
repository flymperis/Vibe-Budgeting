from flask import Flask, flash, redirect, render_template, request, send_file, url_for
import calendar
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlparse

from openpyxl import Workbook, load_workbook

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-change-me")
DB_PATH = "database.db"
EXPORT_FORMAT_VERSION = 1
LIST_PAGE_SIZE = 75

SHEET_META = "_meta"
SHEET_ACCOUNTS = "Accounts"
SHEET_EXPENSE_CATEGORIES = "ExpenseCategories"
SHEET_INCOME_CATEGORIES = "IncomeCategories"
SHEET_EXPENSES = "Expenses"
SHEET_INCOME = "Income"
ALLOWED_PANELS = {"expenses", "income", "summary", "yearly", "settings"}
SETTINGS_SECTIONS = {"general", "expenses", "income", "export", "migration"}


def normalize_expense_amount(raw):
    """Store expenses as negative; positive input is treated as magnitude spent."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        value = 0.0
    return -abs(value)


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

    migrate_expenses_signed_amounts(conn)


def migrate_expenses_signed_amounts(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='expenses'"
    ).fetchone()
    if not row or not row["sql"]:
        return
    create_sql = row["sql"]
    if not re.search(r"CHECK\s*\(\s*amount\s*>=\s*0\s*\)", create_sql, re.I):
        return
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
        ):
            if path.startswith(prefix):
                return mapped

    return "expenses"


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
    exp_pg = normalize_list_page(request.form.get("exp_page") or request.args.get("exp_page") or 1)
    inc_pg = normalize_list_page(request.form.get("inc_page") or request.args.get("inc_page") or 1)
    if target == "expenses" and exp_pg > 1:
        query["exp_page"] = exp_pg
    if target == "income" and inc_pg > 1:
        query["inc_page"] = inc_pg
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


def _parse_excel_timestamp(value, sheet, row_num, column_label):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{sheet} row {row_num}: missing {column_label}")
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(timespec="seconds")
    text = str(value).strip()
    try:
        normalized = text.replace(" ", "T", 1)
        if len(normalized) == 10:
            normalized = f"{normalized}T00:00:00"
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            raise ValueError(f"{sheet} row {row_num}: invalid {column_label}")
    return parsed.replace(microsecond=0).isoformat(timespec="seconds")


def _optional_created_at(value, sheet, row_num):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_excel_timestamp(value, sheet, row_num, "created_at")


def _build_export_workbook(conn):
    wb = Workbook()
    ws_meta = wb.active
    ws_meta.title = SHEET_META
    ws_meta.append(["key", "value"])
    ws_meta.append(["format_version", EXPORT_FORMAT_VERSION])
    ws_meta.append(["exported_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")])

    ws_accounts = wb.create_sheet(SHEET_ACCOUNTS)
    ws_accounts.append(["name", "opening_balance"])
    for row in conn.execute("SELECT name, opening_balance FROM accounts ORDER BY name"):
        ws_accounts.append([row["name"], row["opening_balance"]])

    ws_ec = wb.create_sheet(SHEET_EXPENSE_CATEGORIES)
    ws_ec.append(["name"])
    for row in conn.execute("SELECT name FROM categories ORDER BY name"):
        ws_ec.append([row["name"]])

    ws_ic = wb.create_sheet(SHEET_INCOME_CATEGORIES)
    ws_ic.append(["name"])
    for row in conn.execute("SELECT name FROM income_categories ORDER BY name"):
        ws_ic.append([row["name"]])

    ws_exp = wb.create_sheet(SHEET_EXPENSES)
    ws_exp.append(["notes", "amount", "category_name", "account_name", "spent_at", "created_at"])
    for row in conn.execute(
        """
        SELECT e.notes, e.amount, c.name AS category_name, a.name AS account_name, e.spent_at, e.created_at
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        JOIN accounts a ON a.id = e.account_id
        ORDER BY e.spent_at ASC, e.id ASC
        """
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
        ORDER BY i.received_at ASC, i.id ASC
        """
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


def _lookup_id(conn, sql, name):
    row = conn.execute(sql, (name.strip(),)).fetchone()
    return row["id"] if row else None


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
            spent_at = _parse_excel_timestamp(row.get("spent_at"), SHEET_EXPENSES, idx, "spent_at")
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
            received_at = _parse_excel_timestamp(
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


def _run_import_workbook(wb, replace_movements, sync_opening_balances):
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
                "INSERT OR IGNORE INTO accounts(name, opening_balance) VALUES (?, ?)",
                (name, opening_balance),
            )
            if sync_opening_balances:
                conn.execute(
                    "UPDATE accounts SET opening_balance = ? WHERE name = ?",
                    (opening_balance, name),
                )

        if errors:
            conn.rollback()
            return errors

        for idx, row in enumerate(expense_cat_rows, start=2):
            name = row.get("name")
            if name is None or not str(name).strip():
                continue
            conn.execute(
                "INSERT OR IGNORE INTO categories(name) VALUES (?)",
                (str(name).strip(),),
            )

        for name in sorted(expense_cats_from_movements):
            conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))

        for idx, row in enumerate(income_cat_rows, start=2):
            name = row.get("name")
            if name is None or not str(name).strip():
                continue
            conn.execute(
                "INSERT OR IGNORE INTO income_categories(name) VALUES (?)",
                (str(name).strip(),),
            )

        for name in sorted(income_cats_from_movements):
            conn.execute("INSERT OR IGNORE INTO income_categories(name) VALUES (?)", (name,))

        for acc_name in sorted(accounts_from_movements):
            conn.execute(
                "INSERT OR IGNORE INTO accounts(name, opening_balance) VALUES (?, ?)",
                (acc_name, 0.0),
            )

        if replace_movements:
            conn.execute("DELETE FROM expenses")
            conn.execute("DELETE FROM income_entries")

        for notes, amount, cat_name, acc_name, spent_at, created_at in insert_expenses:
            category_id = _lookup_id(conn, "SELECT id FROM categories WHERE name = ?", cat_name)
            account_id = _lookup_id(conn, "SELECT id FROM accounts WHERE name = ?", acc_name)
            if category_id is None:
                errors.append(f"{SHEET_EXPENSES}: unknown expense category {cat_name!r}")
            if account_id is None:
                errors.append(f"{SHEET_EXPENSES}: unknown account {acc_name!r}")

        for notes, amount, cat_name, acc_name, received_at, created_at in insert_income:
            category_id = _lookup_id(conn, "SELECT id FROM income_categories WHERE name = ?", cat_name)
            account_id = _lookup_id(conn, "SELECT id FROM accounts WHERE name = ?", acc_name)
            if category_id is None:
                errors.append(f"{SHEET_INCOME}: unknown income category {cat_name!r}")
            if account_id is None:
                errors.append(f"{SHEET_INCOME}: unknown account {acc_name!r}")

        if errors:
            conn.rollback()
            return errors

        for notes, amount, cat_name, acc_name, spent_at, created_at in insert_expenses:
            category_id = _lookup_id(conn, "SELECT id FROM categories WHERE name = ?", cat_name)
            account_id = _lookup_id(conn, "SELECT id FROM accounts WHERE name = ?", acc_name)
            if created_at:
                conn.execute(
                    """
                    INSERT INTO expenses (notes, amount, category_id, account_id, spent_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (notes, amount, category_id, account_id, spent_at, created_at),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO expenses (notes, amount, category_id, account_id, spent_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (notes, amount, category_id, account_id, spent_at),
                )

        for notes, amount, cat_name, acc_name, received_at, created_at in insert_income:
            category_id = _lookup_id(conn, "SELECT id FROM income_categories WHERE name = ?", cat_name)
            account_id = _lookup_id(conn, "SELECT id FROM accounts WHERE name = ?", acc_name)
            if created_at:
                conn.execute(
                    """
                    INSERT INTO income_entries (notes, amount, category_id, account_id, received_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (notes, amount, category_id, account_id, received_at, created_at),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO income_entries (notes, amount, category_id, account_id, received_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (notes, amount, category_id, account_id, received_at),
                )

        conn.commit()
        return []
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.route("/export/excel")
def export_excel():
    conn = get_connection()
    try:
        wb = _build_export_workbook(conn)
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

    errors = _run_import_workbook(workbook, replace_movements, sync_opening_balances)
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
        active_panel = "expenses"
        section_from_legacy = None

    month_filter = normalize_month(request.args.get("month"))
    year_filter = normalize_year(request.args.get("year"))
    year_str, month_str = month_filter.split("-", 1)
    month_start = datetime(int(year_str), int(month_str), 1)
    month_end = month_start + timedelta(days=32)
    month_end = month_end.replace(day=1)
    month_start_iso = month_start.strftime("%Y-%m-%d %H:%M:%S")
    month_end_iso = month_end.strftime("%Y-%m-%d %H:%M:%S")
    settings_section = normalize_settings_section(
        section_from_legacy or request.args.get("settings_section")
    )

    categories = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    income_categories = conn.execute("SELECT id, name FROM income_categories ORDER BY name").fetchall()

    expense_total = conn.execute("SELECT COUNT(*) AS n FROM expenses").fetchone()["n"]
    expense_num_pages = max(1, (expense_total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    expense_page = min(normalize_list_page(request.args.get("exp_page")), expense_num_pages)
    expense_offset = (expense_page - 1) * LIST_PAGE_SIZE
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
        LIMIT ? OFFSET ?
        """,
        (LIST_PAGE_SIZE, expense_offset),
    ).fetchall()

    accounts = conn.execute("SELECT id, name, opening_balance FROM accounts ORDER BY name").fetchall()

    income_total = conn.execute("SELECT COUNT(*) AS n FROM income_entries").fetchone()["n"]
    income_num_pages = max(1, (income_total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    income_page = min(normalize_list_page(request.args.get("inc_page")), income_num_pages)
    income_offset = (income_page - 1) * LIST_PAGE_SIZE
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
        LIMIT ? OFFSET ?
        """,
        (LIST_PAGE_SIZE, income_offset),
    ).fetchall()

    total_expenses = conn.execute(
        """
        SELECT COALESCE(-SUM(amount), 0) AS total
        FROM expenses
        WHERE spent_at >= ? AND spent_at < ?
        """,
        (month_start_iso, month_end_iso),
    ).fetchone()["total"]
    total_income = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM income_entries
        WHERE received_at >= ? AND received_at < ?
        """,
        (month_start_iso, month_end_iso),
    ).fetchone()["total"]

    expense_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, COALESCE(-SUM(e.amount), 0) AS total_amount
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        WHERE e.spent_at >= ? AND e.spent_at < ?
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (month_start_iso, month_end_iso),
    ).fetchall()

    income_breakdown = conn.execute(
        """
        SELECT c.name AS category_name, SUM(i.amount) AS total_amount
        FROM income_entries i
        JOIN income_categories c ON c.id = i.category_id
        WHERE i.received_at >= ? AND i.received_at < ?
        GROUP BY c.name
        ORDER BY total_amount DESC
        """,
        (month_start_iso, month_end_iso),
    ).fetchall()

    account_balances = conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.opening_balance
                + COALESCE(income_totals.total_income, 0)
                + COALESCE(expense_totals.total_expenses, 0) AS current_balance
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

    year_start_iso = f"{year_filter:04d}-01-01 00:00:00"
    year_end_iso = f"{year_filter + 1:04d}-01-01 00:00:00"
    expense_by_month = {
        row["m"]: float(row["total"])
        for row in conn.execute(
            """
            SELECT CAST(strftime('%m', spent_at) AS INTEGER) AS m,
                   COALESCE(-SUM(amount), 0) AS total
            FROM expenses
            WHERE spent_at >= ? AND spent_at < ?
            GROUP BY m
            """,
            (year_start_iso, year_end_iso),
        )
    }
    income_by_month = {
        row["m"]: float(row["total"])
        for row in conn.execute(
            """
            SELECT CAST(strftime('%m', received_at) AS INTEGER) AS m,
                   COALESCE(SUM(amount), 0) AS total
            FROM income_entries
            WHERE received_at >= ? AND received_at < ?
            GROUP BY m
            """,
            (year_start_iso, year_end_iso),
        )
    }

    month_names = calendar.month_name
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
            }
        )
    yearly_total_delta = yearly_total_income - yearly_total_expenses

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
        year_filter=year_filter,
        settings_section=settings_section,
        total_expenses=total_expenses,
        total_income=total_income,
        net_balance=total_income - total_expenses,
        expense_breakdown=expense_breakdown,
        income_breakdown=income_breakdown,
        account_balances=account_balances,
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
            (notes, normalize_expense_amount(amount), int(category_id), int(account_id), spent_at),
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
            (notes, normalize_expense_amount(amount), int(category_id), int(account_id), spent_at, expense_id),
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
            (notes, normalize_income_amount(amount), int(category_id), int(account_id), received_at),
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
            (notes, normalize_income_amount(amount), int(category_id), int(account_id), received_at, income_id),
        )
        conn.commit()
        conn.close()

    return redirect_home()


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)