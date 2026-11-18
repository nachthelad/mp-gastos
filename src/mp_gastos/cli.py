"""Command line interface for the local ledger."""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from .client import MercadoPagoClient, MercadoPagoError
from .ledger import classify_movement, database, import_report, initialize, reconcile, review_queue
from .report import money, monthly_report
from .settlements import ARGENTINA, KINDS, load_rules
from .sync import sync


def parser():
    app = argparse.ArgumentParser(prog="mp-gastos")
    app.add_argument("--home", type=Path, default=Path(".mp-gastos"), help="Local ledger directory")
    cmds = app.add_subparsers(dest="command", required=True)
    cmds.add_parser("init", help="Initialize the database and classification rules")
    update = cmds.add_parser("sync", help="Sync API data or import a downloaded settlement")
    update.add_argument("--month", help="Last calendar month, YYYY-MM (default: this month)")
    update.add_argument("--months", type=int, default=1)
    update.add_argument("--wait", type=int, default=10, help="Seconds to wait for each report")
    update.add_argument("--csv", type=Path, help="Import this settlement offline")
    review = cmds.add_parser("review", help="List pending movements or classify one")
    review.add_argument("--id", type=int)
    review.add_argument("--kind", choices=KINDS)
    review.add_argument("--category")
    match = cmds.add_parser("reconcile", help="Match a card purchase to a statement line")
    match.add_argument("--payment", required=True)
    match.add_argument("--line", required=True)
    match.add_argument("--amount", required=True, help="Positive statement line amount")
    match.add_argument("--currency", required=True)
    report = cmds.add_parser("report", help="Write the monthly Markdown report")
    report.add_argument("--month", required=True)
    report.add_argument("--output", type=Path)
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
                if args.csv:
                    if args.month or args.months != 1:
                        raise ValueError("Use --month and --months only with API sync.")
                    import_report(db, args.csv.read_bytes(), load_rules(rules))
                    print("Settlement imported.")
                else:
                    month = args.month or datetime.now(ARGENTINA).strftime("%Y-%m")
                    results = sync(db, MercadoPagoClient(), month, months=args.months,
                                   rules_path=rules, wait=args.wait)
                    for period, status in results.items():
                        print(f"{period}: settlement {status}, cards synced")
                    if "pending" in results.values():
                        return 2
            elif args.command == "review":
                if args.id is None and args.kind is None and args.category is None:
                    for r in review_queue(db):
                        print(r["id"], r["date"], money(r["amount"]), r["currency"], r["description"], sep="\t")
                elif args.id is not None and args.kind and args.category:
                    classify_movement(db, args.id, args.kind, args.category)
                else:
                    raise ValueError("Supply --id, --kind and --category together.")
            elif args.command == "reconcile":
                reconcile(db, args.payment, args.line, args.amount, args.currency)
            elif args.command == "report":
                content = monthly_report(db, args.month)
                if args.output:
                    # Refuse to overwrite the ledger, rules or credentials by accident.
                    if args.output.suffix.lower() != ".md":
                        raise ValueError("Use a .md output file.")
                    args.output.write_text(content, encoding="utf-8")
                else:
                    sys.stdout.write(content)
    except (MercadoPagoError, ValueError, OSError, sqlite3.Error, csv.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
