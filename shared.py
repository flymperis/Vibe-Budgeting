"""Shared expenses: groups of people splitting bills, Splitwise-style.

People and users
----------------
A person is a name in one user's contact list. A person can also be linked
to another Vibe Budgeting user on this server; every user has a person for
themselves (is_self), linked to their own login. A group is created by one
user (its owner) and its members are people from anyone's contact list.

A linked member is first *invited*; once they accept, the group is fully
shared with them: they see the same bills, balances and payments, and can add
and edit them like the owner. Unlinked people are just names that the active
members manage on their behalf. Anyone can be a payer or share in a bill
whatever their status, so a bill can be entered before a friend accepts.

Money is held in integer cents so splits always add back up to the bill.

Links into each user's own books
--------------------------------
Every user decides for themselves whether a bill or payment that involves
them is recorded in their own accounts (a row in shared_expense_links /
shared_settlement_links holds that choice and their category/account):

* Their share of a bill is a normal expense in their chosen category, so their
  budget counts what they consumed, not what they fronted.
* Money they front for others moves from the paying account into their
  "Shared balances" account, which therefore always shows what friends owe
  them (positive) or what they owe (negative).
* A share someone else paid is an expense drawn on that same account, and
  settling up is a plain transfer between it and a real account.

The linked rows are rebuilt for every recording user whenever a bill changes,
whoever changed it, and deleted with it.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

SPLIT_METHODS = ("equal", "exact", "percent", "shares")
SPLIT_METHOD_LABELS = {
    "equal": "Equally",
    "exact": "Exact amounts",
    "percent": "Percentages",
    "shares": "Shares",
}
SELF_NAME = "You"
LEDGER_ACCOUNT_NAME = "Shared balances"
NAME_MAX_LEN = 60
NOTE_PREFIX = "[Shared]"


def migrate_shared(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shared_people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL COLLATE NOCASE,
            is_self INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0, 1)),
            linked_user_id INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, name)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_people_one_self
            ON shared_people(user_id) WHERE is_self = 1;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_people_linked
            ON shared_people(user_id, linked_user_id) WHERE linked_user_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_shared_people_linked_user ON shared_people(linked_user_id);

        CREATE TABLE IF NOT EXISTS shared_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS shared_group_members (
            group_id INTEGER NOT NULL REFERENCES shared_groups(id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES shared_people(id),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'invited', 'declined')),
            PRIMARY KEY (group_id, person_id)
        );

        CREATE TABLE IF NOT EXISTS shared_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            group_id INTEGER NOT NULL REFERENCES shared_groups(id) ON DELETE CASCADE,
            description TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            paid_by INTEGER NOT NULL REFERENCES shared_people(id),
            split_method TEXT NOT NULL CHECK (split_method IN ('equal', 'exact', 'percent', 'shares')),
            spent_on TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS shared_expense_splits (
            expense_id INTEGER NOT NULL REFERENCES shared_expenses(id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES shared_people(id),
            share_cents INTEGER NOT NULL CHECK (share_cents >= 0),
            input_value TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (expense_id, person_id)
        );

        CREATE TABLE IF NOT EXISTS shared_expense_links (
            expense_id INTEGER NOT NULL REFERENCES shared_expenses(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id),
            category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            linked_expense_id INTEGER REFERENCES expenses(id) ON DELETE SET NULL,
            linked_transfer_id INTEGER REFERENCES account_transfers(id) ON DELETE SET NULL,
            PRIMARY KEY (expense_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS shared_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            group_id INTEGER NOT NULL REFERENCES shared_groups(id) ON DELETE CASCADE,
            from_person_id INTEGER NOT NULL REFERENCES shared_people(id),
            to_person_id INTEGER NOT NULL REFERENCES shared_people(id),
            amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
            paid_on TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (from_person_id != to_person_id)
        );

        CREATE TABLE IF NOT EXISTS shared_settlement_links (
            settlement_id INTEGER NOT NULL REFERENCES shared_settlements(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id),
            account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
            linked_transfer_id INTEGER REFERENCES account_transfers(id) ON DELETE SET NULL,
            PRIMARY KEY (settlement_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS shared_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id),
            ledger_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_shared_members_person ON shared_group_members(person_id);
        CREATE INDEX IF NOT EXISTS idx_shared_expenses_group ON shared_expenses(group_id, spent_on);
        CREATE INDEX IF NOT EXISTS idx_shared_settlements_group ON shared_settlements(group_id, paid_on);
        """
    )


# --- money -----------------------------------------------------------------


def parse_cents(raw):
    """'12.5' / '12,50' -> 1250. Raises ValueError on anything that is not a
    plain non-negative number."""
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        raise ValueError("empty amount")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("not a number") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("negative amount")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_weight(raw):
    """A percentage or share count as a Decimal, or None when blank."""
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("not a number") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("negative value")
    return value


def cents_to_amount(cents):
    return round(int(cents) / 100.0, 2)


def _euro(cents):
    """Plain '12.50 €' for flash messages (the templates use the money filter)."""
    return f"{cents / 100:.2f} €"


def allocate(total_cents, weights):
    """Split total_cents in proportion to weights so the parts add up exactly.

    Largest-remainder rounding: everyone gets the floor of their exact part and
    the leftover cents go to the largest fractions, earlier entries first on a
    tie — so an equal split of 10.00 three ways is 3.34 / 3.33 / 3.33.
    """
    weights = [Decimal(w) for w in weights]
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must add up to more than zero")
    exact = [Decimal(total_cents) * w / total_weight for w in weights]
    parts = [int(x) for x in exact]
    leftover = total_cents - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in order[:leftover]:
        parts[i] += 1
    return parts


