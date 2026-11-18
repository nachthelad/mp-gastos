"""Local ledger. Monetary columns hold integer cents."""

import sqlite3
from contextlib import contextmanager

from .settlements import DEFAULT_RULES, KINDS, cents, currency, parse_report

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (slot INTEGER PRIMARY KEY CHECK(slot=1), payer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    month TEXT PRIMARY KEY, task TEXT NOT NULL, end_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL, source TEXT NOT NULL, date TEXT NOT NULL,
    event TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, description TEXT NOT NULL,
    kind TEXT, category TEXT, reviewed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS movement_source ON movements(source);
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY, date TEXT NOT NULL, issuer TEXT NOT NULL, currency TEXT NOT NULL,
    paid INTEGER, description TEXT NOT NULL, method TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS statement_lines (
    reference TEXT PRIMARY KEY, payment TEXT NOT NULL REFERENCES cards(id),
    amount INTEGER NOT NULL CHECK(amount > 0), currency TEXT NOT NULL
);
"""


def initialize(home):
    home.mkdir(parents=True, exist_ok=True)
    try:
        with (home / "rules.toml").open("x", encoding="utf-8") as stream:
            stream.write(DEFAULT_RULES.read_text(encoding="utf-8"))
    except FileExistsError:
        pass
    with database(home, create=True) as db:
        db.executescript(SCHEMA)


@contextmanager
def database(home, *, create=False):
    path = home / "ledger.sqlite3"
    if not create and not path.is_file():
        raise ValueError("Run mp-gastos init first.")
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def import_report(db, payload, rules):
    with db:
        db.executemany("""
            INSERT INTO movements(identity,source,date,event,amount,currency,description,kind,category)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(identity) DO UPDATE SET description=excluded.description,
                amount=excluded.amount, currency=excluded.currency,
                kind=CASE WHEN reviewed THEN kind ELSE excluded.kind END,
                category=CASE WHEN reviewed THEN category ELSE excluded.category END
        """, parse_report(payload, rules))


def review_queue(db):
    return list(db.execute("""
        SELECT * FROM movements m WHERE kind IS NULL
        AND NOT (amount < 0 AND EXISTS(SELECT 1 FROM cards c WHERE c.id=m.source AND c.active))
        ORDER BY date,id
    """))


def classify_movement(db, movement, kind, category):
    if kind not in KINDS or not category.strip():
        raise ValueError("Choose a valid kind and a nonempty category.")
    with db:
        result = db.execute("UPDATE movements SET kind=?, category=?, reviewed=1 WHERE id=?",
                            (kind, category.strip(), movement))
        if not result.rowcount:
            raise ValueError("Unknown movement ID.")


def reconcile(db, payment, reference, amount, code):
    reference = reference.strip()
    value = cents(amount)
    code = currency(code)
    if not reference or value <= 0:
        raise ValueError("Use a unique statement line reference and a positive amount.")
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT payment,amount,currency FROM statement_lines WHERE reference=?",
                              (reference,)).fetchone()
        if existing:
            if tuple(existing) != (payment, value, code):
                raise ValueError("That statement reference already identifies a different line.")
            return
        card = db.execute("SELECT paid,currency FROM cards WHERE id=? AND active=1", (payment,)).fetchone()
        if not card or card["paid"] is None or card["currency"] != code:
            raise ValueError("Expected an active payment with a known total in the same currency.")
        matched = db.execute("SELECT COALESCE(SUM(amount),0) FROM statement_lines WHERE payment=?",
                             (payment,)).fetchone()[0]
        if matched + value > card["paid"]:
            raise ValueError("Statement lines exceed the buyer's total.")
        db.execute("INSERT INTO statement_lines VALUES (?,?,?,?)", (reference, payment, value, code))
