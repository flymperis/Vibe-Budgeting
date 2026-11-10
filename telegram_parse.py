"""Understanding Telegram messages: amounts, dates, accounts, categories,
several transactions per message, shared bills, receipts and questions.

Whatever can be decided without the AI is decided here first (which amounts
the message contains, which account it names, the user's own past category
for the same description); the AI fills in the rest and is checked against
those facts. When the AI is off or unreachable a basic parser still records
simple messages like "καφές 3.50".
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import integrations
import tags

FALLBACK_CATEGORY = "Other"
SHARED_NOTE_PREFIX = "[Shared]"
MAX_AMOUNT = 1_000_000
HISTORY_EXAMPLES = 40
RECEIPT_MAX_AGE_DAYS = 90

# --- text folding ----------------------------------------------------------------
# Greek, Greeklish and OCR output (which mixes Greek, Latin and Cyrillic
# look-alikes) are folded to one plain Latin spelling so "Άλφα", "alpha" and
# "ALFA" compare equal.

_TRANSLIT = {
    # Greek
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th",
    "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p",
    "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "x", "ψ": "ps",
    "ω": "o",
    # Cyrillic look-alikes that OCR models emit for Greek capitals
    "а": "a", "в": "v", "г": "g", "д": "d", "е": "e", "з": "z", "и": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "у": "y", "ф": "f", "х": "x", "ы": "y",
}
_PHONETIC = (
    ("oy", "u"), ("ou", "u"), ("ei", "i"), ("oi", "i"), ("ai", "e"), ("ae", "e"),
    ("y", "i"), ("ph", "f"), ("ck", "k"), ("c", "k"),
)


def fold(text: str) -> str:
    s = unicodedata.normalize("NFD", str(text or "").lower())
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    for old, new in _PHONETIC:
        s = s.replace(old, new)
    return s


def _fw(*words: str) -> tuple[str, ...]:
    """Folded forms of word lists written in natural spelling."""
    return tuple(fold(w) for w in words)


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^0-9a-z]+", fold(text)) if t]


def key(text: str) -> str:
    """Comparable form of a description: folded words, no digits."""
    return " ".join(t for t in tokens(text) if not t.isdigit())


def _similar(a: str, b: str) -> bool:
    """Same word allowing for Greek endings (Νίκος / Νίκου) and short forms."""
    if a == b:
        return True
    if min(len(a), len(b)) < 3:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(3, min(len(a), len(b)) - 2)


# --- amounts and dates -------------------------------------------------------------

_NUM = r"\d{1,7}(?:[.,]\d{1,2})?"
_CUR = r"(?:€|euros?|eur|ευρώ|ευρω|ευρ|e)"
_AMOUNT_RE = re.compile(
    rf"(?<![\w.,/:#])(?<!\d-)({_NUM})(?=\s*{_CUR}?(?![\w/:%]|[.,-]\d))",
    re.IGNORECASE,
)
_SLASH_DATE_RE = re.compile(r"(?<![\d.,])(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?![\d/-])")
_DOT_DATE_RE = re.compile(r"(?<![\d.,])(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?![\d.])")
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


@dataclass
class Amount:
    value: float
    start: int
    end: int


# "for 3 months", "κάθε 2 εβδομάδες", "12 δόσεις": a count, not money. Whole
# nouns only, so "500 μηνιαία" / "500 monthly" still read as an amount.
_COUNT_WORDS = frozenset({
    "mina", "mines", "minon", "mhna", "mhnes", "month", "months",
    "evdomada", "evdomades", "evdomadon", "ebdomada", "ebdomades", "week", "weeks",
    "xrono", "xronia", "xronon", "iear", "iears",
    "fora", "fores", "times", "dosi", "dosis", "doson", "installment", "installments",
})
_NEXT_WORD_RE = re.compile(r"\s*([^\W\d_]+)")


def _counts_something(text: str, end: int) -> bool:
    m = _NEXT_WORD_RE.match(text, end)
    return bool(m) and fold(m.group(1)) in _COUNT_WORDS


def find_amounts(text: str) -> list[Amount]:
    found = []
    for m in _AMOUNT_RE.finditer(text or ""):
        if _counts_something(text, m.end()):
            continue
        try:
            value = float(m.group(1).replace(",", "."))
        except ValueError:
            continue
        if 0 < value < MAX_AMOUNT:
            found.append(Amount(value, m.start(), m.end()))
    return found


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


_DAY_BEFORE = frozenset(fold(w) for w in ("προχθές", "προχτές", "proxthes", "proxtes"))
_DAY_BEFORE_PHRASE = fold("day before yesterday")
_YESTERDAY = frozenset(fold(w) for w in ("χθες", "χτες", "εχθές", "εχτές", "xthes", "xtes", "yesterday"))


def detect_date(text: str, today: date) -> date | None:
    folded = fold(text)
    words = set(tokens(text))
    if words & _DAY_BEFORE or _DAY_BEFORE_PHRASE in folded:
        return today - timedelta(days=2)
    if words & _YESTERDAY:
        return today - timedelta(days=1)
    m = _ISO_DATE_RE.search(text or "")
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for regex in (_DOT_DATE_RE, _SLASH_DATE_RE):
        m = regex.search(text or "")
        if not m:
            continue
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        if year:
            y = int(year)
            y = y + 2000 if y < 100 else y
        else:
            y = today.year
        found = _safe_date(y, month, day)
        if found and not year and found > today:
            found = _safe_date(y - 1, month, day)
        if found:
            return found
    return None


def _date_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for regex in (_ISO_DATE_RE, _DOT_DATE_RE, _SLASH_DATE_RE):
        spans += [m.span() for m in regex.finditer(text or "")]
    return spans


# --- accounts -------------------------------------------------------------------

# Words that appear in account names but never pick one on their own
# ("restaurant 20" is a meal, not the Ticket Restaurant card).
_ACCOUNT_STOPWORDS = frozenset({
    "restaurant", "bank", "trapeza", "bonus", "kard", "karta", "kredit", "pistotiki",
    "akkunt", "logariasmos", "shared", "balankes", "main",
})
_CASH_WORDS = frozenset({"metrita", "metritis", "metritois", "kash", "xartonomisma"})


def detect_account(text: str, accounts: list[dict]) -> tuple[int | None, set[str]]:
    """(account id, message words that named it). accounts: [{id, name}].
    The best-scoring account wins; ties go to the shorter name, so "alpha"
    means AlphaBank and "alpha visa" means AlphaBank Bonus Visa."""
    words = [t for t in tokens(text) if not t.isdigit() and len(t) >= 3]
    best = (0, 0, None)
    best_words: set[str] = set()
    for acc in accounts:
        parts = [p for p in tokens(acc["name"]) if not p.isdigit()]
        joined = "".join(parts)
        score, used = 0, set()
        for w in words:
            if w in _ACCOUNT_STOPWORDS:
                continue
            if w in _CASH_WORDS and joined in _CASH_WORDS:
                score, used = score + 3, used | {w}
            elif w == joined or w in parts:
                score, used = score + 3, used | {w}
            elif len(w) >= 4 and (joined.startswith(w) or any(p.startswith(w) for p in parts)):
                score, used = score + 2, used | {w}
            elif len(w) >= 4 and any(_similar(w, p) and len(p) >= 4 for p in parts if p not in _ACCOUNT_STOPWORDS):
                score, used = score + 1, used | {w}
        if score and (score, -len(parts)) > best[:2]:
            best = (score, -len(parts), acc["id"])
            best_words = used
    return best[2], best_words


# --- categories -----------------------------------------------------------------

# Catch-all category names, most preferred first. The user's own catch-all is
# used; the bot creates "Other" only when they have none of these.
FALLBACK_NAMES = (
    "Other", "Others", "Misc", "Miscellaneous", "General", "Uncategorized", "Uncategorised",
    "Άλλο", "Άλλα", "Λοιπά", "Διάφορα", "Γενικά",
)


def _flat(text: str) -> str:
    return key(text).replace(" ", "")


_FALLBACK_KEYS = tuple(_flat(n) for n in FALLBACK_NAMES)


def is_fallback(name: str) -> bool:
    return _flat(name) in _FALLBACK_KEYS


def existing_fallback(options: list[str]) -> str | None:
    by_key = {_flat(o): o for o in options}
    for k in _FALLBACK_KEYS:
        if k in by_key:
            return by_key[k]
    return None


def find_fallback_category(options: list[str]) -> str:
    return existing_fallback(options) or FALLBACK_CATEGORY


@dataclass(frozen=True)
class Concept:
    """Something people spend on (or earn), recognised in a message by its
    words and matched to whatever the user called that category, in any
    language: groceries → "Super Market", "Groceries", "Σούπερ μάρκετ"…"""

    kind: str
    examples: tuple[str, ...]  # for the AI prompt
    prefixes: tuple[str, ...]  # folded message-word prefixes
    exact: tuple[str, ...]  # folded whole words (short ones like "ταξί" ≠ "ταξίδι")
    categories: tuple[str, ...]  # folded category-name prefixes, most specific first


def _concept(kind: str, words: tuple[str, ...], categories: tuple[str, ...]) -> Concept:
    prefixes = tuple(_flat(w) for w in words if not w.startswith("="))
    exact = tuple(_flat(w[1:]) for w in words if w.startswith("="))
    return Concept(kind, tuple(w.lstrip("=") for w in words[:3]), prefixes, exact,
                   tuple(c for c in (_flat(x) for x in categories) if len(c) >= 3))


# Words are prefixes unless marked "=" (exact). Category names are matched by
# prefix against each word of the user's category and against the whole name.
CONCEPTS = [
    _concept("expense",
             ("καφέ", "coffee", "freddo", "φρέντο", "espresso", "cappuccino", "καπουτσίνο", "cafe"),
             ("coffee", "cafe", "καφέ", "καφές", "entertainment", "διασκέδαση", "ψυχαγωγία", "fun", "leisure")),
    _concept("expense",
             ("σούπερ", "super", "σκλαβενίτ", "sklaben", "μασούτ", "βασιλόπουλ", "lidl", "κρητικ", "γαλαξία",
              "mymarket", "κρεοπωλ", "μανάβ", "φούρν", "αρτοποι", "grocer", "μάρκετ"),
             ("super market", "supermarket", "σούπερ", "groceries", "grocery", "τρόφιμα", "παντοπωλ", "market", "μάρκετ")),
    _concept("expense",
             ("efood", "wolt", "delivery", "ντελίβερι"),
             ("delivery", "ντελίβερι", "παραγγελ", "takeaway", "take away")),
    _concept("expense",
             ("εστιατόρ", "restaurant", "ταβέρν", "σουβλ", "πίτσα", "pizza", "burger", "μπέργκερ", "=γύρος", "=γύρο"),
             ("restaurant", "εστιατόρ", "φαγητό", "eating out", "dining", "food", "φαγητ")),
    _concept("expense",
             ("βενζίν", "benzin", "καύσιμ", "fuel", "petrol", "diesel"),
             ("fuel", "καύσιμ", "βενζίν", "petrol", "car", "αυτοκίνητ", "vehicle", "όχημα", "transport", "μεταφορ", "μετακιν")),
    _concept("expense",
             ("=ταξί", "=taxi", "uber", "=beat", "μετρό", "metro", "λεωφορ", "=bus", "parking", "πάρκινγκ", "διόδι", "toll"),
             ("transport", "μεταφορ", "μετακιν", "συγκοινων", "taxi", "ταξί", "car", "αυτοκίνητ", "vehicle", "όχημα")),
    _concept("expense",
             ("ρεύμα", "ρευμα", "=δεη", "electric", "ηλεκτρ"),
             ("electric", "ρεύμα", "ηλεκτρ", "ενέργεια", "energy", "utilit", "bills", "λογαριασμοί", "κοινόχρηστ")),
    _concept("expense",
             ("νερό", "ευδαπ", "eydap", "water", "ύδρευσ"),
             ("water", "νερό", "ύδρευσ", "utilit", "bills", "λογαριασμοί")),
    _concept("expense",
             ("ενοίκ", "νοίκι", "rent"),
             ("rent", "ενοίκ", "νοίκι", "housing", "στέγαση", "house", "home", "σπίτι")),
    _concept("expense",
             ("internet", "ίντερνετ", "wifi"),
             ("internet", "ίντερνετ", "telecom", "τηλεπικοιν", "bills", "λογαριασμοί")),
    _concept("expense",
             ("κινητ", "mobile", "καρτοκινητ"),
             ("mobile", "κινητ", "phone", "τηλέφων", "telecom", "τηλεπικοιν")),
    _concept("expense",
             ("γυμναστήρ", "=gym", "fitness", "crossfit", "pilates", "yoga", "γιόγκα"),
             ("gym", "γυμναστήρ", "fitness", "sport", "αθλητ", "άθλησ")),
    _concept("expense",
             ("φαρμακ", "pharmac", "γιατρ", "doctor", "ιατρ", "οδοντ", "dentist", "νοσοκομ", "hospital"),
             ("health", "υγεία", "ιατρ", "medical", "pharmac", "φαρμακ")),
    _concept("expense",
             ("δώρ", "gift", "present"),
             ("gift", "δώρ")),
    _concept("expense",
             ("ταξίδ", "trip", "travel", "hotel", "ξενοδοχ", "airbnb", "booking", "πτήσ", "flight", "διακοπ", "vacation"),
             ("trip", "travel", "ταξίδ", "διακοπ", "holiday", "vacation")),
    _concept("expense",
             ("ρούχ", "clothes", "παπούτσ", "shoes", "shopping", "zara"),
             ("clothes", "clothing", "ρούχ", "ένδυσ", "shopping", "ψώνια")),
    _concept("expense",
             ("σινεμά", "cinema", "movie", "ταινί", "θέατρ", "theat", "συναυλ", "concert", "μπύρ", "beer", "=bar", "club",
              "=ποτό", "=ποτά"),
             ("entertainment", "διασκέδαση", "ψυχαγωγία", "fun", "leisure", "going out")),
    _concept("expense",
             ("netflix", "spotify", "disney", "youtube", "icloud", "=hbo", "subscription", "συνδρομ"),
             ("subscript", "συνδρομ", "streaming", "entertainment", "διασκέδαση")),
    _concept("income",
             ("μισθ", "salary", "payroll", "wage"),
             ("salary", "μισθ", "payroll", "wage", "μισθοδοσ")),
]
_INCOME_PREFIXES = ("misth", "salari", "payroll", "bonus", "pliromi apo", "eispra", "ispra")


def _message_words(text: str) -> list[str]:
    words = [t for t in tokens(text) if not t.isdigit()]
    return words + [a + b for a, b in zip(words, words[1:])]  # "my market" → "mimarket"


def _triggered(concept: Concept, words: list[str]) -> bool:
    return any(w in concept.exact or any(w.startswith(p) for p in concept.prefixes if len(p) >= 3) for w in words)


def concept_category(concept: Concept, options: list[str]) -> str | None:
    """The user's category for a concept, whatever language it is named in."""
    named = [(o, _flat(o), key(o).split()) for o in options if not is_fallback(o)]
    for pattern in concept.categories:
        for original, flat, parts in named:
            if len(pattern) <= 3:
                hit = pattern in parts or flat == pattern  # "car" must not pick "Cards"
            else:
                hit = flat.startswith(pattern) or any(p.startswith(pattern) for p in parts)
            if hit:
                return original
    return None


