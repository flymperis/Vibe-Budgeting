from datetime import datetime, timedelta, timezone
from openpyxl import Workbook, load_workbook

from config import EXPORT_FORMAT_VERSION, SHEET_ACCOUNTS, SHEET_EXPENSES, SHEET_EXPENSE_CATEGORIES, SHEET_INCOME, SHEET_INCOME_CATEGORIES, SHEET_META
from db import get_connection
from helpers import _lookup_account_id, _lookup_category_id


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
