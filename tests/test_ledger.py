import csv
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from mp_gastos.cli import main
from mp_gastos.ledger import classify_movement, database, import_report, initialize, reconcile, review_queue
from mp_gastos.report import monthly_report
from mp_gastos.settlements import load_rules

FIXTURE = Path(__file__).parent / "fixtures" / "settlement.csv"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        initialize(self.home)
        self.db = self.enterContext(database(self.home))
        self.rules = load_rules(self.home / "rules.toml")
        self.csv = FIXTURE.read_bytes()

    def test_reimport_review_and_rules(self):
        import_report(self.db, self.csv, self.rules)
        r = next(row for row in review_queue(self.db) if row["source"] == "71005")
        classify_movement(self.db, r["id"], "purchase", "Models")
        import_report(self.db, self.csv, [])
        self.assertEqual(self.db.execute("SELECT category FROM movements WHERE reviewed").fetchone()[0],
                         "Models")
        self.assertEqual(len(review_queue(self.db)), 5)

    def test_multiline_quotes_refund_and_anonymous_duplicates(self):
        header = self.csv.splitlines()[0]
        original = self.csv.splitlines()[1]
        ref = original.replace(b"SETTLEMENT;", b"REFUND;")
        anon = self.csv.splitlines()[3]
        payload = b"\n".join((header, original, ref, anon, anon))
        payload = payload.replace(b"Nube de Papel", b'Nube ""de""\nPapel')
        for _ in range(2):
            import_report(self.db, payload, self.rules)
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 4)
        self.assertIn('"de"\n', self.db.execute("SELECT description FROM movements LIMIT 1").fetchone()[0])

    def test_invalid_csv_rolls_back(self):
        for data in (self.csv + b"bad;row\n", self.csv + b'"unfinished\n',
                     self.csv.replace(b"SETTLEMENT_NET_AMOUNT", b"unknown"),
                     self.csv.replace(b";-41;", b";NaN;")):
            with self.assertRaises((ValueError, csv.Error)):
                import_report(self.db, data, self.rules)
            self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 0)

    def test_report_reconciliation_and_currency_isolation(self):
        import_report(self.db, self.csv, self.rules)
        with self.db:
            self.db.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,1)", [
                ("71002", "2026-04-10", "71", "ARS", 2345, "Example", "credit_card"),
                ("71006", "2026-04-12", "71", "USD", 500, "Example", "debit_card"),
                ("71007", "2026-04-13", "72", "USD", None, "Example", "credit_card")])
        for _ in range(2):
            reconcile(self.db, "71002", "April-8", "12.00", "ARS")
        for args in (("71002", "April-8", "12.01", "ARS"), ("71002", "May-1", "12", "ARS"),
                     ("71006", "April-8", "5", "USD"), ("71006", "April-9", "5", "ARS")):
            with self.assertRaises(ValueError):
                reconcile(self.db, *args)
        report = monthly_report(self.db, "2026-04")
        self.assertIn("| ARS | 38,25 | 12,00 | 26,25 |", report)
        self.assertIn("| USD | 5,00 | 0,00 | 5,00 |", report)
        self.assertIn("| 71 | ARS | 1 | 23,45 |", report)
        self.assertIn("Compras sin total del comprador: 1", report)
        self.assertEqual(len(review_queue(self.db)), 1)
        reconcile(self.db, "71002", "May-1", "11.45", "ARS")
        self.assertIn("| ARS | 38,25 | 23,45 | 14,80 |", monthly_report(self.db, "2026-04"))

    def test_offline_cli(self):
        def run(*args):
            return main(["--home", str(self.home), *args])
        output = self.home / "report.md"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(run("init"), 0)
            self.assertEqual(run("sync", "--csv", str(FIXTURE)), 0)
            self.assertEqual(run("review", "--id", "6", "--kind", "transfer", "--category", "Transfers"), 0)
            self.assertEqual(run("review", "--kind", "transfer"), 1)
            self.assertEqual(run("report", "--month", "2026-04", "--output", str(output)), 0)
        self.assertIn("| ARS | 14,80 |", output.read_text(encoding="utf-8"))
