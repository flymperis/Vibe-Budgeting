"""Bank badges for accounts.

Accounts are free-text names ("Eurobank Main", "Πειραιώς μισθοδοσία",
"Revolut EUR"), so the bank is recognised from the name rather than stored.
Each known bank gets a small badge in its brand colours; drop an official logo
at static/banks/<slug>.svg (or .png/.webp/.ico) and it is used instead of the
badge. The shipped files are the banks' own site/app icons.
"""

import os
import re
import unicodedata

_LOGO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "banks")

# (slug, display name, badge text, background, text colour, name patterns)
# Patterns are matched as whole words against the lower-cased, accent-stripped
# account name, so "viva" does not fire on "vivacious". More specific entries
# come first ("viva wallet" before the generic "wallet" of Cash).
_BANKS = [
    # --- Greece ------------------------------------------------------------
    ("nbg", "National Bank of Greece", "NBG", "#005F86", "#FFFFFF",
     [r"nbg", r"ethniki", r"εθνικη", r"national bank of greece", r"i-bank"]),
    ("alpha", "Alpha Bank", "α", "#0A3E8C", "#FFFFFF",
     [r"alpha ?bank", r"alpha", r"αλφα"]),
    ("eurobank", "Eurobank", "EB", "#C8102E", "#FFFFFF",
     [r"eurobank", r"euro bank", r"γιουρομπανκ"]),
    ("piraeus", "Piraeus Bank", "P", "#FFD100", "#1B1B1B",
     [r"piraeus", r"peiraios", r"peiraiws", r"πειραιως", r"winbank"]),
    # Attica Bank rebranded to CrediaBank; old account names still match.
    ("attica", "CrediaBank", "CB", "#E87722", "#FFFFFF",
     [r"credia", r"crediabank", r"attica", r"attiki", r"αττικη", r"αττικης", r"pancreta", r"παγκρητια"]),
    ("optima", "Optima bank", "O", "#0C2C52", "#FFFFFF",
     [r"optima"]),
    ("viva", "Viva Wallet", "V", "#1D1D1B", "#FFFFFF",
     [r"viva ?wallet", r"viva"]),
    # --- Neobanks / payments ---------------------------------------------
    ("revolut", "Revolut", "R", "#191C1F", "#FFFFFF", [r"revolut"]),
    ("n26", "N26", "N26", "#36A18B", "#FFFFFF", [r"n26"]),
    ("wise", "Wise", "W", "#9FE870", "#163300", [r"wise", r"transferwise"]),
    ("paypal", "PayPal", "PP", "#003087", "#FFFFFF", [r"paypal"]),
    # --- Meal vouchers ---------------------------------------------------
    # Edenred's Ticket Restaurant card is commonly kept as its own "account".
    ("ticket_restaurant", "Ticket Restaurant", "TR", "#F72717", "#FFFFFF",
     [r"ticket ?restaurant", r"ticket", r"edenred", r"τικετ"]),
    # --- Not a bank, but a very common "account" --------------------------
    ("cash", "Cash", "€", "#2E7D32", "#FFFFFF", [r"cash", r"μετρητα", r"metrita", r"πορτοφολι", r"wallet"]),
]


def _fold(text):
    """Lower-case and strip accents, so 'Πειραιώς' matches 'πειραιως'."""
    decomposed = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _logo_url(slug):
    for ext in ("svg", "png", "webp", "ico"):
        if os.path.isfile(os.path.join(_LOGO_DIR, f"{slug}.{ext}")):
            return f"/static/banks/{slug}.{ext}"
    return None


# Patterns are compiled with explicit (?<!\w)...(?!\w) boundaries instead of \b
# so they behave the same next to Greek letters, digits and hyphens.
_COMPILED = [
    {
        "slug": slug,
        "name": name,
        "label": label,
        "bg": bg,
        "fg": fg,
        "logo": _logo_url(slug),
        "_re": re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)"),
    }
    for slug, name, label, bg, fg, patterns in _BANKS
]


def detect_bank(account_name):
    """Return the bank badge dict for an account name, or None if unknown."""
    folded = _fold(account_name)
    if not folded:
        return None
    for bank in _COMPILED:
        if bank["_re"].search(folded):
            return {k: v for k, v in bank.items() if k != "_re"}
    return None


def bank_badges_for(names):
    """{account name: badge} for every name that maps to a known bank; used to
    hand the same badges to the JS select pickers."""
    out = {}
    for name in names:
        bank = detect_bank(name)
        if bank:
            out[name] = bank
    return out
