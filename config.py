from werkzeug.security import check_password_hash, generate_password_hash
import os
import re
import secrets


# 2 added the Transfers/Stocks/Crypto/Recurring sheets. Version 1 workbooks
# still import: the sheets added in 2 are optional on the way in.
EXPORT_FORMAT_VERSION = 2

LIST_PAGE_SIZE = 75

TRANSFER_LOG_LIMIT = 10

ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "true").lower() in ("1", "true", "yes")

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")

_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(16))

SHEET_META = "_meta"

SHEET_ACCOUNTS = "Accounts"

SHEET_EXPENSE_CATEGORIES = "ExpenseCategories"

SHEET_INCOME_CATEGORIES = "IncomeCategories"

SHEET_EXPENSES = "Expenses"

SHEET_INCOME = "Income"

SHEET_TRANSFERS = "Transfers"

SHEET_STOCKS = "Stocks"

SHEET_CRYPTO = "Crypto"

SHEET_RECURRING = "Recurring"

ALLOWED_PANELS = {
    "home",
    "expenses",
    "income",
    "recurring",
    "transfer",
    "summary",
    "yearly",
    "reports",
    "investments",
    "settings",
}

SETTINGS_SECTIONS = {"general", "banks", "expenses", "income", "export", "migration", "integrations"}

INVESTMENTS_SECTIONS = {"crypto", "stocks"}

REPORTS_SECTIONS = {"overview", "spending", "investments"}

# Old section names, kept so existing links and bookmarks still land somewhere
# sensible: the bank chart now lives on Overview, and the two portfolio charts
# share the Investments section instead of a tab each.
LEGACY_REPORTS_SECTIONS = {
    "bank": "overview",
    "crypto": "investments",
    "stocks": "investments",
}

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()

def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")

CSRF_FIELD_NAME = "_csrf_token"  # noqa: S105 - form field name, not a secret

_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

_CSRF_EXEMPT_ENDPOINTS = frozenset({"static"})