def compute_splits(method, total_cents, entries):
    """Work out who owes what for one bill.

    entries: [(person_id, included, raw_value)] for every group member, in
    display order. Returns [(person_id, share_cents, input_value)] and raises
    ValueError with a user-facing message when the split does not work.
    """
    if method not in SPLIT_METHODS:
        raise ValueError("Choose how to split the bill.")
    if total_cents <= 0:
        raise ValueError("Enter an amount above zero.")

    if method == "equal":
        people = [pid for pid, included, _ in entries if included]
        if not people:
            raise ValueError("Pick at least one person to split with.")
        parts = allocate(total_cents, [1] * len(people))
        return [(pid, part, "") for pid, part in zip(people, parts)]

    if method == "exact":
        result = []
        for pid, _included, raw in entries:
            if not str(raw or "").strip():
                continue
            try:
                cents = parse_cents(raw)
            except ValueError as exc:
                raise ValueError("Each person's amount must be a number of zero or more.") from exc
            if cents > 0:
                result.append((pid, cents, f"{cents / 100:.2f}"))
        assigned = sum(c for _, c, _ in result)
        if assigned != total_cents:
            raise ValueError(
                f"The amounts add up to {_euro(assigned)}, not {_euro(total_cents)}."
            )
        return result

    weighted = []
    for pid, _included, raw in entries:
        try:
            weight = parse_weight(raw)
        except ValueError as exc:
            kind = "percentage" if method == "percent" else "number of shares"
            raise ValueError(f"Each person's {kind} must be a number of zero or more.") from exc
        if weight is not None and weight > 0:
            weighted.append((pid, weight))
    if not weighted:
        raise ValueError("Give at least one person a share of the bill.")
    if method == "percent":
        total_pct = sum(w for _, w in weighted)
        if abs(total_pct - 100) > Decimal("0.01"):
            raise ValueError(f"The percentages add up to {total_pct.normalize():f}%, not 100%.")
    parts = allocate(total_cents, [w for _, w in weighted])
    return [(pid, part, f"{w.normalize():f}") for (pid, w), part in zip(weighted, parts)]


def simplify_debts(net):
    """Fewest payments that settle everyone: net is {person_id: cents}, positive
    when the group owes that person. Returns [(from_id, to_id, cents)].

    Greedy biggest-debtor-pays-biggest-creditor; for n people this needs at
    most n-1 payments, and ties are broken by id so the list is stable.
    """
    debtors = {pid: -v for pid, v in net.items() if v < 0}
    creditors = {pid: v for pid, v in net.items() if v > 0}
    payments = []
    while debtors and creditors:
        d = max(debtors, key=lambda p: (debtors[p], -p))
        c = max(creditors, key=lambda p: (creditors[p], -p))
        amount = min(debtors[d], creditors[c])
        payments.append((d, c, amount))
        debtors[d] -= amount
        creditors[c] -= amount
        if debtors[d] == 0:
            del debtors[d]
        if creditors[c] == 0:
            del creditors[c]
    return payments


# --- people ------------------------------------------------------------------


def clean_name(raw):
    return " ".join(str(raw or "").split())[:NAME_MAX_LEN]


def ensure_self(conn, user_id):
    """The user's own person row (linked to their login), created on first use."""
    uid = int(user_id)
    row = conn.execute(
        "SELECT id, linked_user_id FROM shared_people WHERE user_id = ? AND is_self = 1", (uid,)
    ).fetchone()
    if row:
        if row["linked_user_id"] != uid:
            conn.execute("UPDATE shared_people SET linked_user_id = ? WHERE id = ?", (uid, row["id"]))
        return int(row["id"])
    clash = conn.execute(
        "SELECT id FROM shared_people WHERE user_id = ? AND name = ?", (uid, SELF_NAME)
    ).fetchone()
    if clash:
        conn.execute(
            "UPDATE shared_people SET is_self = 1, linked_user_id = ? WHERE id = ?", (uid, clash["id"])
        )
        return int(clash["id"])
    cur = conn.execute(
        "INSERT INTO shared_people (user_id, name, is_self, linked_user_id) VALUES (?, ?, 1, ?)",
        (uid, SELF_NAME, uid),
    )
    return int(cur.lastrowid)


