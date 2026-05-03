from flask import Flask, redirect, render_template, request, url_for
import sqlite3

app = Flask(__name__)
DB_PATH = "database.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount >= 0),
            category_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES categories(id)
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            opening_balance REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS income_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount >= 0),
            account_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id)
        );
        """
    )

    conn.execute("INSERT OR IGNORE INTO categories(name) VALUES ('General')")
    conn.execute("INSERT OR IGNORE INTO accounts(name, opening_balance) VALUES ('Main', 0)")
    conn.commit()
    conn.close()


@app.route("/")
def index():
    conn = get_connection()

    categories = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    expenses = conn.execute(
        """
        SELECT e.id, e.item, e.amount, e.created_at, c.name AS category_name
        FROM expenses e
        JOIN categories c ON c.id = e.category_id
        ORDER BY e.created_at DESC
        """
    ).fetchall()

    accounts = conn.execute("SELECT id, name, opening_balance FROM accounts ORDER BY name").fetchall()
    income_entries = conn.execute(
        """
        SELECT i.id, i.source, i.amount, i.created_at, a.name AS account_name
        FROM income_entries i
        JOIN accounts a ON a.id = i.account_id
        ORDER BY i.created_at DESC
        """
    ).fetchall()

    total_expenses = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM expenses").fetchone()["total"]
    total_income = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM income_entries").fetchone()["total"]

    account_balances = conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.opening_balance + COALESCE(SUM(i.amount), 0) AS current_balance
        FROM accounts a
        LEFT JOIN income_entries i ON i.account_id = a.id
        GROUP BY a.id, a.name, a.opening_balance
        ORDER BY a.name
        """
    ).fetchall()

    conn.close()

    return render_template(
        "index.html",
        categories=categories,
        expenses=expenses,
        accounts=accounts,
        income_entries=income_entries,
        total_expenses=total_expenses,
        total_income=total_income,
        net_balance=total_income - total_expenses,
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
    return redirect(url_for("index"))


@app.route("/categories/<int:category_id>/edit", methods=["POST"])
def edit_category(category_id):
    name = request.form.get("name", "").strip()
    if name:
        conn = get_connection()
        conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        conn.commit()
        conn.close()
    return redirect(url_for("index"))


@app.route("/expenses/add", methods=["POST"])
def add_expense():
    item = request.form.get("item", "").strip()
    amount = request.form.get("amount", "0").strip()
    category_id = request.form.get("category_id", "").strip()

    if item and category_id:
        conn = get_connection()
        conn.execute(
            "INSERT INTO expenses (item, amount, category_id) VALUES (?, ?, ?)",
            (item, float(amount), int(category_id)),
        )
        conn.commit()
        conn.close()

    return redirect(url_for("index"))


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
    return redirect(url_for("index"))


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
    return redirect(url_for("index"))


@app.route("/income/add", methods=["POST"])
def add_income():
    source = request.form.get("source", "").strip()
    amount = request.form.get("amount", "0").strip()
    account_id = request.form.get("account_id", "").strip()

    if source and account_id:
        conn = get_connection()
        conn.execute(
            "INSERT INTO income_entries (source, amount, account_id) VALUES (?, ?, ?)",
            (source, float(amount), int(account_id)),
        )
        conn.commit()
        conn.close()
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)