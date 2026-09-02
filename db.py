from werkzeug.security import check_password_hash, generate_password_hash
import integrations
import os
import re
import sqlite3
import sys
import telegram_bot


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
    migrate_crypto_transactions(conn)
    migrate_stock_transactions(conn)
    migrate_crypto_month_prices(conn)
    migrate_stock_month_prices(conn)
    integrations.migrate_user_integrations(conn)
    telegram_bot.migrate_telegram(conn)
    migrate_add_indexes(conn)

def migrate_add_indexes(conn):
    """Additive only (IF NOT EXISTS) — safe to run against an existing populated DB.

    Every route below filters/sorts on these user_id + date columns; without
    indexes those queries full-scan as history grows.
    """
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, spent_at);
        CREATE INDEX IF NOT EXISTS idx_income_entries_user_date ON income_entries(user_id, received_at);
        CREATE INDEX IF NOT EXISTS idx_stock_transactions_user_date ON stock_transactions(user_id, transacted_at);
        CREATE INDEX IF NOT EXISTS idx_crypto_transactions_user_date ON crypto_transactions(user_id, transacted_at);
        CREATE INDEX IF NOT EXISTS idx_account_transfers_user_date ON account_transfers(user_id, transferred_at);
        CREATE INDEX IF NOT EXISTS idx_recurring_entries_user ON recurring_entries(user_id);
        CREATE INDEX IF NOT EXISTS idx_categories_user ON categories(user_id);
        CREATE INDEX IF NOT EXISTS idx_income_categories_user ON income_categories(user_id);
        CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);
        """
    )

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
        "INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)",
        (uid, "Other"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO income_categories (user_id, name) VALUES (?, ?)",
        (uid, "General"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO income_categories (user_id, name) VALUES (?, ?)",
        (uid, "Salary"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO income_categories (user_id, name) VALUES (?, ?)",
        (uid, "Other"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO accounts (user_id, name, opening_balance) VALUES (?, ?, ?)",
        (uid, "Main", 0.0),
    )

def migrate_crypto_transactions(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            coin_id TEXT NOT NULL,
            coin_symbol TEXT NOT NULL,
            coin_name TEXT NOT NULL,
            tx_type TEXT NOT NULL CHECK (tx_type IN ('buy', 'sell')),
            quantity REAL NOT NULL CHECK (quantity > 0),
            price_per_unit REAL NOT NULL CHECK (price_per_unit >= 0),
            fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
            exchange TEXT NOT NULL DEFAULT '',
            transacted_at TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

def migrate_crypto_month_prices(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_month_prices (
            coin_id TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            price_eur REAL NOT NULL,
            price_date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'coingecko',
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (coin_id, year, month)
        )
        """
    )

def migrate_stock_month_prices(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_month_prices (
            symbol TEXT NOT NULL,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            price_usd REAL NOT NULL,
            price_date TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'yfinance',
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, year, month)
        )
        """
    )

def migrate_stock_transactions(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            symbol TEXT NOT NULL,
            ticker TEXT NOT NULL,
            instrument_name TEXT NOT NULL,
            tx_type TEXT NOT NULL CHECK (tx_type IN ('buy', 'sell')),
            quantity REAL NOT NULL CHECK (quantity > 0),
            price_per_unit REAL NOT NULL CHECK (price_per_unit >= 0),
            fee REAL NOT NULL DEFAULT 0 CHECK (fee >= 0),
            broker TEXT NOT NULL DEFAULT '',
            transacted_at TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
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
            months_to_run INTEGER CHECK (months_to_run IS NULL OR months_to_run > 0),
            notes TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cols = _column_names(conn, "recurring_entries")
    if cols and "months_to_run" not in cols:
        conn.execute(
            "ALTER TABLE recurring_entries ADD COLUMN months_to_run INTEGER CHECK (months_to_run IS NULL OR months_to_run > 0)"
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