def find_or_create_person(conn, user_id, name):
    """Person id in this user's contacts for a name (case-insensitive)."""
    uid = int(user_id)
    name = clean_name(name)
    if not name:
        raise ValueError("Enter a name.")
    row = conn.execute(
        "SELECT id FROM shared_people WHERE user_id = ? AND name = ?", (uid, name)
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute("INSERT INTO shared_people (user_id, name) VALUES (?, ?)", (uid, name))
    return int(cur.lastrowid)


def person_owned(conn, user_id, person_id):
    return conn.execute(
        "SELECT 1 FROM shared_people WHERE id = ? AND user_id = ?", (int(person_id), int(user_id))
    ).fetchone() is not None


def find_user(conn, username):
    """The user id for a login name, or None."""
    name = str(username or "").strip().lstrip("@")
    if not name:
        return None
    row = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
    return int(row["id"]) if row else None


def _unique_contact_name(conn, uid, base):
    name, n = base, 2
    while conn.execute(
        "SELECT 1 FROM shared_people WHERE user_id = ? AND name = ?", (uid, name)
    ).fetchone():
        name = f"{base} ({n})"
        n += 1
    return name


def contact_for_username(conn, user_id, username):
    """A person in this user's contacts linked to the named server user,
    creating or linking one as needed. Raises ValueError for unknown users."""
    uid = int(user_id)
    target = find_user(conn, username)
    if target is None:
        raise ValueError(f"There is no user called “{clean_name(username)}” on this server.")
    if target == uid:
        return ensure_self(conn, uid)
    row = conn.execute(
        "SELECT id FROM shared_people WHERE user_id = ? AND linked_user_id = ?", (uid, target)
    ).fetchone()
    if row:
        return int(row["id"])
    login = conn.execute("SELECT username FROM users WHERE id = ?", (target,)).fetchone()["username"]
    same_name = conn.execute(
        "SELECT id, linked_user_id, is_self FROM shared_people WHERE user_id = ? AND name = ?",
        (uid, login),
    ).fetchone()
    if same_name and same_name["linked_user_id"] is None and not same_name["is_self"]:
        link_contact(conn, uid, int(same_name["id"]), login)
        return int(same_name["id"])
    cur = conn.execute(
        "INSERT INTO shared_people (user_id, name, linked_user_id) VALUES (?, ?, ?)",
        (uid, _unique_contact_name(conn, uid, login), target),
    )
    return int(cur.lastrowid)


def link_contact(conn, user_id, person_id, username):
    """Tie one of this user's contacts to a server login. Groups the contact
    is already in turn into invitations for that user."""
    uid, pid = int(user_id), int(person_id)
    person = conn.execute(
        "SELECT is_self, linked_user_id FROM shared_people WHERE id = ? AND user_id = ?", (pid, uid)
    ).fetchone()
    if not person or person["is_self"]:
        raise ValueError("That person can't be linked.")
    target = find_user(conn, username)
    if target is None:
        raise ValueError(f"There is no user called “{clean_name(username)}” on this server.")
    if target == uid:
        raise ValueError("That's your own login.")
    if person["linked_user_id"] == target:
        return
    if person["linked_user_id"] is not None:
        raise ValueError("Unlink them first.")
    other = conn.execute(
        "SELECT name FROM shared_people WHERE user_id = ? AND linked_user_id = ?", (uid, target)
    ).fetchone()
    if other:
        raise ValueError(f"That user is already linked to {other['name']}.")
    clash = conn.execute(
        """
        SELECT 1 FROM shared_group_members m
        JOIN shared_group_members m2 ON m2.group_id = m.group_id
        JOIN shared_people p2 ON p2.id = m2.person_id
        WHERE m.person_id = ? AND p2.linked_user_id = ?
        LIMIT 1
        """,
        (pid, target),
    ).fetchone()
    if clash:
        raise ValueError("That user is already in one of this person's groups.")
    conn.execute("UPDATE shared_people SET linked_user_id = ? WHERE id = ?", (target, pid))
    conn.execute("UPDATE shared_group_members SET status = 'invited' WHERE person_id = ?", (pid,))


def unlink_contact(conn, user_id, person_id):
    uid, pid = int(user_id), int(person_id)
    if conn.execute("SELECT 1 FROM shared_group_members WHERE person_id = ? LIMIT 1", (pid,)).fetchone():
        raise ValueError("They are in a group; unlinking would lock them out of it.")
    conn.execute(
        "UPDATE shared_people SET linked_user_id = NULL WHERE id = ? AND user_id = ? AND is_self = 0",
        (pid, uid),
    )


# --- groups & access -----------------------------------------------------------


def group_for_user(conn, user_id, group_id):
    """The group row if this user is an active member, else None."""
    return conn.execute(
        """
        SELECT g.id, g.name, g.user_id FROM shared_groups g
        WHERE g.id = ? AND EXISTS (
            SELECT 1 FROM shared_group_members m JOIN shared_people p ON p.id = m.person_id
            WHERE m.group_id = g.id AND p.linked_user_id = ? AND m.status = 'active'
        )
        """,
        (int(group_id), int(user_id)),
    ).fetchone()


def member_person(conn, group_id, user_id, statuses=("active",)):
    """The person standing for this user in the group, or None."""
    marks = ",".join("?" * len(statuses))
    row = conn.execute(
        f"""
        SELECT p.id FROM shared_group_members m JOIN shared_people p ON p.id = m.person_id
        WHERE m.group_id = ? AND p.linked_user_id = ? AND m.status IN ({marks})
        """,
        (int(group_id), int(user_id), *statuses),
    ).fetchone()
    return int(row["id"]) if row else None


def group_member_ids(conn, group_id):
    return [
        int(r["person_id"])
        for r in conn.execute(
            "SELECT person_id FROM shared_group_members WHERE group_id = ? ORDER BY person_id",
            (int(group_id),),
        )
    ]


def create_group(conn, user_id, name):
    uid = int(user_id)
    cur = conn.execute("INSERT INTO shared_groups (user_id, name) VALUES (?, ?)", (uid, name))
    gid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO shared_group_members (group_id, person_id, status) VALUES (?, ?, 'active')",
        (gid, ensure_self(conn, uid)),
    )
    return gid


