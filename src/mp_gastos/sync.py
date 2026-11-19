"""Sync one account in bounded calendar-month windows."""

import re
import time
from datetime import UTC, datetime, timedelta

from .client import CARD_TYPES, REPORT, MercadoPagoError, ready_files, report_task_id
from .ledger import import_reports
from .settlements import ARGENTINA, REQUIRED, cents, currency, load_rules, local_date, month_end

DEFAULT_WAIT = 180


def month_start(month):
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("Expected YYYY-MM.")
    return datetime.fromisoformat(month + "-01").replace(tzinfo=ARGENTINA)


def windows(month, months, now):
    start = month_start(month)
    today = now.astimezone(ARGENTINA)
    age = (today.year - start.year) * 12 + today.month - start.month
    if not 1 <= months <= 12 or age < 0 or age + months > 12:
        raise ValueError("Choose up to 12 months within the current and preceding 11 calendar months.")
    result = []
    for _ in range(months):
        result.append((start, min(today, month_end(start))))
        start = (start - timedelta(days=1)).replace(day=1)
    return reversed(result)


def api_date(value):
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ensure_config(client):
    try:
        config = client.get_report_config()
    except MercadoPagoError as exc:
        if exc.status != 404:
            raise
        config = {}
    if not config:
        columns = REQUIRED | {"DESCRIPTION", "TRANSACTION_AMOUNT", "FEE_AMOUNT", "EXTERNAL_REFERENCE",
                              "PAYMENT_METHOD_TYPE"}
        client._object("POST", REPORT + "/config", {
            "file_name_prefix": "mp-gastos", "display_timezone": "GMT-03",
            "separator": ";", "header_language": "en", "include_withdraw": True,
            "frequency": {"hour": 0, "type": "monthly", "value": 1},
            "columns": [{"key": name} for name in sorted(columns)],
        })
    elif config.get("separator") not in (None, ";"):
        raise ValueError("Select ';' as the settlement report separator in Mercado Pago.")


def _card_values(row, month):
    date = local_date(row.get("date_created", "")).isoformat()
    if date[:7] != month:
        return None
    payment = str(row.get("id"))
    if not payment.isdigit():
        raise ValueError("Invalid payment ID.")
    details = row.get("transaction_details") or {}
    paid = details.get("total_paid_amount")
    paid = None if paid is None else cents(paid)
    if paid is not None and paid < 0:
        raise ValueError("Invalid buyer total.")
    return (payment, date, str(row.get("issuer_id") or "unknown"), currency(row.get("currency_id")),
            paid, row.get("description") or "", row["payment_type_id"])


def sync_cards(db, client, payer, start, end):
    month = start.strftime("%Y-%m")
    with db:
        # Serialize each month snapshot. A failed second page rolls everything back.
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE cards SET active=0 WHERE substr(date,1,7)=?", (month,))
        for method in CARD_TYPES:
            for row in client.search_card_payments(payer, method, api_date(start), api_date(end)):
                values = _card_values(row, month)
                if values is not None:
                    db.execute("""
                        INSERT INTO cards(id,date,issuer,currency,paid,description,method)
                        VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET date=excluded.date, issuer=excluded.issuer,
                            currency=excluded.currency, paid=excluded.paid,
                            description=excluded.description, method=excluded.method, active=1
                    """, values)


def _same_end(left, right):
    left, right = local_date(left), local_date(right)
    if left.microsecond and right.microsecond:
        return left == right
    return left.replace(microsecond=0) == right.replace(microsecond=0)


def _matches_window(report, begin, end):
    try:
        return (local_date(report["begin_date"]) == local_date(begin)
                and _same_end(report["end_date"], end))
    except (KeyError, ValueError, TypeError):
        return False


def _sync_settlement(db, client, start, end, rules, wait, enrich):
    month = start.strftime("%Y-%m")
    begin, cutoff = api_date(start), api_date(end)
    reports = client.list_reports()
    with db:
        db.execute("BEGIN IMMEDIATE")
        job = db.execute("SELECT * FROM jobs WHERE month=?", (month,)).fetchone()
        if job and (not job["done"] or _same_end(job["end_at"], cutoff)):
            task, cutoff = job["task"], job["end_at"]
        else:
            # Recover a server-accepted POST even if the previous response was lost.
            report = next((r for r in reports if _matches_window(r, begin, cutoff)
                           and str(r.get("status")).lower() in ("processed", "available", "pending", "processing")
                           and (report_task_id(r) or ready_files(r))), None)
            if report:
                task = report_task_id(report)
            else:
                response = client.create_report(begin_date=begin, end_date=cutoff)
                task = report_task_id(response)
            db.execute("INSERT OR REPLACE INTO jobs(month,task,end_at) VALUES(?,?,?)",
                       (month, task, cutoff))
    deadline = time.monotonic() + wait
    report_id = None
    while True:
        ready = next((r for r in reports if (_matches_window(r, begin, cutoff)
                      or (task and report_task_id(r) == task)
                      or (report_id and str(r.get("report_id") or r.get("id")) == report_id))
                      and ready_files(r)), None)
        if not ready and task:
            try:
                ready = client.get_report_task(task)
                if ready.get("report_id"):
                    report_id = str(ready["report_id"])
            except MercadoPagoError as exc:
                if exc.status != 404:
                    raise
                ready = None
        state = str((ready or {}).get("status")).lower()
        files = ready_files(ready or {})
        if files:
            import_reports(db, (client.download_report(name) for name in files), rules,
                           monthly_end=cutoff, enrich=enrich)
            with db:
                db.execute("UPDATE jobs SET done=1 WHERE month=? AND end_at=? AND task=?",
                           (month, cutoff, task))
            return "imported" if _same_end(cutoff, api_date(end)) else "catch_up"
        if state in ("failed", "error", "cancelled", "canceled"):
            with db:
                db.execute("DELETE FROM jobs WHERE month=? AND task=? AND end_at=?", (month, task, cutoff))
            raise MercadoPagoError("Report generation failed. Run sync again to retry.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "pending"
        time.sleep(min(2, remaining))
        reports = client.list_reports()


def sync(db, client, month, *, months=1, rules_path, wait=DEFAULT_WAIT, now=None):
    if wait < 0:
        raise ValueError("Wait time must be nonnegative.")
    periods = list(windows(month, months, now or datetime.now(UTC)))
    rules = load_rules(rules_path)
    payer = client.get_user_id()
    with db:
        db.execute("INSERT OR IGNORE INTO account VALUES(1,?)", (payer,))
        if db.execute("SELECT payer FROM account").fetchone()[0] != payer:
            raise ValueError("This ledger belongs to another account. Choose another data directory.")
    ensure_config(client)
    enrich = client.payment_enricher(payer)
    result = {}
    for start, end in periods:
        sync_cards(db, client, payer, start, end)
        status = _sync_settlement(db, client, start, end, rules, wait, enrich)
        if status == "catch_up":
            status = _sync_settlement(db, client, start, end, rules, wait, enrich)
        result[start.strftime("%Y-%m")] = status
    return result
