"""Nightly backups: a verified copy, the schedule, and rotation that only
ever deletes its own files."""

import os
import sqlite3
from datetime import date, datetime, timedelta

import backup


def _db(tmp_path, rows=3):
    path = tmp_path / "database.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    return str(path), conn


def test_backup_is_a_complete_checked_copy_even_while_the_app_writes(tmp_path):
    path, live = _db(tmp_path)
    live.execute("INSERT INTO t VALUES (99)")  # uncommitted write in progress
    out = backup.backup_now(path, str(tmp_path / "backups"), now=datetime(2026, 11, 10, 3, 30))
    live.rollback()
    assert os.path.basename(out) == "vibe-budgeting-2026-11-10.db"
    copy = sqlite3.connect(out)
    assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    snap = backup.backup_now(path, str(tmp_path / "backups"), label="Pre-Deploy!", now=datetime(2026, 11, 10, 17, 5, 9))
    assert os.path.basename(snap) == "vibe-budgeting-2026-11-10-170509-predeploy.db"
    assert not [n for n in os.listdir(tmp_path / "backups") if n.endswith(".partial")]


def _touch(folder, names):
    folder.mkdir(exist_ok=True)
    for n in names:
        (folder / n).write_bytes(b"x")


def test_rotation_keeps_14_days_and_one_per_month_and_nothing_else_is_touched(tmp_path):
    folder = tmp_path / "backups"
    today = date(2026, 11, 10)
    days = [today - timedelta(days=i) for i in range(400)]
    _touch(folder, [f"vibe-budgeting-{d.isoformat()}.db" for d in days])
    _touch(folder, [f"vibe-budgeting-2026-11-{d:02d}-120000-predeploy.db" for d in range(1, 9)])
    _touch(folder, ["database.db.bak-20261110-131355", "notes.txt", "vibe-budgeting-weird.db"])

    backup.rotate(str(folder), keep_days=14, keep_months=12)
    left = sorted(os.listdir(folder))

    dailies = [n for n in left if backup.DAILY_RE.match(n)]
    newest = {f"vibe-budgeting-{(today - timedelta(days=i)).isoformat()}.db" for i in range(14)}
    assert newest <= set(dailies)
    monthly = set(dailies) - newest
    # the last copy of each month, 12 months counting November; November's and
    # October's (31 Oct) are already among the 14 newest
    assert sorted(monthly) == sorted(
        f"vibe-budgeting-{d}.db" for d in [
            "2026-09-30", "2026-08-31", "2026-07-31", "2026-06-30", "2026-05-31",
            "2026-04-30", "2026-03-31", "2026-02-28", "2026-01-31", "2025-12-31",
        ]
    )
    snaps = [n for n in left if backup.SNAPSHOT_RE.match(n)]
    assert snaps == [f"vibe-budgeting-2026-11-{d:02d}-120000-predeploy.db" for d in range(4, 9)]
    for untouched in ("database.db.bak-20261110-131355", "notes.txt", "vibe-budgeting-weird.db"):
        assert untouched in left


def test_schedule():
    night = (3, 30)
    assert backup.is_due(datetime(2026, 11, 10, 12, 0), None, *night)  # never backed up
    assert not backup.is_due(datetime(2026, 11, 10, 2, 0), date(2026, 11, 9), *night)  # before tonight's time
    assert backup.is_due(datetime(2026, 11, 10, 3, 31), date(2026, 11, 9), *night)
    assert not backup.is_due(datetime(2026, 11, 10, 23, 0), date(2026, 11, 10), *night)  # done today
    assert backup.is_due(datetime(2026, 11, 10, 1, 0), date(2026, 11, 8), *night)  # server was off: catch up now


def test_run_if_due_and_switch_off(tmp_path, monkeypatch):
    path, live = _db(tmp_path)
    live.close()
    folder = tmp_path / "elsewhere"
    monkeypatch.setenv("VB_BACKUP_DIR", str(folder))
    monkeypatch.setenv("VB_BACKUP_TIME", "04:15")
    assert backup.run_if_due(path, now=datetime(2026, 11, 10, 4, 0)).endswith("vibe-budgeting-2026-11-10.db")
    assert backup.run_if_due(path, now=datetime(2026, 11, 10, 5, 0)) is None
    assert backup.run_if_due(path, now=datetime(2026, 11, 11, 4, 0)) is None  # before 04:15
    assert backup.run_if_due(path, now=datetime(2026, 11, 11, 4, 16)) is not None
    monkeypatch.setenv("VB_BACKUP_KEEP_DAYS", "0")
    assert backup.run_if_due(path, now=datetime(2026, 11, 20, 4, 16)) is None


def test_command_line_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VB_BACKUP_DIR", str(tmp_path))
    assert backup.main(["--label", "predeploy"]) == 0
    printed = capsys.readouterr().out.strip()
    assert os.path.exists(printed) and backup.SNAPSHOT_RE.match(os.path.basename(printed))


def test_sleeps_until_the_nightly_time_but_at_most_an_hour():
    night = (3, 30)
    assert backup.seconds_to_next_check(datetime(2026, 11, 10, 3, 0), *night) == 1800
    assert backup.seconds_to_next_check(datetime(2026, 11, 10, 3, 29, 50), *night) == 30  # floor
    assert backup.seconds_to_next_check(datetime(2026, 11, 10, 12, 0), *night) == 3600  # cap
    assert backup.seconds_to_next_check(datetime(2026, 11, 10, 3, 30), *night) == 3600  # tomorrow's run is far