def keyword_category(text: str, options: list[str], kind: str) -> str | None:
    words = _message_words(text)
    for concept in CONCEPTS:
        if concept.kind == kind and _triggered(concept, words):
            picked = concept_category(concept, options)
            if picked:
                return picked
    return None


def category_named_in(text: str, options: list[str]) -> str | None:
    """One of the user's categories written in the message itself
    ("βενζίνη 50" with a category "Βενζίνη"); the longest name wins."""
    words = [w for w in _message_words(text) if len(w) >= 3]
    best, best_len = None, 0
    for option in options:
        if is_fallback(option):
            continue
        parts = [p for p in key(option).split() if len(p) >= 3]
        flat = "".join(parts)
        if not parts:
            continue
        whole = any(w == flat or (len(flat) >= 4 and _similar(w, flat)) for w in words)
        each = all(any(_similar(w, p) for w in words) for p in parts)
        if (whole or each) and len(flat) > best_len:
            best, best_len = option, len(flat)
    return best


def category_hints(expense_categories: list[str], income_categories: list[str]) -> list[str]:
    hints = []
    for concept in CONCEPTS:
        picked = concept_category(concept, income_categories if concept.kind == "income" else expense_categories)
        if picked:
            hints.append(f'{"/".join(concept.examples)} → "{picked}"' + (" (income)" if concept.kind == "income" else ""))
    return hints


