"""Nightly SQLite backups with rotation.

Every night (03:30 server time by default, or as soon as the app starts if
the last backup is over a day old) the database is copied with SQLite's
online backup API, which is safe while the app keeps writing, checked with
PRAGMA integrity_check, and kept as backups/vibe-budgeting-YYYY-MM-DD.db next
to the database. The newest 14 daily copies are kept, plus the last copy of
each of the 12 previous months.

Snapshots taken on demand (python -m backup --label predeploy) are named
vibe-budgeting-YYYY-MM-DD-HHMMSS-<label>.db; the newest 5 of each label are
kept. Nothing else in the folder is ever deleted.

Settings (environment):
  VB_BACKUP_DIR          folder for backups (default: "backups" next to the database)
  VB_BACKUP_KEEP_DAYS    daily copies to keep (default 14; 0 turns nightly backups off)
  VB_BACKUP_KEEP_MONTHS  monthly copies to keep (default 12)
  VB_BACKUP_TIME         HH:MM of the nightly run (default 03:30)
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta

PREFIX = "vibe-budgeting-"
DAILY_RE = re.compile(r"^vibe-budgeting-(\d{4}-\d{2}-\d{2})\.db$")
SNAPSHOT_RE = re.compile(r"^vibe-budgeting-(\d{4}-\d{2}-\d{2})-(\d{6})-([a-z0-9]+)\.db$")
KEEP_SNAPSHOTS = 5
MAX_SLEEP = 3600  # wake at least hourly, so a missed night (suspend, clock change) is caught within an hour
MIN_SLEEP = 30


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def settings(db_path: str) -> dict:
    raw_time = os.environ.get("VB_BACKUP_TIME", "03:30")
    try:
        hour, minute = (int(x) for x in raw_time.split(":", 1))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        hour, minute = 3, 30
    return {
        "dir": os.environ.get("VB_BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups"),
        "keep_days": _int_env("VB_BACKUP_KEEP_DAYS", 14),
        "keep_months": _int_env("VB_BACKUP_KEEP_MONTHS", 12),
        "hour": hour,
        "minute": minute,
    }


# --- taking a copy ---------------------------------------------------------------------


def backup_now(db_path: str, dest_dir: str, *, label: str | None = None, now: datetime | None = None) -> str:
    """Copy the database into dest_dir and verify the copy; returns its path.
    Raises RuntimeError (and leaves nothing behind) if the copy is damaged."""
    now = now or datetime.now()
    os.makedirs(dest_dir, exist_ok=True)
    if label:
        clean = re.sub(r"[^a-z0-9]", "", label.lower()) or "manual"
        name = f"{PREFIX}{now:%Y-%m-%d-%H%M%S}-{clean}.db"
    else:
        name = f"{PREFIX}{now:%Y-%m-%d}.db"
    final = os.path.join(dest_dir, name)
    partial = final + ".partial"
    if os.path.exists(partial):
        os.remove(partial)
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(partial)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if result != "ok":
        os.remove(partial)
        raise RuntimeError(f"backup failed the integrity check: {result}")
    os.replace(partial, final)
    return final


# --- rotation --------------------------------------------------------------------------


def rotate(dest_dir: str, keep_days: int, keep_months: int, keep_snapshots: int = KEEP_SNAPSHOTS) -> list[str]:
    """Delete backups beyond what the policy keeps; returns the deleted names.
    Only files this module names are considered."""
    if not os.path.isdir(dest_dir):
        return []
    dailies: dict[str, str] = {}
    snapshots: dict[str, list[tuple[str, str]]] = {}
    for name in os.listdir(dest_dir):
        m = DAILY_RE.match(name)
        if m:
            dailies[m.group(1)] = name
            continue
        m = SNAPSHOT_RE.match(name)
        if m:
            snapshots.setdefault(m.group(3), []).append((f"{m.group(1)}-{m.group(2)}", name))

    keep: set[str] = set()
    days = sorted(dailies, reverse=True)
    keep.update(dailies[d] for d in days[:keep_days])
    newest_by_month: dict[str, str] = {}
    for d in days:
        newest_by_month.setdefault(d[:7], d)
    for month in sorted(newest_by_month, reverse=True)[:keep_months]:
        keep.add(dailies[newest_by_month[month]])
    for items in snapshots.values():
        keep.update(name for _, name in sorted(items, reverse=True)[:keep_snapshots])

    deleted = []
    for name in list(dailies.values()) + [n for items in snapshots.values() for _, n in items]:
        if name not in keep:
            os.remove(os.path.join(dest_dir, name))
            deleted.append(name)
    return sorted(deleted)


# --- scheduling --------------------------------------------------------------------------


def last_daily(dest_dir: str) -> date | None:
    if not os.path.isdir(dest_dir):
        return None
    found = [DAILY_RE.match(n).group(1) for n in os.listdir(dest_dir) if DAILY_RE.match(n)]
    return date.fromisoformat(max(found)) if found else None


def is_due(now: datetime, last: date | None, hour: int, minute: int) -> bool:
    """A day with no backup yet is due once its nightly time has passed, and
    straight away when the newest copy is more than a day old (the server
    was off at night)."""
    if last is None or last < now.date() - timedelta(days=1):
        return True
    if last >= now.date():
        return False
    return (now.hour, now.minute) >= (hour, minute)


def seconds_to_next_check(now: datetime, hour: int, minute: int) -> float:
    """Sleep until the next nightly time, but never longer than an hour."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(MIN_SLEEP, min(MAX_SLEEP, (target - now).total_seconds()))


def run_if_due(db_path: str, now: datetime | None = None) -> str | None:
    cfg = settings(db_path)
    if cfg["keep_days"] == 0:
        return None
    now = now or datetime.now()
    if not is_due(now, last_daily(cfg["dir"]), cfg["hour"], cfg["minute"]):
        return None
    path = backup_now(db_path, cfg["dir"], now=now)
    deleted = rotate(cfg["dir"], cfg["keep_days"], cfg["keep_months"])
    print(f"[backup] Saved {os.path.basename(path)}" + (f", removed {len(deleted)} old" if deleted else ""), flush=True)
    return path


_started = False
_start_lock = threading.Lock()


def start_scheduler(db_path: str) -> None:
    """Back up when due, then sleep until the nightly time (rechecking at
    least hourly). One thread per process."""
    global _started
    with _start_lock:
        if _started or settings(db_path)["keep_days"] == 0:
            return
        _started = True

    def loop():
        while True:
            try:
                run_if_due(db_path)
            except Exception as exc:  # keep trying next round; never take the app down
                print(f"[backup] Failed: {exc}", flush=True)
            cfg = settings(db_path)
            time.sleep(seconds_to_next_check(datetime.now(), cfg["hour"], cfg["minute"]))

    threading.Thread(target=loop, name="nightly-backup", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the Vibe Budgeting database now.")
    parser.add_argument("--label", default="manual", help="snapshot label, e.g. predeploy (default: manual)")
    args = parser.parse_args(argv)
    from db import DB_PATH

    cfg = settings(DB_PATH)
    path = backup_now(DB_PATH, cfg["dir"], label=args.label)
    rotate(cfg["dir"], cfg["keep_days"] or 10**6, cfg["keep_months"])
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
