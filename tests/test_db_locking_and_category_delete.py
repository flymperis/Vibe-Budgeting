"""Two production bugs that turned out to be one.

Deleting a category that still had expenses raised IntegrityError past the
`conn.commit(); conn.close()` at the end of the route. The traceback kept that
connection — and its open write transaction — alive while Flask rendered the
500, so the *next* write got "database is locked". The log showed both errors
together for that reason.

Verified before fixing: a leaked connection blocks the next writer in WAL just
as it does in rollback-journal mode, so closing the connection is the fix that
matters and WAL is a separate, secondary improvement.
"""

import sqlite3
import threading

import pytest

import app as vb_app
import db as vb_db
from tests.conftest import csrf_token, register_and_login


def _ids(username):
    conn = vb_app.get_connection()
    uid = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()["id"]
    cat = conn.execute(
        "SELECT id, name FROM categories WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)
    ).fetchone()
    inc = conn.execute(
        "SELECT id, name FROM income_categories WHERE user_id = ? ORDER BY id LIMIT 1",
        (uid,),
    ).fetchone()
    acc = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? ORDER BY id LIMIT 1", (uid,)
    ).fetchone()["id"]
    conn.close()
    return uid, cat, inc, acc


# --------------------------------------------------------------------------
# 1. Connection settings
# --------------------------------------------------------------------------

def test_busy_timeout_is_actually_applied():
    """connect(timeout=) is supposed to set this; assert it rather than assume."""
    conn = vb_app.get_connection()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == vb_db.BUSY_TIMEOUT_MS
    finally:
        conn.close()


def test_foreign_keys_stay_on():
    conn = vb_app.get_connection()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_journal_mode_is_wal_or_a_working_fallback():
    """WAL needs shared memory, which some networked filesystems (PythonAnywhere,
    NFS, SMB) cannot provide. Falling back is fine; crashing or ending up in a
    broken mode is not."""
    conn = vb_app.get_connection()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()
    assert mode in {"wal", "delete", "truncate", "persist"}


# --------------------------------------------------------------------------
# 2. Deleting a category that is still referenced
# --------------------------------------------------------------------------