def best_name_match(name: str, options: list[str]) -> str | None:
    """An option for a free-text category name: exact, folded, contained, or
    word match; None when nothing is close."""
    needle = (name or "").strip()
    if not needle or not options:
        return None
    lowered = {o.lower(): o for o in options}
    if needle.lower() in lowered:
        return lowered[needle.lower()]
    folded = {key(o).replace(" ", ""): o for o in options}
    nk = key(needle).replace(" ", "")
    if nk and nk in folded:
        return folded[nk]
    for fk, original in folded.items():
        if nk and fk and len(min(nk, fk, key=len)) >= 4 and (nk in fk or fk in nk):
            return original
    for word in tokens(needle):
        if len(word) < 3 or word.isdigit():
            continue
        for fk, original in folded.items():
            if word == fk:
                return original
    return None


def resolve_category(name: str, options: list[str]) -> str:
    clean = (name or "").strip()
    if not options:
        return FALLBACK_CATEGORY
    if not clean or is_fallback(clean) or clean.lower() in {"unknown", "none", "null"}:
        return find_fallback_category(options)
    matched = best_name_match(clean, options)
    if matched and not is_fallback(matched):
        return matched
    return find_fallback_category(options)


# --- the user's own history ----------------------------------------------------------


@dataclass
class History:
    examples: list[tuple[str, str, str]] = field(default_factory=list)  # (note, category, kind)
    lookup: dict[tuple[str, str], str] = field(default_factory=dict)  # (kind, key) → category

    def category_for(self, kind: str, text: str, options: list[str]) -> str | None:
        k = key(text)
        if not k:
            return None
        found = self.lookup.get((kind, k))
        if found and not is_fallback(found):
            return next((o for o in options if o.lower() == found.lower()), None)
        return None