def add_member(conn, group_id, person_id):
    """Add a person to a group: plain names join straight away, linked users
    are invited (a declined user is invited again). Returns 'added',
    'invited' or 'already'."""
    gid, pid = int(group_id), int(person_id)
    person = conn.execute(
        "SELECT linked_user_id FROM shared_people WHERE id = ?", (pid,)
    ).fetchone()
    linked = person["linked_user_id"] if person else None
    existing = conn.execute(
        "SELECT status FROM shared_group_members WHERE group_id = ? AND person_id = ?", (gid, pid)
    ).fetchone()
    if existing:
        if existing["status"] == "declined":
            conn.execute(
                "UPDATE shared_group_members SET status = 'invited' WHERE group_id = ? AND person_id = ?",
                (gid, pid),
            )
            return "invited"
        return "already"
    if linked is not None and conn.execute(
        """
        SELECT 1 FROM shared_group_members m JOIN shared_people p ON p.id = m.person_id
        WHERE m.group_id = ? AND p.linked_user_id = ?
        """,
        (gid, linked),
    ).fetchone():
        return "already"
    status = "invited" if linked is not None else "active"
    conn.execute(
        "INSERT INTO shared_group_members (group_id, person_id, status) VALUES (?, ?, ?)",
        (gid, pid, status),
    )
    return status if status == "invited" else "added"


def respond_to_invite(conn, user_id, group_id, accept):
    pid = member_person(conn, group_id, user_id, statuses=("invited",))
    if pid is None:
        return False
    conn.execute(
        "UPDATE shared_group_members SET status = ? WHERE group_id = ? AND person_id = ?",
        ("active" if accept else "declined", int(group_id), pid),
    )
    return True


def person_involved_in_group(conn, group_id, person_id):
    """True when removing this member would rewrite the group's history."""
    gid, pid = int(group_id), int(person_id)
    return conn.execute(
        """
        SELECT 1 FROM shared_expenses e
        WHERE e.group_id = ? AND (
            e.paid_by = ?
            OR EXISTS (SELECT 1 FROM shared_expense_splits s WHERE s.expense_id = e.id AND s.person_id = ?)
        )
        UNION ALL
        SELECT 1 FROM shared_settlements
        WHERE group_id = ? AND (from_person_id = ? OR to_person_id = ?)
        LIMIT 1
        """,
        (gid, pid, pid, gid, pid, pid),
    ).fetchone() is not None


def delete_group(conn, group_id):
    """Remove a group and every personal row it created, for every user."""
    gid = int(group_id)
    for row in conn.execute("SELECT id FROM shared_expenses WHERE group_id = ?", (gid,)).fetchall():
        delete_expense(conn, row["id"])
    for row in conn.execute("SELECT id FROM shared_settlements WHERE group_id = ?", (gid,)).fetchall():
        delete_settlement(conn, row["id"])
    conn.execute("DELETE FROM shared_groups WHERE id = ?", (gid,))


# --- links into each user's own accounts --------------------------------------


def ledger_account_id(conn, user_id):
    """The user's "Shared balances" account if it already exists, else None."""
    row = conn.execute(
        """
        SELECT a.id FROM shared_settings s
        JOIN accounts a ON a.id = s.ledger_account_id AND a.user_id = s.user_id
        WHERE s.user_id = ?
        """,
        (int(user_id),),
    ).fetchone()
    return int(row["id"]) if row else None


def ensure_ledger_account(conn, user_id):
    """Create (or adopt an existing same-named) "Shared balances" account."""
    uid = int(user_id)
    existing = ledger_account_id(conn, uid)
    if existing:
        return existing
    row = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? AND name = ?", (uid, LEDGER_ACCOUNT_NAME)
    ).fetchone()
    if row:
        account_id = int(row["id"])
    else:
        cur = conn.execute(
            "INSERT INTO accounts (user_id, name, opening_balance) VALUES (?, ?, 0)",
            (uid, LEDGER_ACCOUNT_NAME),
        )
        account_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO shared_settings (user_id, ledger_account_id) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET ledger_account_id = excluded.ledger_account_id
        """,
        (uid, account_id),
    )
    return account_id


def _insert_transfer(conn, uid, from_id, to_id, cents, day, notes):
    cur = conn.execute(
        """
        INSERT INTO account_transfers (user_id, from_account_id, to_account_id, amount, transferred_at, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (uid, from_id, to_id, cents_to_amount(cents), day, notes),
    )
    return int(cur.lastrowid)


def _delete_rows(conn, uid, expense_id=None, transfer_id=None):
    if expense_id:
        conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, uid))
    if transfer_id:
        conn.execute("DELETE FROM account_transfers WHERE id = ? AND user_id = ?", (transfer_id, uid))


def expense_involvement(conn, expense_id, person_id):
    """(my_share_cents, i_paid) for one person in one bill."""
    e = conn.execute("SELECT paid_by FROM shared_expenses WHERE id = ?", (int(expense_id),)).fetchone()
    share = conn.execute(
        "SELECT share_cents FROM shared_expense_splits WHERE expense_id = ? AND person_id = ?",
        (int(expense_id), person_id),
    ).fetchone()
    return (int(share["share_cents"]) if share else 0, bool(e) and int(e["paid_by"]) == person_id)