def test_deleting_category_with_expenses_is_refused_not_a_500(client):
    register_and_login(client, "lock-delete-expense")
    token = csrf_token(client)
    uid, cat, _inc, acc = _ids("lock-delete-expense")

    resp = client.post(
        "/expenses/add",
        data={"notes": "coffee", "amount": "-4.20", "category_id": cat["id"],
              "account_id": acc, "_csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    resp = client.post(
        f"/categories/{cat['id']}/delete",
        data={"_csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200, "must not 500"
    body = resp.get_data(as_text=True)
    assert "still has" in body and "1 expense" in body
    assert "flash-error" in body

    # and the category survives
    conn = vb_app.get_connection()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM categories WHERE id = ?", (cat["id"],)
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_deleting_income_category_with_entries_is_refused(client):
    register_and_login(client, "lock-delete-income")
    token = csrf_token(client)
    uid, _cat, inc, acc = _ids("lock-delete-income")

    client.post(
        "/income/add",
        data={"notes": "salary", "amount": "1200", "category_id": inc["id"],
              "account_id": acc, "_csrf_token": token},
        follow_redirects=True,
    )

    resp = client.post(
        f"/income-categories/{inc['id']}/delete",
        data={"_csrf_token": token},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "still has" in body and "1 income entry" in body
    # never the naive plural
    assert "income entrys" not in body


def test_unused_category_still_deletes(client):
    """The guard must not block the normal case."""
    register_and_login(client, "lock-delete-ok")
    token = csrf_token(client)

    client.post("/categories/add", data={"name": "Disposable", "_csrf_token": token},
                follow_redirects=True)
    conn = vb_app.get_connection()
    row = conn.execute("SELECT id FROM categories WHERE name = 'Disposable'").fetchone()
    conn.close()
    assert row is not None

    resp = client.post(f"/categories/{row['id']}/delete",
                       data={"_csrf_token": token}, follow_redirects=True)
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM categories WHERE id = ?", (row["id"],)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_recurring_rule_blocks_delete_even_though_it_has_no_foreign_key(client):
    """recurring_entries.category_id has no FK, so this would not raise — the
    rows would just be silently orphaned. Blocked deliberately."""
    register_and_login(client, "lock-delete-recurring")
    token = csrf_token(client)
    uid, cat, _inc, acc = _ids("lock-delete-recurring")

    conn = vb_app.get_connection()
    conn.execute(
        "INSERT INTO recurring_entries"
        " (user_id, entry_type, amount, category_id, account_id, day_of_month, notes, enabled)"
        " VALUES (?, 'expense', 50.0, ?, ?, 1, '', 1)",
        (uid, cat["id"], acc),
    )
    conn.commit()
    conn.close()

    resp = client.post(f"/categories/{cat['id']}/delete",
                       data={"_csrf_token": token}, follow_redirects=True)
    assert resp.status_code == 200
    assert "1 recurring rule" in resp.get_data(as_text=True)


# --------------------------------------------------------------------------
# 3. The lock itself
# --------------------------------------------------------------------------

def test_refused_delete_does_not_leave_the_database_locked(client):
    """The regression that matters: after a refused delete, the next write must
    still go through. Before the fix this raised OperationalError."""
    register_and_login(client, "lock-no-leak")
    token = csrf_token(client)
    uid, cat, _inc, acc = _ids("lock-no-leak")

    client.post(
        "/expenses/add",
        data={"notes": "x", "amount": "-1", "category_id": cat["id"],
              "account_id": acc, "_csrf_token": token},
        follow_redirects=True,
    )
    client.post(f"/categories/{cat['id']}/delete",
                data={"_csrf_token": token}, follow_redirects=True)

    # the write that used to fail with "database is locked"
    resp = client.post("/categories/add",
                       data={"name": "AfterRefusal", "_csrf_token": token},
                       follow_redirects=True)
    assert resp.status_code == 200

    conn = vb_app.get_connection()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM categories WHERE name = 'AfterRefusal' AND user_id = ?",
            (uid,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_writers_do_not_deadlock():
    """Two writers overlapping on the same file. Serialising is fine — raising
    OperationalError is not."""
    errors = []
    barrier = threading.Barrier(2, timeout=10)

    def writer(tag):
        try:
            barrier.wait()
            for i in range(10):
                conn = vb_app.get_connection()
                try:
                    conn.execute(
                        "INSERT INTO users (username, password_hash) VALUES (?, 'x')",
                        (f"conc-{tag}-{i}",),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001 - the point is to surface any of them
            errors.append(f"{tag}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "writer thread hung — likely a lock"

    assert not errors, f"concurrent writes failed: {errors}"


def test_a_leaked_transaction_blocks_writers_in_every_journal_mode(tmp_path):
    """Documents why closing the connection is the fix and WAL is not.

    WAL stops readers blocking writers; it does nothing about a write
    transaction that was never closed.
    """
    results = {}
    for mode in ("DELETE", "WAL"):
        path = tmp_path / f"{mode}.db"
        setup = sqlite3.connect(path)
        setup.execute(f"PRAGMA journal_mode = {mode}")
        setup.executescript(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
            "CREATE TABLE child (id INTEGER PRIMARY KEY,"
            " parent_id INTEGER NOT NULL REFERENCES parent(id));"
            "INSERT INTO parent (id) VALUES (1);"
            "INSERT INTO child (id, parent_id) VALUES (1, 1);"
        )
        setup.commit()
        setup.close()

        held = sqlite3.connect(path, timeout=0.5)
        held.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            held.execute("DELETE FROM parent WHERE id = 1")  # leaves a transaction open

        other = sqlite3.connect(path, timeout=0.5)
        try:
            other.execute("INSERT INTO parent (id) VALUES (2)")
            other.commit()
            results[mode] = "unblocked"
        except sqlite3.OperationalError:
            results[mode] = "blocked"
        finally:
            other.close()

        held.rollback()   # what the fix's finally/rollback does
        held.close()

        after = sqlite3.connect(path, timeout=0.5)
        try:
            after.execute("INSERT INTO parent (id) VALUES (3)")
            after.commit()
            results[mode + "-after-close"] = "unblocked"
        except sqlite3.OperationalError:
            results[mode + "-after-close"] = "blocked"
        finally:
            after.close()

    assert results["DELETE"] == "blocked"
    assert results["WAL"] == "blocked", "WAL alone does not rescue a leaked transaction"
    assert results["DELETE-after-close"] == "unblocked"
    assert results["WAL-after-close"] == "unblocked"