def load_history(conn, user_id: int, limit: int = 400) -> History:
    """Past descriptions and the category the user filed them under, newest
    first. A description filed under several categories maps to the one used
    most often."""
    rows = conn.execute(
        """
        SELECT kind, notes, category, day, id FROM (
            SELECT 'expense' AS kind, e.notes, c.name AS category, date(e.spent_at) AS day, e.id
            FROM expenses e JOIN categories c ON c.id = e.category_id
            WHERE e.user_id = ? AND trim(e.notes) != '' AND e.notes NOT LIKE ?
            UNION ALL
            SELECT 'income', i.notes, c.name, date(i.received_at), i.id
            FROM income_entries i JOIN income_categories c ON c.id = i.category_id
            WHERE i.user_id = ? AND trim(i.notes) != ''
        )
        ORDER BY day DESC, id DESC
        LIMIT ?
        """,
        (int(user_id), SHARED_NOTE_PREFIX + "%", int(user_id), limit),
    ).fetchall()
    counts: dict[tuple[str, str], Counter] = {}
    first_note: dict[tuple[str, str], str] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        k = (r["kind"], key(r["notes"]))
        if not k[1]:
            continue
        if k not in counts:
            counts[k] = Counter()
            first_note[k] = " ".join(str(r["notes"]).split())[:40]
            order.append(k)
        counts[k][r["category"]] += 1
    hist = History()
    for k in order:
        category = counts[k].most_common(1)[0][0]
        hist.lookup[k] = category
        if len(hist.examples) < HISTORY_EXAMPLES:
            hist.examples.append((first_note[k], category, k[0]))
    return hist


# --- descriptions ------------------------------------------------------------------

_FILLER = frozenset({
    "se", "sto", "sti", "stin", "ston", "stis", "stus", "ta", "to", "ti", "tin", "ton", "gia", "apo",
    "me", "ke", "kai", "the", "at", "for", "on", "in", "to", "of", "and", "euro", "euros", "eur",
    "eiro", "iro", "ero", "e", "simera", "today", "xthes", "xtes", "exthes", "yesterday", "proxthes",
    "proxtes", "plirosa", "edosa", "pliromi", "paid", "spent", "xalasa", "xrimata", "lefta",
})


def describe(text: str, skip_words: set[str] | None = None) -> str:
    """The message minus amounts, dates, currency, account names and filler,
    in the user's own spelling: "χθες καφές 3.50 revolut" → "καφές"."""
    skip = set(skip_words or ())
    cut = sorted([(a.start, a.end) for a in find_amounts(text)] + _date_spans(text))
    pieces, pos = [], 0
    for start, end in cut:
        pieces.append(text[pos:start])
        pos = max(pos, end)
    pieces.append(text[pos:])
    kept = []
    for raw in " ".join(pieces).split():
        word = raw.strip(".,;:!?()[]{}\"'«»€$")
        if not word:
            continue
        folded = [t for t in tokens(word) if not t.isdigit()]
        if not folded or all(t in _FILLER or t in skip for t in folded):
            continue
        kept.append(word)
    return " ".join(kept)[:60]


# --- transactions ------------------------------------------------------------------


@dataclass
class Txn:
    type: str  # "expense" | "income"
    amount: float
    category: str
    notes: str
    date: str  # YYYY-MM-DD; for a repeating one, the first entry
    account_id: int | None = None
    source: str = "ai"  # "ai" | "basic" | "receipt"
    repeat: Repeat | None = None  # set when the message says it repeats


REPEAT_UNITS = ("week", "month", "year")
MAX_REPEAT_EVERY = 99
MAX_REPEAT_TIMES = 1000
_UNIT_ALIASES = {"weekly": "week", "monthly": "month", "yearly": "year", "annual": "year", "annually": "year"}


@dataclass
class Repeat:
    """Every `every` weeks/months/years, `times` entries in all (None: no end)."""

    unit: str
    every: int = 1
    times: int | None = None


def _positive_int(raw) -> int | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return int(value) if value == int(value) and value >= 1 else None


def coerce_repeat(raw) -> Repeat | None:
    """The AI's repeat object, or None when it is missing, malformed or means
    a single entry anyway."""
    if not isinstance(raw, dict):
        return None
    unit = str(raw.get("unit") or "").strip().lower()
    unit = _UNIT_ALIASES.get(unit, unit.rstrip("s"))
    if unit not in REPEAT_UNITS:
        return None
    every = _positive_int(raw.get("every") if raw.get("every") not in (None, "") else 1)
    if every is None or every > MAX_REPEAT_EVERY:
        return None
    times = None
    if raw.get("times") not in (None, "", 0, "0"):
        times = _positive_int(raw.get("times"))
        if times is None or times > MAX_REPEAT_TIMES:
            return None
        if times == 1:
            return None
    return Repeat(unit, every, times)