def _rebuild_expense_link(conn, expense_id, user_id):
    eid, uid = int(expense_id), int(user_id)
    link = conn.execute(
        "SELECT * FROM shared_expense_links WHERE expense_id = ? AND user_id = ?", (eid, uid)
    ).fetchone()
    if not link:
        return
    _delete_rows(conn, uid, link["linked_expense_id"], link["linked_transfer_id"])
    linked_expense_id = linked_transfer_id = None
    e = conn.execute(
        """
        SELECT e.*, g.name AS group_name FROM shared_expenses e
        JOIN shared_groups g ON g.id = e.group_id WHERE e.id = ?
        """,
        (eid,),
    ).fetchone()
    me = member_person(conn, e["group_id"], uid) if e else None
    if me is not None:
        my_share, i_paid = expense_involvement(conn, eid, me)
        notes = f"{NOTE_PREFIX} {e['description']} · {e['group_name']}"
        if my_share or i_paid:
            ledger_id = ensure_ledger_account(conn, uid)
            spend_from = link["account_id"] if i_paid else ledger_id
            if my_share > 0 and link["category_id"] and spend_from:
                cur = conn.execute(
                    """
                    INSERT INTO expenses (user_id, notes, amount, category_id, account_id, spent_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (uid, notes, -cents_to_amount(my_share), link["category_id"], spend_from, e["spent_on"]),
                )
                linked_expense_id = int(cur.lastrowid)
            lent = int(e["amount_cents"]) - my_share
            if i_paid and lent > 0 and link["account_id"]:
                linked_transfer_id = _insert_transfer(
                    conn, uid, link["account_id"], ledger_id, lent, e["spent_on"], f"{notes} (lent)"
                )
    conn.execute(
        """
        UPDATE shared_expense_links SET linked_expense_id = ?, linked_transfer_id = ?
        WHERE expense_id = ? AND user_id = ?
        """,
        (linked_expense_id, linked_transfer_id, eid, uid),
    )


def relink_expense(conn, expense_id):
    """Rebuild every recording user's rows after a bill changed."""
    for row in conn.execute(
        "SELECT user_id FROM shared_expense_links WHERE expense_id = ?", (int(expense_id),)
    ).fetchall():
        _rebuild_expense_link(conn, expense_id, row["user_id"])


def set_expense_link(conn, user_id, expense_id, category_id, account_id):
    """Record (or re-record) a bill in this user's books with their choices."""
    conn.execute(
        """
        INSERT INTO shared_expense_links (expense_id, user_id, category_id, account_id) VALUES (?, ?, ?, ?)
        ON CONFLICT(expense_id, user_id) DO UPDATE
            SET category_id = excluded.category_id, account_id = excluded.account_id
        """,
        (int(expense_id), int(user_id), category_id, account_id),
    )
    _rebuild_expense_link(conn, expense_id, user_id)


def clear_expense_link(conn, user_id, expense_id):
    uid = int(user_id)
    link = conn.execute(
        "SELECT linked_expense_id, linked_transfer_id FROM shared_expense_links WHERE expense_id = ? AND user_id = ?",
        (int(expense_id), uid),
    ).fetchone()
    if link:
        _delete_rows(conn, uid, link["linked_expense_id"], link["linked_transfer_id"])
        conn.execute(
            "DELETE FROM shared_expense_links WHERE expense_id = ? AND user_id = ?", (int(expense_id), uid)
        )


def create_expense(conn, user_id, group_id, description, amount_cents, paid_by, split_method, spent_on, splits, link):
    """Insert a bill with its splits; link is the creator's own (category_id,
    account_id) recording choice or None. Returns the new bill id."""
    cur = conn.execute(
        """
        INSERT INTO shared_expenses (user_id, group_id, description, amount_cents, paid_by, split_method, spent_on)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (int(user_id), int(group_id), description, int(amount_cents), int(paid_by), split_method, spent_on),
    )
    expense_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO shared_expense_splits (expense_id, person_id, share_cents, input_value) VALUES (?, ?, ?, ?)",
        [(expense_id, pid, cents, value) for pid, cents, value in splits],
    )
    if link:
        set_expense_link(conn, user_id, expense_id, *link)
    relink_expense(conn, expense_id)
    return expense_id


def groups_for_bot(conn, user_id):
    """The user's active groups with every member's possible names, for the
    Telegram bot: [{id, name, me, members: [{id, names}]}]."""
    uid = int(user_id)
    out = []
    for g in conn.execute(
        """
        SELECT g.id, g.name FROM shared_groups g
        JOIN shared_group_members m ON m.group_id = g.id
        JOIN shared_people p ON p.id = m.person_id
        WHERE p.linked_user_id = ? AND m.status = 'active'
        ORDER BY g.name
        """,
        (uid,),
    ).fetchall():
        names = _Names(conn, uid)
        members = []
        for row in _members(conn, g["id"]):
            aliases = {row["name"], names.person(row)}
            if row["linked_user_id"] is not None:
                aliases.add(names.logins.get(int(row["linked_user_id"]), ""))
            members.append({"id": int(row["id"]), "names": [a for a in aliases if a and a != SELF_NAME]})
        out.append({
            "id": int(g["id"]),
            "name": g["name"],
            "me": member_person(conn, g["id"], uid),
            "members": members,
        })
    return out


def person_display_name(conn, viewer_id, person_id):
    row = conn.execute("SELECT name, linked_user_id FROM shared_people WHERE id = ?", (int(person_id),)).fetchone()
    return _Names(conn, viewer_id).person(row) if row else "?"


def delete_expense(conn, expense_id):
    for row in conn.execute(
        "SELECT user_id FROM shared_expense_links WHERE expense_id = ?", (int(expense_id),)
    ).fetchall():
        clear_expense_link(conn, row["user_id"], expense_id)
    conn.execute("DELETE FROM shared_expenses WHERE id = ?", (int(expense_id),))


def set_settlement_link(conn, user_id, settlement_id, account_id):
    sid, uid = int(settlement_id), int(user_id)
    clear_settlement_link(conn, uid, sid)
    s = conn.execute(
        "SELECT s.*, g.name AS group_name FROM shared_settlements s JOIN shared_groups g ON g.id = s.group_id WHERE s.id = ?",
        (sid,),
    ).fetchone()
    me = member_person(conn, s["group_id"], uid) if s else None
    if me is None or me not in (int(s["from_person_id"]), int(s["to_person_id"])):
        return
    ledger_id = ensure_ledger_account(conn, uid)
    if int(s["from_person_id"]) == me:
        src, dst, what = account_id, ledger_id, "paid"
    else:
        src, dst, what = ledger_id, account_id, "received"
    notes = f"{NOTE_PREFIX} Settle up ({what}) · {s['group_name']}"
    transfer_id = _insert_transfer(conn, uid, src, dst, int(s["amount_cents"]), s["paid_on"], notes)
    conn.execute(
        "INSERT INTO shared_settlement_links (settlement_id, user_id, account_id, linked_transfer_id) VALUES (?, ?, ?, ?)",
        (sid, uid, account_id, transfer_id),
    )


def clear_settlement_link(conn, user_id, settlement_id):
    uid = int(user_id)
    link = conn.execute(
        "SELECT linked_transfer_id FROM shared_settlement_links WHERE settlement_id = ? AND user_id = ?",
        (int(settlement_id), uid),
    ).fetchone()
    if link:
        _delete_rows(conn, uid, transfer_id=link["linked_transfer_id"])
        conn.execute(
            "DELETE FROM shared_settlement_links WHERE settlement_id = ? AND user_id = ?",
            (int(settlement_id), uid),
        )


def delete_settlement(conn, settlement_id):
    for row in conn.execute(
        "SELECT user_id FROM shared_settlement_links WHERE settlement_id = ?", (int(settlement_id),)
    ).fetchall():
        clear_settlement_link(conn, row["user_id"], settlement_id)
    conn.execute("DELETE FROM shared_settlements WHERE id = ?", (int(settlement_id),))


# --- balances & panel data ---------------------------------------------------


def group_net_balances(conn, group_id):
    """{person_id: cents} for one group; positive = the group owes them."""
    gid = int(group_id)
    net = {pid: 0 for pid in group_member_ids(conn, gid)}
    for row in conn.execute(
        "SELECT paid_by, SUM(amount_cents) AS c FROM shared_expenses WHERE group_id = ? GROUP BY paid_by",
        (gid,),
    ):
        net[int(row["paid_by"])] = net.get(int(row["paid_by"]), 0) + int(row["c"])
    for row in conn.execute(
        """
        SELECT s.person_id, SUM(s.share_cents) AS c
        FROM shared_expense_splits s JOIN shared_expenses e ON e.id = s.expense_id
        WHERE e.group_id = ? GROUP BY s.person_id
        """,
        (gid,),
    ):
        net[int(row["person_id"])] = net.get(int(row["person_id"]), 0) - int(row["c"])
    for row in conn.execute(
        "SELECT from_person_id, to_person_id, amount_cents FROM shared_settlements WHERE group_id = ?",
        (gid,),
    ):
        net[int(row["from_person_id"])] = net.get(int(row["from_person_id"]), 0) + int(row["amount_cents"])
        net[int(row["to_person_id"])] = net.get(int(row["to_person_id"]), 0) - int(row["amount_cents"])
    return net


class _Names:
    """Display names as one viewer should see them: their own person is
    "You"; another user is shown under the viewer's own name for them if
    they have one, else by login; plain names stay as typed."""

    def __init__(self, conn, viewer):
        self.viewer = int(viewer)
        self.logins = {int(r["id"]): r["username"] for r in conn.execute("SELECT id, username FROM users")}
        self.contact_names = {
            int(r["linked_user_id"]): r["name"]
            for r in conn.execute(
                "SELECT name, linked_user_id FROM shared_people WHERE user_id = ? AND linked_user_id IS NOT NULL AND is_self = 0",
                (self.viewer,),
            )
        }

    def person(self, row):
        linked = row["linked_user_id"]
        if linked is None:
            return row["name"]
        if int(linked) == self.viewer:
            return SELF_NAME
        return self.contact_names.get(int(linked)) or self.logins.get(int(linked)) or row["name"]

    def user(self, user_id):
        if int(user_id) == self.viewer:
            return "you"
        return self.contact_names.get(int(user_id)) or self.logins.get(int(user_id), "?")


def _members(conn, gid):
    return conn.execute(
        """
        SELECT p.id, p.name, p.linked_user_id, m.status
        FROM shared_group_members m JOIN shared_people p ON p.id = m.person_id
        WHERE m.group_id = ?
        """,
        (int(gid),),
    ).fetchall()


def _expense_activity(conn, gid, viewer, me, names, person_names):
    items = []
    rows = conn.execute(
        """
        SELECT e.id, e.user_id, e.description, e.amount_cents, e.paid_by, e.split_method, e.spent_on,
               l.user_id AS link_user, l.category_id, l.account_id, l.linked_expense_id, l.linked_transfer_id
        FROM shared_expenses e
        LEFT JOIN shared_expense_links l ON l.expense_id = e.id AND l.user_id = ?
        WHERE e.group_id = ?
        """,
        (viewer, gid),
    ).fetchall()
    splits_by_expense = {}
    for s in conn.execute(
        """
        SELECT s.expense_id, s.person_id, s.share_cents, s.input_value
        FROM shared_expense_splits s JOIN shared_expenses e ON e.id = s.expense_id
        WHERE e.group_id = ?
        """,
        (gid,),
    ):
        splits_by_expense.setdefault(int(s["expense_id"]), []).append(s)
    for r in rows:
        splits = splits_by_expense.get(int(r["id"]), [])
        my_share = sum(int(s["share_cents"]) for s in splits if int(s["person_id"]) == me)
        i_paid = int(r["paid_by"]) == me
        if i_paid:
            effect = int(r["amount_cents"]) - my_share
            effect_text = "you lent" if effect > 0 else "you paid only your share"
        elif my_share:
            effect = -my_share
            effect_text = "you borrowed"
        else:
            effect = 0
            effect_text = "not involved"
        recorded = r["link_user"] is not None
        split_rows = sorted(
            (
                {"person_id": int(s["person_id"]), "name": person_names.get(int(s["person_id"]), "?"),
                 "share": cents_to_amount(s["share_cents"]), "input_value": s["input_value"]}
                for s in splits
            ),
            key=lambda x: (x["person_id"] != me, x["name"].lower()),
        )
        items.append(
            {
                "kind": "expense",
                "id": int(r["id"]),
                "date": r["spent_on"],
                "description": r["description"],
                "amount": cents_to_amount(r["amount_cents"]),
                "paid_by_name": person_names.get(int(r["paid_by"]), "?"),
                "added_by": names.user(r["user_id"]),
                "split_method": r["split_method"],
                "method_label": SPLIT_METHOD_LABELS.get(r["split_method"], r["split_method"]),
                "splits": split_rows,
                "effect": cents_to_amount(effect),
                "effect_text": effect_text,
                "involved": bool(my_share or i_paid),
                "i_paid": i_paid,
                "my_share": cents_to_amount(my_share),
                "recorded": recorded,
                # Recorded, but the category/account it needs is missing
                # (e.g. someone else made me the payer after I recorded it).
                "record_incomplete": recorded and (
                    (my_share and not r["linked_expense_id"])
                    or (i_paid and int(r["amount_cents"]) > my_share and not r["linked_transfer_id"])
                ),
                "payload": {
                    "id": int(r["id"]),
                    "description": r["description"],
                    "amount": f"{int(r['amount_cents']) / 100:.2f}",
                    "spent_on": r["spent_on"],
                    "paid_by": int(r["paid_by"]),
                    "split_method": r["split_method"],
                    "included": [int(s["person_id"]) for s in splits],
                    "values": {
                        str(int(s["person_id"])): (
                            s["input_value"] if r["split_method"] != "exact"
                            else f"{int(s['share_cents']) / 100:.2f}"
                        )
                        for s in splits
                    },
                    "record_personal": recorded,
                    "category_id": r["category_id"],
                    "account_id": r["account_id"],
                },
            }
        )
    return items


def _settlement_activity(conn, gid, viewer, me, names, person_names):
    items = []
    for r in conn.execute(
        """
        SELECT s.id, s.user_id, s.from_person_id, s.to_person_id, s.amount_cents, s.paid_on, s.notes,
               l.user_id AS link_user
        FROM shared_settlements s
        LEFT JOIN shared_settlement_links l ON l.settlement_id = s.id AND l.user_id = ?
        WHERE s.group_id = ?
        """,
        (viewer, gid),
    ):
        frm, to = int(r["from_person_id"]), int(r["to_person_id"])
        if frm == me:
            effect = cents_to_amount(r["amount_cents"])
        elif to == me:
            effect = -cents_to_amount(r["amount_cents"])
        else:
            effect = 0.0
        items.append(
            {
                "kind": "settlement",
                "id": int(r["id"]),
                "date": r["paid_on"],
                "from_name": person_names.get(frm, "?"),
                "to_name": person_names.get(to, "?"),
                "amount": cents_to_amount(r["amount_cents"]),
                "notes": r["notes"],
                "added_by": names.user(r["user_id"]),
                "effect": effect,
                "involved": me in (frm, to),
                "i_paid": frm == me,
                "recorded": r["link_user"] is not None,
            }
        )
    return items


def panel_context(conn, user_id, selected_group_id=None):
    """Everything the Shared panel renders, from this user's point of view."""
    uid = int(user_id)
    ensure_self(conn, uid)
    names = _Names(conn, uid)

    contacts = [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "login": names.logins.get(int(r["linked_user_id"])) if r["linked_user_id"] else None,
            "group_count": int(r["n"]),
        }
        for r in conn.execute(
            """
            SELECT p.id, p.name, p.linked_user_id,
                   (SELECT COUNT(*) FROM shared_group_members m WHERE m.person_id = p.id) AS n
            FROM shared_people p
            WHERE p.user_id = ? AND p.is_self = 0
            ORDER BY p.name COLLATE NOCASE
            """,
            (uid,),
        )
    ]

    invitations = [
        {"id": int(r["id"]), "name": r["name"], "owner": names.user(r["user_id"])}
        for r in conn.execute(
            """
            SELECT g.id, g.name, g.user_id FROM shared_groups g
            JOIN shared_group_members m ON m.group_id = g.id
            JOIN shared_people p ON p.id = m.person_id
            WHERE p.linked_user_id = ? AND m.status = 'invited'
            ORDER BY g.name COLLATE NOCASE
            """,
            (uid,),
        )
    ]

    groups = []
    friend_totals = {}
    friend_labels = {}
    selected = None
    for g in conn.execute(
        """
        SELECT g.id, g.name, g.user_id FROM shared_groups g
        WHERE EXISTS (
            SELECT 1 FROM shared_group_members m JOIN shared_people p ON p.id = m.person_id
            WHERE m.group_id = g.id AND p.linked_user_id = ? AND m.status = 'active'
        )
        ORDER BY g.name COLLATE NOCASE, g.id
        """,
        (uid,),
    ).fetchall():
        gid = int(g["id"])
        members = _members(conn, gid)
        me = next(
            (int(m["id"]) for m in members if m["linked_user_id"] == uid and m["status"] == "active"), None
        )
        person_names = {int(m["id"]): names.person(m) for m in members}
        # The same friend is a different person row in each group they were
        # added to by someone else, so friends add up by login where known.
        friend_key = {
            int(m["id"]): ("u", int(m["linked_user_id"])) if m["linked_user_id"] else ("p", int(m["id"]))
            for m in members
        }
        net = group_net_balances(conn, gid)
        payments = simplify_debts(net)
        for frm, to, cents in payments:
            if me in (frm, to):
                other = to if frm == me else frm
                key = friend_key[other]
                friend_totals[key] = friend_totals.get(key, 0) + (cents if to == me else -cents)
                friend_labels[key] = person_names[other]
        ordered = sorted(
            members, key=lambda m: (int(m["id"]) != me, person_names[int(m["id"])].lower())
        )
        summary = {
            "id": gid,
            "name": g["name"],
            "member_names": [person_names[int(m["id"])] for m in ordered],
            "my_net": cents_to_amount(net.get(me, 0)),
            "shared_with_users": sum(
                1 for m in members if m["linked_user_id"] and m["linked_user_id"] != uid and m["status"] == "active"
            ),
        }
        groups.append(summary)
        if selected_group_id is not None and gid == int(selected_group_id):
            activity = _expense_activity(conn, gid, uid, me, names, person_names) + _settlement_activity(
                conn, gid, uid, me, names, person_names
            )
            activity.sort(key=lambda a: (a["date"], a["kind"] == "expense", a["id"]), reverse=True)
            total_spent = sum(a["amount"] for a in activity if a["kind"] == "expense")
            member_ids = {int(m["id"]) for m in members}
            member_users = {int(m["linked_user_id"]) for m in members if m["linked_user_id"]}
            selected = {
                **summary,
                "is_owner": int(g["user_id"]) == uid,
                "owner_name": names.user(g["user_id"]),
                "me_id": me,
                "members": [
                    {
                        "id": int(m["id"]),
                        "name": person_names[int(m["id"])],
                        "is_me": int(m["id"]) == me,
                        "is_user": m["linked_user_id"] is not None,
                        "login": names.logins.get(int(m["linked_user_id"])) if m["linked_user_id"] else None,
                        "is_owner": m["linked_user_id"] == int(g["user_id"]),
                        "status": m["status"],
                        "net": cents_to_amount(net.get(int(m["id"]), 0)),
                        "removable": m["linked_user_id"] != int(g["user_id"])
                        and not person_involved_in_group(conn, gid, int(m["id"])),
                    }
                    for m in ordered
                ],
                "suggestions": [
                    {
                        "from_id": frm,
                        "to_id": to,
                        "from_name": person_names.get(frm, "?"),
                        "to_name": person_names.get(to, "?"),
                        "amount": cents_to_amount(cents),
                        "involves_me": me in (frm, to),
                    }
                    for frm, to, cents in payments
                ],
                "activity": activity,
                "total_spent": round(total_spent, 2),
                "addable_contacts": [
                    c for c in contacts
                    if c["id"] not in member_ids
                    and not (c["login"] and find_user(conn, c["login"]) in member_users)
                ],
            }

    friends = sorted(
        (
            {"name": friend_labels[k], "amount": cents_to_amount(c)}
            for k, c in friend_totals.items()
            if c != 0
        ),
        key=lambda f: (-abs(f["amount"]), f["name"].lower()),
    )
    owed_to_me = sum(f["amount"] for f in friends if f["amount"] > 0)
    i_owe = -sum(f["amount"] for f in friends if f["amount"] < 0)
    return {
        "contacts": contacts,
        "invitations": invitations,
        "groups": groups,
        "friend_balances": friends,
        "owed_to_me": round(owed_to_me, 2),
        "i_owe": round(i_owe, 2),
        "net": round(owed_to_me - i_owe, 2),
        "selected": selected,
        "ledger_account_id": ledger_account_id(conn, uid),
        "split_methods": [(m, SPLIT_METHOD_LABELS[m]) for m in SPLIT_METHODS],
    }
