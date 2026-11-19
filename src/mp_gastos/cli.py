"""Command line interface for the local ledger."""

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .client import MercadoPagoClient, MercadoPagoError
from .ledger import (classify_movement, daily_gap_count, database, import_reconciliations, import_reports,
                     initialize, reclassify, reconcile, review_queue)
from .report import money, monthly_data, monthly_report
from .settlements import ARGENTINA, KINDS, load_issuers, load_rules, month_end
from .sync import DEFAULT_WAIT, month_start, sync


def parser():
    app = argparse.ArgumentParser(prog="mp-gastos")
    app.add_argument("--home", type=Path, default=Path(".mp-gastos"), help="Local ledger directory")
    cmds = app.add_subparsers(dest="command", required=True)
    cmds.add_parser("init", help="Initialize the database and classification rules")
    update = cmds.add_parser("sync", help="Sync API data or import a downloaded settlement")
    update.add_argument("--month", help="Last calendar month, YYYY-MM (default: this month)")
    update.add_argument("--months", type=int, default=1)
    update.add_argument("--wait", type=int, default=DEFAULT_WAIT,
                        help="Seconds to wait for each report (default: %(default)s)")
    update.add_argument("--csv", type=Path, help="Import a monthly settlement CSV")
    update.add_argument("--daily-csv", type=Path, action="append", default=[],
                        help="Import a daily settlement CSV (repeat for multiple files)")
    update.add_argument("--enrich", action="store_true", help="Look up missing counterparties for local CSVs")
    cmds.add_parser("reclassify", help="Reapply rules to movements without a manual classification")
    review = cmds.add_parser("review", help="List pending movements or classify one")
    review.add_argument("--id", type=int)
    review.add_argument("--kind", choices=KINDS)
    review.add_argument("--category")
    review.add_argument("--json", action="store_true")
    match = cmds.add_parser("reconcile", help="Match a card purchase to a statement line")
    mode = match.add_mutually_exclusive_group(required=True)
    mode.add_argument("--payment")
    mode.add_argument("--import", dest="import_path", type=Path, help="CSV of matched statement lines")
    match.add_argument("--line")
    match.add_argument("--amount", help="Positive statement line amount")
    match.add_argument("--currency")
    report = cmds.add_parser("report", help="Write the monthly report as Markdown or JSON")
    report.add_argument("--month", required=True)
    report.add_argument("--output", type=Path)
    report.add_argument("--json", action="store_true")
    return app


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.home)
            print(args.home / "ledger.sqlite3")
            return 0
        with database(args.home) as db:
            if args.command == "sync":
                rules = args.home / "rules.toml"
                status_code = 0
                if args.csv or args.daily_csv:
                    if args.months != 1 or (args.month and not args.csv):
                        raise ValueError("Use --months with API sync and --month with API sync or a monthly CSV.")
                    end = month_end(month_start(args.month)).isoformat() if args.month else None
                    enrich = None
                    if args.enrich:
                        client = MercadoPagoClient()
                        payer = client.get_user_id()
                        stored = db.execute("SELECT payer FROM account").fetchone()
                        if stored and stored["payer"] != payer:
                            raise ValueError("This ledger belongs to another account.")
                        enrich = client.payment_enricher(payer)
                    periods = import_reports(db, (args.csv.read_bytes(),) if args.csv else (), load_rules(rules),
                                             daily=(path.read_bytes() for path in args.daily_csv),
                                             monthly_end=end, enrich=enrich)
                    print("Settlement imported.")
                else:
                    if args.enrich:
                        raise ValueError("API sync already enriches payments. Use --enrich with local CSVs.")
                    month = args.month or datetime.now(ARGENTINA).strftime("%Y-%m")
                    results = sync(db, MercadoPagoClient(), month, months=args.months,
                                   rules_path=rules, wait=args.wait)
                    for period, status in results.items():
                        print(f"{period}: settlement {status}, cards synced")
                    periods = results
                    if "pending" in results.values():
                        status_code = 2
                for period in sorted(periods):
                    count = daily_gap_count(db, period)
                    if count:
                        print(f"Aviso {period}: {count} movimiento(s) en los reportes diarios "
                              "que faltan en el mensual.", file=sys.stderr)
                return status_code
            elif args.command == "reclassify":
                count = reclassify(db, load_rules(args.home / "rules.toml"))
                print(f"Movimientos reclasificados: {count}.")
            elif args.command == "review":
                if args.id is None and args.kind is None and args.category is None:
                    rows = review_queue(db)
                    if args.json:
                        print(json.dumps([{"id": r["id"], "date": r["date"], "source": r["source"],
                                           "event": r["event"], "amount_cents": r["amount"],
                                           "currency": r["currency"], "description": r["description"],
                                           "counterparty": r["counterparty"], "impact_kind": r["impact_kind"]}
                                          for r in rows], ensure_ascii=False))
                    else:
                        for r in rows:
                            print(r["id"], r["date"], money(r["amount"]), r["currency"],
                                  r["description"] or r["counterparty"], sep="\t")
                elif args.id is not None and args.kind and args.category:
                    classify_movement(db, args.id, args.kind, args.category)
                    if args.json:
                        print(json.dumps({"id": args.id, "kind": args.kind, "category": args.category.strip()},
                                         ensure_ascii=False))
                else:
                    raise ValueError("Supply --id, --kind and --category together.")
            elif args.command == "reconcile":
                if args.import_path:
                    if any(v is not None for v in (args.line, args.amount, args.currency)):
                        raise ValueError("Supply statement lines in the import file.")
                    import_reconciliations(db, args.import_path)
                elif args.line and args.amount and args.currency:
                    reconcile(db, args.payment, args.line, args.amount, args.currency)
                else:
                    raise ValueError("Supply --line, --amount and --currency with --payment.")
            elif args.command == "report":
                issuers = load_issuers(args.home / "rules.toml")
                if args.json:
                    content = json.dumps(monthly_data(db, args.month, issuers), ensure_ascii=False, indent=2) + "\n"
                else:
                    content = monthly_report(db, args.month, issuers)
                if args.output:
                    # Refuse to overwrite the ledger, rules or credentials by accident.
                    suffix = ".json" if args.json else ".md"
                    if args.output.suffix.lower() != suffix:
                        raise ValueError(f"Use a {suffix} output file.")
                    args.output.write_text(content, encoding="utf-8")
                else:
                    sys.stdout.write(content)
    except (MercadoPagoError, ValueError, OSError, sqlite3.Error, csv.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