@dataclass
class Context:
    """What the parser knows about the user."""

    expense_categories: list[str]
    income_categories: list[str]
    accounts: list[dict]  # [{id, name}] excluding the Shared balances ledger
    history: History
    ai_settings: dict
    today: date = field(default_factory=lambda: datetime.now().date())

    def categories(self, kind: str) -> list[str]:
        return self.income_categories if kind == "income" else self.expense_categories


def _is_income(text: str) -> bool:
    folded = fold(text)
    return any(w.startswith(_INCOME_PREFIXES) for w in tokens(text)) or "pliromi apo" in folded


def _segments(text: str, amounts: list[Amount]) -> list[tuple[str, Amount]]:
    """Split "καφές 3, βενζίνη 40" (or "3 καφές 40 βενζίνη") into one piece of
    text per amount."""
    if len(amounts) == 1:
        return [(text, amounts[0])]
    amount_first = not text[: amounts[0].start].strip(" ,;+\n")
    out = []
    for i, a in enumerate(amounts):
        if amount_first:
            end = amounts[i + 1].start if i + 1 < len(amounts) else len(text)
            out.append((text[a.start:end], a))
        else:
            start = amounts[i - 1].end if i else 0
            out.append((text[start:a.end], a))
    return out


def _choose_category(ctx: Context, kind: str, description: str, suggested: str | None) -> str:
    """The user's past choice, then the AI's, then a category named in the
    message, then a known kind of spending, then the user's catch-all."""
    options = ctx.categories(kind)
    from_history = ctx.history.category_for(kind, description, options)
    if from_history:
        return from_history
    if suggested:
        resolved = resolve_category(suggested, options)
        if not is_fallback(resolved):
            return resolved
    return (
        category_named_in(description, options)
        or keyword_category(description, options, kind)
        or find_fallback_category(options)
    )


def _with_tags(txns: list[Txn], found: list[str]) -> list[Txn]:
    for t in txns:
        t.notes = tags.add_tags(t.notes, found)
    return txns


def basic_parse(text: str, ctx: Context) -> list[Txn]:
    """Rule-based parse used when the AI is off, unreachable or unusable."""
    found = tags.find_tags(text)
    text = tags.strip_tags(text)
    amounts = find_amounts(text)
    if not amounts:
        return []
    account_id, account_words = detect_account(text, ctx.accounts)
    whole_date = detect_date(text, ctx.today) or ctx.today
    out = []
    for segment, amount in _segments(text, amounts):
        kind = "income" if _is_income(segment) else "expense"
        notes = describe(segment, account_words)
        seg_date = detect_date(segment, ctx.today) or whole_date
        out.append(Txn(
            type=kind,
            amount=round(amount.value, 2),
            category=_choose_category(ctx, kind, notes, None),
            notes=notes,
            date=seg_date.isoformat(),
            account_id=account_id,
            source="basic",
        ))
    return _with_tags(out, found)


def _ai_ready(ai: dict) -> bool:
    return bool(ai.get("ai_enabled") and (ai.get("ai_base_url") or "").strip() and (ai.get("ai_model") or "").strip())


def _ai_call(ctx: Context, prompt: str, *, timeout: int | None = None) -> dict | None:
    ai = ctx.ai_settings
    if not _ai_ready(ai):
        return None
    raw = integrations.ollama_chat(
        ai["ai_base_url"].strip(),
        ai["ai_model"].strip(),
        prompt,
        timeout=timeout or int(ai.get("ai_timeout") or integrations.DEFAULT_AI_TIMEOUT),
        json_mode=True,
        options={"temperature": 0},
    )
    return integrations.parse_json_text(raw) if raw else None


def _history_block(ctx: Context) -> str:
    if not ctx.history.examples:
        return ""
    lines = [f'- "{note}" → {cat}' + (" (income)" if kind == "income" else "") for note, cat, kind in ctx.history.examples]
    return "Past entries by this user (description → category). Follow these whenever the message is about the same thing:\n" + "\n".join(lines) + "\n"


def _dates_block(today: date) -> str:
    return (
        f"Today is {today.isoformat()} ({today.strftime('%A')}). "
        f"χθες/yesterday = {(today - timedelta(days=1)).isoformat()}, "
        f"προχθές/day before yesterday = {(today - timedelta(days=2)).isoformat()}."
    )


def _record_prompt(text: str, ctx: Context) -> str:
    expense = [c for c in ctx.expense_categories if not is_fallback(c)]
    income = [c for c in ctx.income_categories if not is_fallback(c)]
    hints = category_hints(ctx.expense_categories, ctx.income_categories)
    hint_line = ("Typical words: " + "; ".join(hints) + ".\n") if hints else ""
    return f"""You turn short personal-finance messages (Greek, Greeklish or English) into JSON.
{_dates_block(ctx.today)}

Expense categories: {json.dumps(expense, ensure_ascii=False)}
Income categories: {json.dumps(income, ensure_ascii=False)}
Account names (never part of notes): {json.dumps([a["name"] for a in ctx.accounts], ensure_ascii=False)}
{hint_line}{_history_block(ctx)}
Rules:
- A message can hold several transactions ("καφές 3, βενζίνη 40" is two). Return each one.
- type: "income" only for money received (salary/μισθός/misthos, bonus, money someone paid you); otherwise "expense".
- amount: the positive number written for that transaction, no currency.
- category: copy one name exactly from the list for its type; if nothing fits use "Other".
- notes: a short description in the user's own words (e.g. "καφές"), without amount, date, account or how often it repeats.
- date: YYYY-MM-DD; today unless the message says otherwise. For a repeating transaction, the date of its first entry (today unless the message names a day, e.g. "every 1st" means the next 1st).
- repeat: null for a one-off transaction. Only when the message says the transaction repeats or is recurring (in any wording or language), give {{"every": N, "unit": "week" | "month" | "year", "times": T}}: every N weeks/months/years, T entries in all. T is null unless the message itself gives a duration or a count; "every month" alone is {{"every": 1, "unit": "month", "times": null}}, never a guessed 12. A duration becomes a count of entries: "for 3 months" monthly is times 3, "for a year" monthly is times 12, "for 6 weeks" weekly is times 6. Numbers counting months, weeks, years or times are never transaction amounts.

Message: {json.dumps(text, ensure_ascii=False)}

Return only JSON: {{"transactions": [{{"type": "expense", "amount": 0, "category": "", "notes": "", "date": "YYYY-MM-DD", "repeat": null}}]}}"""


