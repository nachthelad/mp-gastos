"""Local ledger. Monetary columns hold integer cents."""

import csv
import sqlite3
from collections import Counter
from contextlib import contextmanager

from .settlements import (DEFAULT_RULES, DETAIL_FIELDS, KINDS, cents, classify, currency, local_date, month_end,
                          movement_identity, parse_report)

MONTHLY, DAILY = 1, 2
MONTHLY_REPORTS = """
CREATE TABLE IF NOT EXISTS monthly_reports (month TEXT PRIMARY KEY, end_at TEXT NOT NULL);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (slot INTEGER PRIMARY KEY CHECK(slot=1), payer TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    month TEXT PRIMARY KEY, task TEXT NOT NULL, end_at TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL, source TEXT NOT NULL, date TEXT NOT NULL,
    event TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL, description TEXT NOT NULL,
    kind TEXT, category TEXT, reviewed INTEGER NOT NULL DEFAULT 0,
    external_reference TEXT NOT NULL DEFAULT '', payment_method_type TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '', impact_kind TEXT NOT NULL DEFAULT '',
    report_origin INTEGER NOT NULL DEFAULT 0
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
""" + MONTHLY_REPORTS


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
            _upgrade_movements(db)
            yield db
    finally:
        db.close()


def _upgrade_movements(db):
    columns = {row["name"] for row in db.execute("PRAGMA table_info(movements)")}
    if not columns or "report_origin" in columns:
        return
    with db:
        db.execute("BEGIN IMMEDIATE")
        if any(row["name"] == "report_origin" for row in db.execute("PRAGMA table_info(movements)")):
            return
        for field in DETAIL_FIELDS[1:]:
            db.execute(f"ALTER TABLE movements ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        db.execute("ALTER TABLE movements ADD COLUMN report_origin INTEGER NOT NULL DEFAULT 0")
        db.execute(MONTHLY_REPORTS)
        occurrences = Counter()
        db.execute("UPDATE movements SET identity='legacy:' || id")
        db.executemany("UPDATE movements SET identity=? WHERE id=?", (
            (movement_identity(row, occurrences), row["id"]) for row in db.execute(
                "SELECT id,source,date,event,amount,currency FROM movements ORDER BY id")
        ))


def import_report(db, payload, rules):
    import_reports(db, (payload,), rules)


def import_reports(db, payloads, rules, *, daily=(), daily_month=None, monthly_end=None, enrich=None):
    if monthly_end is None:
        coverage = {}
    else:
        end = local_date(monthly_end)
        coverage = {end.strftime("%Y-%m"): end.isoformat()}
    months = set(coverage)
    movements = {}
    for reports, origin in ((daily, DAILY), (payloads, MONTHLY)):
        for payload in reports:
            for row in parse_report(payload):
                month = row["date"][:7]
                if origin == DAILY and daily_month is not None and month != daily_month:
                    continue
                months.add(month)
                if origin == MONTHLY and monthly_end is None and month not in coverage:
                    coverage[month] = month_end(local_date(row["date"])).isoformat()
                identity = row["identity"]
                if identity in movements:
                    prior = movements[identity]
                    for field in DETAIL_FIELDS:
                        row[field] = row[field] or prior[field]
                    origin_flags = prior["report_origin"] | origin
                else:
                    origin_flags = origin
                row["report_origin"] = origin_flags
                movements[identity] = row

    if enrich:
        enrich_fields = ("counterparty", "impact_kind")
        for row in movements.values():
            if any(not row[field] for field in enrich_fields):
                stored = db.execute("SELECT * FROM movements WHERE identity=?",
                                    (row["identity"],)).fetchone()
                if any(not row[field] and (not stored or not stored[field]) for field in enrich_fields):
                    for field, value in enrich(row["source"]).items():
                        row[field] = row[field] or (stored[field] if stored else "") or value

    with db:
        db.execute("BEGIN IMMEDIATE")
        db.executemany("""
            INSERT INTO monthly_reports(month,end_at) VALUES (?,?)
            ON CONFLICT(month) DO UPDATE SET end_at=excluded.end_at
            WHERE julianday(excluded.end_at) > julianday(monthly_reports.end_at)
        """, coverage.items())
        for row in movements.values():
            existing = db.execute("SELECT * FROM movements WHERE identity=?", (row["identity"],)).fetchone()
            if existing:
                for field in DETAIL_FIELDS:
                    row[field] = row[field] or existing[field]
                row["report_origin"] |= existing["report_origin"]
            row["kind"], row["category"] = classify(row, rules)
            db.execute("""
                INSERT INTO movements(identity,source,date,event,amount,currency,description,external_reference,
                                      payment_method_type,counterparty,impact_kind,report_origin,kind,category)
                VALUES (:identity,:source,:date,:event,:amount,:currency,:description,:external_reference,
                        :payment_method_type,:counterparty,:impact_kind,:report_origin,:kind,:category)
                ON CONFLICT(identity) DO UPDATE SET description=excluded.description,
                    external_reference=excluded.external_reference, payment_method_type=excluded.payment_method_type,
                    counterparty=excluded.counterparty, impact_kind=excluded.impact_kind,
                    report_origin=excluded.report_origin,
                    kind=CASE WHEN reviewed THEN kind ELSE excluded.kind END,
                    category=CASE WHEN reviewed THEN category ELSE excluded.category END
            """, row)
    return months


def daily_gap_count(db, month):
    return db.execute("""
        SELECT count(*) FROM movements m JOIN monthly_reports r ON r.month=substr(m.date,1,7)
        WHERE r.month=? AND m.report_origin=? AND julianday(m.date) <= julianday(r.end_at)
    """, (month, DAILY)).fetchone()[0]


def reclassify(db, rules):
    with db:
        db.execute("BEGIN IMMEDIATE")
        updates = []
        for row in db.execute("SELECT * FROM movements WHERE NOT reviewed"):
            kind, category = classify(row, rules)
            if (kind, category) != (row["kind"], row["category"]):
                updates.append((kind, category, row["id"]))
        db.executemany("UPDATE movements SET kind=?, category=? WHERE id=?", updates)
    return len(updates)


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


def import_classifications(db, path):
    """Apply a manual classification to every existing movement with the given source ID."""
    with path.open(encoding="utf-8-sig", newline="") as stream, db:
        reader = csv.DictReader(stream, strict=True)
        if sorted(reader.fieldnames or []) != ["category", "kind", "source_id"]:
            raise ValueError("Expected source_id,kind,category headers.")
        db.execute("BEGIN IMMEDIATE")
        seen = {}
        count = 0
        for row in reader:
            line = reader.line_num
            if None in row or any(value is None or not value.strip() for value in row.values()):
                raise ValueError(f"Line {line}: expected a value in every column.")
            source, kind, category = (row[field].strip() for field in ("source_id", "kind", "category"))
            if kind not in KINDS:
                raise ValueError(f"Line {line}: unknown classification kind.")
            if source in seen:
                if seen[source] != (kind, category):
                    raise ValueError(f"Line {line}: conflicting classifications for the same source ID.")
                continue
            result = db.execute("UPDATE movements SET kind=?, category=?, reviewed=1 WHERE source=?",
                                (kind, category, source))
            if not result.rowcount:
                raise ValueError(f"Line {line}: unknown source ID.")
            seen[source] = kind, category
            count += result.rowcount
    return count


def reconcile(db, payment, reference, amount, code):
    with db:
        db.execute("BEGIN IMMEDIATE")
        _reconcile_line(db, payment, reference, amount, code)


def import_reconciliations(db, path):
    with path.open(encoding="utf-8-sig", newline="") as stream, db:
        reader = csv.DictReader(stream, strict=True)
        fields = reader.fieldnames or []
        if sorted(fields) not in (["line", "payment"], ["amount", "currency", "line", "payment"]):
            raise ValueError("Expected payment,line or payment,line,amount,currency headers.")
        db.execute("BEGIN IMMEDIATE")
        for row in reader:
            try:
                if None in row or any(v is None or not v.strip() for v in row.values()):
                    raise ValueError("Expected a value in every column.")
                _reconcile_line(db, row["payment"].strip(), row["line"], row.get("amount"), row.get("currency"))
            except ValueError as exc:
                raise ValueError(f"Line {reader.line_num}: {exc}") from exc


def _reconcile_line(db, payment, reference, amount, code):
    reference = reference.strip()
    card = db.execute("SELECT paid,currency,active FROM cards WHERE id=?", (payment,)).fetchone()
    if amount is None:
        if not card or card["paid"] is None:
            raise ValueError("Expected a payment with a known total.")
        value, code = card["paid"], card["currency"]
    else:
        value, code = cents(amount), currency(code)
    if not reference or value <= 0:
        raise ValueError("Use a unique statement line reference and a positive amount.")
    existing = db.execute("SELECT payment,amount,currency FROM statement_lines WHERE reference=?",
                          (reference,)).fetchone()
    if existing:
        if tuple(existing) != (payment, value, code):
            raise ValueError("That statement reference already identifies a different line.")
        return
    if not card or not card["active"] or card["paid"] is None or card["currency"] != code:
        raise ValueError("Expected an active payment with a known total in the same currency.")
    matched = db.execute("SELECT COALESCE(SUM(amount),0) FROM statement_lines WHERE payment=?",
                         (payment,)).fetchone()[0]
    if matched + value > card["paid"]:
        raise ValueError("Statement lines exceed the buyer's total.")
    db.execute("INSERT INTO statement_lines VALUES (?,?,?,?)", (reference, payment, value, code))
