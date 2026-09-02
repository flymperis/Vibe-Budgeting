from flask import Flask, abort, g, redirect, request, session, url_for
import hmac
import os
import secrets
import sys
import threading

import telegram_bot
from config import CSRF_FIELD_NAME, _CSRF_EXEMPT_ENDPOINTS, _CSRF_SAFE_METHODS, _env_flag
from db import DB_PATH, _prepare_sqlite_storage, get_connection, init_db
from finance import fetch_account_balances_through
from helpers import coerce_txn_day, get_csrf_token
from routes import accounts, auth, categories, dashboard, data_io, expenses, income, investments, recurring, settings


def _resolve_secret_key():
    """FLASK_SECRET_KEY if set; otherwise a random key persisted next to the
    DB so sessions survive restarts, instead of a hardcoded fallback value."""
    env_key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if env_key:
        return env_key

    key_path = os.path.join(os.path.dirname(DB_PATH) or ".", "secret_key")
    try:
        with open(key_path, "r", encoding="utf-8") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass

    generated = secrets.token_hex(32)
    os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(generated)
    except FileExistsError:
        with open(key_path, "r", encoding="utf-8") as f:
            generated = f.read().strip()
    print(
        f"[vibe-budgeting] WARNING: FLASK_SECRET_KEY not set; generated and "
        f"persisted a random key at {key_path!r}. Set FLASK_SECRET_KEY "
        "explicitly in production (see README).",
        file=sys.stderr,
    )
    return generated


app = Flask(__name__)
app.secret_key = _resolve_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Enable only when served over HTTPS (set VB_SECURE_COOKIES=true behind TLS).
    SESSION_COOKIE_SECURE=_env_flag("VB_SECURE_COOKIES", False),
)

for module in (
    auth,
    dashboard,
    expenses,
    income,
    accounts,
    categories,
    recurring,
    investments,
    settings,
    data_io,
):
    app.register_blueprint(module.bp)


@app.context_processor
def _inject_csrf_token():
    return {"csrf_token": get_csrf_token()}


@app.before_request
def _csrf_protect():
    if request.method in _CSRF_SAFE_METHODS:
        return None
    if request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return None
    expected = session.get("_csrf_token")
    submitted = request.form.get(CSRF_FIELD_NAME) or request.headers.get("X-CSRF-Token", "")
    if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
        abort(400, description="Invalid or missing CSRF token.")
    return None


@app.before_request
def _require_login():
    if request.endpoint in ("auth.login", "auth.register", "static", None):
        return None
    uid = session.get("user_id")
    if not uid:
        return redirect(url_for("auth.login", next=request.path))
    try:
        g.user_id = int(uid)
    except (TypeError, ValueError):
        session.pop("user_id", None)
        session.pop("username", None)
        return redirect(url_for("auth.login", next=request.path))
    g.username = session.get("username") or ""
    return None


@app.template_filter("txn_day")
def txn_day_filter(raw):
    return coerce_txn_day(raw)


@app.template_filter("money")
def money_filter(value, currency="€", signed=False):
    """1527.08 -> '1.527,08 €'. Stocks are quoted in USD, so pass '$' there.

    Returns an em dash for missing values: several investment columns are None
    until a live price has been fetched.
    """
    if value is None or value == "":
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"

    sign = "-" if amount < 0 else ("+" if signed and amount > 0 else "")
    # 1234.5 -> '1,234.50' -> '1.234,50' (dot thousands, comma decimals)
    digits = f"{abs(amount):,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if currency == "$":
        return f"{sign}${digits}"
    return f"{sign}{digits} {currency}" if currency else f"{sign}{digits}"


@app.template_filter("amount_class")
def amount_class_filter(value):
    """CSS class for colouring an amount. Sign stays visible in the text too,
    so colour is never the only carrier of meaning."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "amount-zero"
    if amount > 0:
        return "amount-pos"
    if amount < 0:
        return "amount-neg"
    return "amount-zero"


def arm_telegram_poller() -> None:
    global _telegram_poller_armed
    if _telegram_poller_armed:
        return
    with _telegram_poller_guard:
        if _telegram_poller_armed:
            return
        telegram_bot.start_poller(get_connection, fetch_account_balances_through, _poll_lock_path)
        _telegram_poller_armed = True


_prepare_sqlite_storage()
init_db()

_poll_lock_path = os.path.join(os.path.dirname(DB_PATH) or ".", "telegram_poll.lock")
_telegram_poller_armed = False
_telegram_poller_guard = threading.Lock()

_conn_boot = get_connection()
_boot_cfg = telegram_bot.get_server_config(_conn_boot)
if telegram_bot.is_configured(_boot_cfg):
    ok, msg = telegram_bot.clear_webhook(_boot_cfg)
    print(f"Telegram: {msg}", file=sys.stderr)
_conn_boot.close()

if __name__ == "__main__":
    arm_telegram_poller()
    app.run(host="0.0.0.0", port=5000)