def _ai_items(parsed: dict | None) -> list[dict]:
    if not isinstance(parsed, dict):
        return []
    items = parsed.get("transactions")
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]
    if "amount" in parsed:
        return [parsed]
    return []


def _coerce_date(raw, default: date, today: date, max_age_days: int = 366, max_ahead_days: int = 1) -> date:
    text = str(raw or "").strip()[:10]
    try:
        found = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return default
    if found > today + timedelta(days=max_ahead_days) or found < today - timedelta(days=max_age_days):
        return default
    return found


def parse_transactions(text: str, ctx: Context) -> list[Txn]:
    """Transactions in a text message; [] when it holds no amount at all.
    The AI's answer is only kept if it is consistent with the message (no more
    transactions than amounts written, each one an amount written); otherwise
    the basic parser is used."""
    found = tags.find_tags(text)
    text = tags.strip_tags(text)
    amounts = find_amounts(text)
    if not amounts:
        return []
    written = {round(a.value, 2) for a in amounts}
    account_id, account_words = detect_account(text, ctx.accounts)
    explicit_date = detect_date(text, ctx.today)
    items = _ai_items(_ai_call(ctx, _record_prompt(text, ctx)))
    out: list[Txn] = []
    for item in items[: len(amounts)]:
        kind = str(item.get("type", "")).strip().lower()
        if kind not in ("expense", "income"):
            kind = "income" if _is_income(text) else "expense"
        if len(amounts) == 1:
            # The number written beats the AI's reading of it ("12,99" → 1299).
            value = amounts[0].value
        else:
            try:
                value = round(abs(float(str(item.get("amount")).replace(",", "."))), 2)
            except (TypeError, ValueError):
                continue
            if value not in written:
                continue
        notes = describe(str(item.get("notes") or ""), account_words)
        if not notes and len(amounts) == 1:
            notes = describe(text, account_words)
        repeat = coerce_repeat(item.get("repeat"))
        # A repeating entry may start later, e.g. "rent on the 1st of every month".
        ahead = 366 if repeat else 1
        if explicit_date and len(amounts) == 1:
            when = explicit_date
        else:
            when = _coerce_date(item.get("date"), explicit_date or ctx.today, ctx.today, max_ahead_days=ahead)
        lookup_text = describe(text, account_words) if len(amounts) == 1 else notes
        category = _choose_category(ctx, kind, lookup_text, str(item.get("category") or ""))
        out.append(Txn(kind, round(value, 2), category, notes, when.isoformat(), account_id, "ai", repeat))
    return _with_tags(out or basic_parse(text, ctx), found)


# --- shared bills ------------------------------------------------------------------

_SHARED_PREFIXES = ("miras", "misa", "split", "shared", "kino", "koin", "mazi")
_OTHER_PAID = frozenset({"plirose", "eplirose", "plironi", "kerase", "ekerase", "paid", "pays", "pagare"})


@dataclass
class SharedPlan:
    group_id: int
    group_name: str
    paid_by: int  # person id
    participants: list[int]  # person ids sharing the bill
    payer_is_me: bool
    skip_words: set[str] = field(default_factory=set)  # message words about the sharing itself

    def description(self, notes: str) -> str:
        """notes without "μοιρασμένο", "πλήρωσε", member or group names."""
        return describe(notes, self.skip_words)


def detect_shared(text: str, groups: list[dict]) -> SharedPlan | str | None:
    """A shared-bill plan when the message says so ("μοιρασμένο", "split",
    "μισά", or names one of the user's groups); a message string when it does
    but no group fits; None for an ordinary expense.

    groups: [{id, name, me: person_id, members: [{id, names: [..]}]}]"""
    words = [t for t in tokens(text) if not t.isdigit()]
    keyword = any(w.startswith(_SHARED_PREFIXES) for w in words)
    named_group = None
    for g in groups:
        gk = tokens(g["name"])
        if gk and all(any(_similar(w, part) for w in words) for part in gk):
            named_group = g
            break
    if not keyword and not named_group:
        return None
    if not groups:
        return "You have no shared groups yet. Create one in the app under Shared."

    def mentioned(g):
        found = []
        for m in g["members"]:
            if m["id"] == g["me"]:
                continue
            if any(_similar(w, part) for n in m["names"] for part in tokens(n) for w in words if len(w) >= 3):
                found.append(m["id"])
        return found

    group = named_group
    if not group:
        scored = sorted(groups, key=lambda g: len(mentioned(g)), reverse=True)
        if len(groups) == 1:
            group = groups[0]
        elif mentioned(scored[0]) and len(mentioned(scored[0])) > len(mentioned(scored[1])):
            group = scored[0]
        else:
            names = ", ".join(g["name"] for g in groups)
            return f"Which group? Add its name to the message ({names})."
    people = mentioned(group)
    paid_by = group["me"]
    if people and any(w in _OTHER_PAID for w in words):
        # "η Άννα πλήρωσε 40" → Anna paid; "πλήρωσα" (I paid) keeps me as payer.
        paid_by = people[0]
    if people and not (named_group and not keyword):
        participants = sorted(set(people) | {group["me"], paid_by})
    else:
        participants = [m["id"] for m in group["members"]]
    names = [n for m in group["members"] for n in m["names"]] + [group["name"]]
    skip = {
        w for w in words
        if w.startswith(_SHARED_PREFIXES) or w in _OTHER_PAID
        or any(_similar(w, part) for n in names for part in tokens(n))
    }
    return SharedPlan(group["id"], group["name"], paid_by, participants, paid_by == group["me"], skip)


# --- receipts --------------------------------------------------------------------

_TOTAL_WORDS = ("σύνολο", "total", "πληρωτέο", "plhpote", "γενικό", "amount due", "προς πληρωμή")
_TOTAL_RE = re.compile("|".join(re.escape(fold(w)) for w in _TOTAL_WORDS))


def receipt_total(ocr_text: str) -> float | None:
    """The amount on the last 'total' line of a receipt (ΣΥΝΟΛΟ / TOTAL /
    ΠΛΗΡΩΤΕΟ), or None."""
    best = None
    for line in (ocr_text or "").splitlines():
        if _TOTAL_RE.search(fold(line)) and fold("φπα") not in fold(line):
            amounts = find_amounts(line)
            if amounts:
                best = amounts[-1].value
    return best


def receipt_date(ocr_text: str, today: date) -> date | None:
    found = detect_date(ocr_text, today)
    if found and today - timedelta(days=RECEIPT_MAX_AGE_DAYS) <= found <= today:
        return found
    return None


def parse_receipt(ocr_text: str, caption: str, ctx: Context) -> Txn | None:
    """One expense from a receipt's OCR text. Total and date are read from the
    text directly when possible; the AI names the shop and picks a category."""
    total = receipt_total(ocr_text)
    when = receipt_date(ocr_text, ctx.today)
    expense = [c for c in ctx.expense_categories if not is_fallback(c)]
    prompt = f"""This is the OCR text of a shop receipt (often Greek; letters may be garbled).
{_dates_block(ctx.today)}
Expense categories: {json.dumps(expense, ensure_ascii=False)}
Supermarkets (Σκλαβενίτης, ΑΒ Βασιλόπουλος, Μασούτης, Lidl, Κρητικός, My Market) go under the groceries/supermarket category if there is one.
{_history_block(ctx)}
Receipt text:
\"\"\"
{ocr_text[:3000]}
\"\"\"
User caption: {json.dumps(caption or "", ensure_ascii=False)}

Return only JSON: {{"merchant": "shop name, cleaned up", "total": 0, "date": "YYYY-MM-DD or null", "category": "one name from the list, or Other"}}"""
    parsed = _ai_call(ctx, prompt, timeout=max(int(ctx.ai_settings.get("ai_timeout") or 30), 60)) or {}
    if total is None:
        try:
            total = abs(float(str(parsed.get("total")).replace(",", ".")))
        except (TypeError, ValueError):
            total = None
    if not total or not 0 < total < MAX_AMOUNT:
        return None
    if when is None and parsed.get("date"):
        candidate = _coerce_date(parsed.get("date"), ctx.today, ctx.today)
        if ctx.today - timedelta(days=RECEIPT_MAX_AGE_DAYS) <= candidate:
            when = candidate
    merchant = " ".join(str(parsed.get("merchant") or "").split())[:60]
    if not merchant:
        first = next((ln.strip() for ln in ocr_text.splitlines() if re.search(r"[^\W\d_]{3}", ln)), "")
        merchant = first[:60]
    caption_tags = tags.find_tags(caption or "")
    caption = tags.strip_tags(caption or "")
    caption_notes = describe(caption)
    notes = tags.add_tags(merchant or caption_notes or "Receipt", caption_tags)
    lookup = merchant + " " + caption_notes
    # The shop name on the receipt (e.g. a supermarket chain) is a stronger
    # signal than the AI's guess from the item list.
    head = " ".join(ocr_text.splitlines()[:4])
    shop_category = keyword_category(f"{merchant} {head} {caption_notes}", ctx.expense_categories, "expense")
    category = ctx.history.category_for("expense", merchant, ctx.expense_categories) or shop_category
    category = category or _choose_category(ctx, "expense", lookup, str(parsed.get("category") or ""))
    if is_fallback(category) and caption_notes:
        category = _choose_category(ctx, "expense", caption_notes, None)
    account_id, _ = detect_account(caption or "", ctx.accounts)
    return Txn("expense", round(total, 2), category, notes, (when or ctx.today).isoformat(), account_id, "receipt")


# --- questions -------------------------------------------------------------------

_QUESTION_STARTS = frozenset({
    "posa", "poso", "pso", "ti", "pu", "pia", "pio", "pies", "pii", "pote", "how", "what", "where",
    "which", "when", "show", "dixe", "dikse", "list", "ipolipo", "ipolipa", "balance", "balances",
    "sinolika", "xrostao", "xrostas", "xrostai", "who", "pios",
})


def looks_like_question(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith(("?", ";", ";")):
        return True
    words = tokens(stripped)
    if words and words[0] in _QUESTION_STARTS:
        return True
    return not find_amounts(stripped)


def period_presets(today: date) -> dict[str, tuple[date, date]]:
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())
    return {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "this_week": (week_start, today),
        "last_week": (week_start - timedelta(days=7), week_start - timedelta(days=1)),
        "this_month": (month_start, today),
        "last_month": (last_month_end.replace(day=1), last_month_end),
        "last_30_days": (today - timedelta(days=29), today),
        "this_year": (today.replace(month=1, day=1), today),
        "last_year": (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)),
    }


METRICS = ("spent", "income", "net", "balance", "breakdown", "recent", "shared")


def plan_question(text: str, ctx: Context, group_names: list[str]) -> dict:
    """{metric, category, account, from, to, search} for a question. The AI
    only plans; the numbers come from the database."""
    presets = period_presets(ctx.today)
    preset_lines = "\n".join(f"- {k}: {a.isoformat()} to {b.isoformat()}" for k, (a, b) in presets.items())
    prompt = f"""You plan a database lookup for a question about the user's personal finances.
{_dates_block(ctx.today)}
Periods:
{preset_lines}
Expense categories: {json.dumps(ctx.expense_categories, ensure_ascii=False)}
Income categories: {json.dumps(ctx.income_categories, ensure_ascii=False)}
Accounts: {json.dumps([a["name"] for a in ctx.accounts], ensure_ascii=False)}
Shared groups: {json.dumps(group_names, ensure_ascii=False)}

metric is one of:
- "spent": total spending (optionally one category, account or description search)
- "income": total income (βγάλαμε/πήρα/κέρδισα/έσοδα = income)
- "net": income minus spending
- "balance": current account balances
- "breakdown": spending per category
- "recent": list the latest transactions
- "shared": who owes whom in shared groups
category/account: copy a name exactly from the lists, or null. search: a word to look for in descriptions (e.g. "efood"), or null.
from/to: YYYY-MM-DD; this month when the question gives no period.

Question: {json.dumps(text, ensure_ascii=False)}

Return only JSON: {{"metric": "spent", "category": null, "account": null, "search": null, "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}}"""
    parsed = _ai_call(ctx, prompt) or {}
    plan = _heuristic_plan(text, ctx)
    metric = str(parsed.get("metric") or "").strip().lower()
    if metric in METRICS:
        plan["metric"] = metric
        plan["source"] = "ai"
    for field_name, options in (("category", ctx.expense_categories + ctx.income_categories), ("account", [a["name"] for a in ctx.accounts])):
        value = parsed.get(field_name)
        if value:
            match = best_name_match(str(value), options)
            if match:
                plan[field_name] = match
    if parsed.get("search"):
        plan["search"] = str(parsed["search"]).strip()[:40] or None
    start = _coerce_date(parsed.get("from"), plan["from"], ctx.today, max_age_days=3660)
    end = _coerce_date(parsed.get("to"), plan["to"], ctx.today, max_age_days=3660)
    if parsed.get("from") and parsed.get("to"):
        plan["from"], plan["to"] = min(start, end), max(start, end)
    return plan


def _heuristic_plan(text: str, ctx: Context) -> dict:
    words = tokens(text)
    joined = " ".join(words)

    def has(*prefixes):
        return any(w.startswith(_fw(*prefixes)) for w in words)

    presets = period_presets(ctx.today)
    period = presets["this_month"]
    if (has("προηγούμεν") and has("μήνα")) or fold("last month") in joined:
        period = presets["last_month"]
    elif has("εβδομάδ", "week"):
        period = presets["this_week"]
    elif has("χρονιά", "έτος", "year"):
        period = presets["this_year"]
    elif set(words) & set(_fw("σήμερα", "today")):
        period = presets["today"]
    elif set(words) & _YESTERDAY:
        period = presets["yesterday"]
    metric = "breakdown"
    if has("υπόλοιπ", "balanc", "διαθέσιμ"):
        metric = "balance"
    elif has("χρωστ", "owe", "χρει"):
        metric = "shared"
    elif has("έσοδα", "είσπραξ", "income", "μισθ"):
        metric = "income"
    elif has("τελευτα", "recent", "last", "κινήσεις"):
        metric = "recent"
    elif has("ξόδεψ", "ξοδέψ", "χάλασ", "έδωσα", "spent", "spend", "πλήρωσα"):
        metric = "spent"
    category = None
    for w in words:
        for c in ctx.expense_categories:
            if len(w) >= 4 and _similar(w, key(c).replace(" ", "")):
                category = c
    account_id, _ = detect_account(text, ctx.accounts)
    account = next((a["name"] for a in ctx.accounts if a["id"] == account_id), None)
    return {"metric": metric, "category": category, "account": account, "search": None,
            "from": period[0], "to": period[1], "source": "basic"}


# Word lists above are partly written in natural spelling; fold them once so
# they compare equal to folded user text.
_ACCOUNT_STOPWORDS = frozenset(_fw(*_ACCOUNT_STOPWORDS, "κάρτα", "card", "credit", "account", "λογαριασμός", "τράπεζα", "balances"))
_CASH_WORDS = frozenset(_fw(*_CASH_WORDS, "μετρητά", "cash", "χαρτονόμισμα"))
_INCOME_PREFIXES = _fw(*_INCOME_PREFIXES, "μισθ", "salary", "είσπραξ")
_FILLER = frozenset(_fw(*_FILLER, "η", "ο", "οι", "του", "της", "των", "και", "ευρώ", "σήμερα", "χθες", "προχθές", "πλήρωσα", "έδωσα", "πληρωμή", "λεφτά"))
_SHARED_PREFIXES = _fw(*_SHARED_PREFIXES, "μοιρασ", "μισά", "κοινό", "μαζί")
_OTHER_PAID = frozenset(_fw(*_OTHER_PAID, "πλήρωσε", "επλήρωσε", "πληρώνει", "κέρασε"))
_QUESTION_STARTS = frozenset(_fw(*_QUESTION_STARTS, "πόσα", "πόσο", "τι", "πού", "ποια", "ποιο", "ποιες", "ποιοι",
                                 "πότε", "δείξε", "υπόλοιπο", "υπόλοιπα", "συνολικά", "χρωστάω", "χρωστάς", "χρωστάει", "ποιος"))
