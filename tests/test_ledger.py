import csv
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from mp_gastos.cli import main
from mp_gastos.ledger import (classify_movement, daily_gap_count, database, import_report, import_reports,
                             initialize, reconcile, review_queue)
from mp_gastos.report import monthly_data, monthly_report
from mp_gastos.settlements import load_rules

FIXTURE = Path(__file__).parent / "fixtures" / "settlement.csv"
DAILY = FIXTURE.with_name("settlement-daily.csv")
MONTHLY = FIXTURE.with_name("settlement-monthly.csv")
BOUNDARY = FIXTURE.with_name("settlement-boundary.csv")
CLASSIFICATIONS = FIXTURE.with_name("classifications.csv")


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
        self.assertEqual(len(review_queue(self.db)), 4)

    def test_daily_monthly_overlap_and_reclassification(self):
        self.cli("sync", "--daily-csv", str(DAILY))
        self.assertEqual(daily_gap_count(self.db, "2026-04"), 0)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["--home", str(self.home), "sync", "--csv", str(MONTHLY),
                                   "--daily-csv", str(DAILY), "--daily-csv", str(DAILY)]), 0)
        self.assertIn("2026-04: 1 movimiento(s)", err.getvalue())
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 6)
        yields = self.db.execute("SELECT amount,kind FROM movements WHERE source='72001' ORDER BY amount")
        self.assertEqual([tuple(row) for row in yields], [(27, "yield"), (111, "yield")])
        rules = self.home / "rules.toml"
        rules.write_text("""
[[rule]]
field = "counterparty"
exact = "financiera ambar"
kind = "loan"
category = "Cuotas"
direction = "debit"

[[rule]]
field = "impact_kind"
prefix = "asset_management"
kind = "yield"
category = "Inversiones"
""", encoding="utf-8")
        movement = self.db.execute("SELECT id FROM movements WHERE source='72005'").fetchone()[0]
        classify_movement(self.db, movement, "purchase", "Manual")
        self.assertIn("reclasificados: 4", self.cli("reclassify"))
        self.assertIn("reclasificados: 0", self.cli("reclassify"))
        for source, kind, category in (("72002", "loan", "Cuotas"), ("72003", "card_payment", "Tarjeta"),
                                       ("72004", None, None), ("72005", "purchase", "Manual")):
            values = self.db.execute("SELECT kind,category FROM movements WHERE source=?", (source,)).fetchone()
            self.assertEqual(tuple(values), (kind, category))
        data = json.loads(self.cli("report", "--month", "2026-04", "--json"))
        self.assertEqual(data["daily_only_movements"], 1)
        self.assertIn({"currency": "ARS", "kind": "loan", "category": "Cuotas", "net_cents": -2000},
                      data["other_movements"])

    def test_daily_after_partial_month_and_month_updates(self):
        daily = DAILY.read_bytes()
        empty = MONTHLY.read_bytes().splitlines()[0]
        import_reports(self.db, [empty], self.rules, monthly_end="2026-04-18T07:00:00-03:00")
        import_reports(self.db, [], self.rules, daily=[daily])
        self.assertEqual(daily_gap_count(self.db, "2026-04"), 0)
        import_report(self.db, MONTHLY.read_bytes(), self.rules)
        self.assertEqual(daily_gap_count(self.db, "2026-04"), 1)
        import_report(self.db, daily, self.rules)
        self.assertEqual(daily_gap_count(self.db, "2026-04"), 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 6)

    def test_empty_monthly_csv_with_explicit_month(self):
        empty = self.home / "empty.csv"
        empty.write_bytes(MONTHLY.read_bytes().splitlines()[0])
        self.cli("sync", "--csv", str(empty), "--month", "2026-04", "--daily-csv", str(DAILY))
        self.assertEqual(daily_gap_count(self.db, "2026-04"), 6)

    def test_invalid_daily_batch_rolls_back(self):
        with self.assertRaises(ValueError):
            import_reports(self.db, [MONTHLY.read_bytes()], self.rules, daily=[DAILY.read_bytes(), b"bad"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM monthly_reports").fetchone()[0], 0)

    def test_daily_month_filter_and_default_credit_order(self):
        self.cli("sync", "--daily-csv", str(BOUNDARY), "--month", "2026-10")
        rows = {row["source"]: row for row in self.db.execute("SELECT * FROM movements")}
        self.assertEqual(set(rows), {str(n) for n in range(83002, 83015)} | {"83016"})
        self.assertEqual(self.db.execute("SELECT count(*) FROM monthly_reports").fetchone()[0], 0)
        self.assertEqual({source: row["kind"] for source, row in rows.items()}, {
            "83002": "yield", "83003": "yield", "83004": "transfer_in", "83005": "transfer_in",
            "83006": "transfer_in", "83007": "transfer_in", "83008": "transfer_in", "83009": "refund",
            "83010": None, "83011": None, "83012": "yield", "83013": "transfer_in", "83014": None,
            "83016": "yield",
        })
        self.assertEqual(rows["83012"]["impact_kind"], "asset_management_gain")
        self.assertEqual(rows["83013"]["impact_kind"], "loan_disbursement")

    def test_custom_credit_rules_and_whole_words(self):
        self.cli("sync", "--daily-csv", str(BOUNDARY), "--month", "2026-10")
        path = self.home / "rules.toml"
        config = """
[yield_heuristic]
start = "23:59"
end = "00:01"
max_amount = "10.00"
category = "Interés de prueba"

[credit_fallback]
enabled = false

[[rule]]
field = "description"
word = "gas"
kind = "purchase"
category = "Servicios"
direction = "debit"
"""
        path.write_text(config, encoding="utf-8")
        self.cli("sync", "--daily-csv", str(BOUNDARY), "--month", "2026-09")
        self.cli("reclassify")
        for source, expected in {
            "83001": ("yield", "Interés de prueba"), "83002": (None, None), "83003": (None, None),
            "83004": (None, None), "83010": (None, None), "83011": ("purchase", "Servicios"),
        }.items():
            row = self.db.execute("SELECT kind,category FROM movements WHERE source=?", (source,)).fetchone()
            self.assertEqual(tuple(row), expected)
        path.write_text(config.replace("23:59", "25:00"), encoding="utf-8")
        self.cli("reclassify", expected=1)

    def test_import_classifications_survive_rules_and_reimport(self):
        self.cli("sync", "--daily-csv", str(DAILY), "--month", "2026-04")
        for _ in range(2):
            self.assertIn("manualmente: 3", self.cli("import-classifications", str(CLASSIFICATIONS)))
        self.assertIn("reclasificados: 0", self.cli("reclassify"))
        self.cli("sync", "--csv", str(MONTHLY), "--daily-csv", str(DAILY), "--month", "2026-04")
        rows = self.db.execute("SELECT source,kind,category,reviewed FROM movements WHERE reviewed ORDER BY source,id")
        self.assertEqual([tuple(row) for row in rows], [
            ("72001", "transfer_in", "Aportes, octubre", 1), ("72001", "transfer_in", "Aportes, octubre", 1),
            ("72005", "purchase", "Materiales", 1),
        ])

    def test_manual_payout_classifications_take_priority(self):
        (self.home / "rules.toml").write_text("""
[[rule]]
field = "transaction_type"
exact = "PAYOUT"
kind = "withdrawal"
category = "Retiros"
""", encoding="utf-8")
        daily = FIXTURE.with_name("settlement-payouts.csv")
        monthly = FIXTURE.with_name("settlement-payouts-monthly.csv")
        batch = FIXTURE.with_name("classifications-payouts.csv")
        self.cli("sync", "--daily-csv", str(daily))
        self.assertEqual([row[0] for row in self.db.execute("SELECT DISTINCT kind FROM movements")], ["withdrawal"])
        self.assertIn("manualmente: 1", self.cli("import-classifications", str(batch)))
        movement = self.db.execute("SELECT id FROM movements WHERE source='84002'").fetchone()[0]
        self.cli("review", "--id", str(movement), "--kind", "transfer_out", "--category", "Envío manual")
        self.cli("sync", "--csv", str(monthly))
        self.assertIn("reclasificados: 0", self.cli("reclassify"))
        rows = self.db.execute("SELECT source,kind,category,reviewed FROM movements ORDER BY source,amount")
        self.assertEqual([tuple(row) for row in rows], [
            ("84001", "transfer_out", "Giros de prueba", 1),
            ("84001", "transfer_out", "Giros de prueba", 1),
            ("84002", "transfer_out", "Envío manual", 1),
            ("84003", "withdrawal", "Retiros", 0),
        ])
        data = json.loads(self.cli("report", "--month", "2026-04", "--json"))
        self.assertEqual(data["totals"], [])
        self.assertIn({"currency": "ARS", "kind": "transfer_out", "category": "Giros de prueba", "net_cents": -3000},
                      data["other_movements"])

    def test_credit_fallback_with_empty_category(self):
        self.cli("sync", "--daily-csv", str(DAILY))
        rules = self.home / "rules.toml"
        for category in ('category = "Banco de prueba"', '', 'category = ""', 'category = "  "'):
            with self.subTest(category=category):
                rules.write_text(f"[credit_fallback]\nenabled = true\n{category}\n", encoding="utf-8")
                self.cli("reclassify")
                data = json.loads(self.cli("report", "--month", "2026-04", "--json"))
                expected = "Banco de prueba" if "Banco" in category else ""
                self.assertIn({"currency": "ARS", "kind": "transfer_in", "category": expected, "net_cents": 2000},
                              data["other_movements"])
                self.assertFalse(any(row["source"] == "72004" for row in json.loads(self.cli("review", "--json"))))

    def test_invalid_classification_batch_rolls_back(self):
        self.cli("sync", "--daily-csv", str(DAILY))
        batch = self.home / "manual.csv"
        for invalid in ("99999,purchase,Desconocido", "72004,invalido,Algo", "72004,transfer_in, ",
                        "72005,loan,Contradice la primera fila", "72004,transfer_in", ",purchase,Algo"):
            with self.subTest(invalid=invalid):
                batch.write_text("source_id,kind,category\n72005,purchase,Materiales\n" + invalid,
                                 encoding="utf-8-sig")
                self.cli("import-classifications", str(batch), expected=1)
                self.assertEqual(self.db.execute("SELECT count(*) FROM movements WHERE reviewed").fetchone()[0], 0)

    def test_legacy_ledger_migrates_without_changing_reviewed_id(self):
        home = self.home / "legacy"
        home.mkdir()
        stamp = "2026-04-18T08:00:00-03:00"
        with sqlite3.connect(home / "ledger.sqlite3") as old:
            old.execute("""CREATE TABLE movements (
                id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
                date TEXT NOT NULL, event TEXT NOT NULL, amount INTEGER NOT NULL, currency TEXT NOT NULL,
                description TEXT NOT NULL, kind TEXT, category TEXT, reviewed INTEGER NOT NULL DEFAULT 0
            )""")
            old.execute("INSERT INTO movements VALUES(88,'old.1','72001',?,'SETTLEMENT',27,'ARS','','yield','Manual',1)",
                        (stamp,))
        old.close()
        with database(home) as upgraded:
            import_reports(upgraded, [MONTHLY.read_bytes()], self.rules, daily=[DAILY.read_bytes()])
            rows = upgraded.execute("SELECT id,amount,category FROM movements WHERE source='72001' ORDER BY amount")
            self.assertEqual([tuple(row) for row in rows], [(88, 27, "Manual"), (89, 111, "Rendimientos")])

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
        self.assertIn("| ARS | 14,80 | 12,00 | 11,45 |", report)
        self.assertIn("| USD | 0,00 | 0,00 | 5,00 |", report)
        self.assertEqual(monthly_data(self.db, "2026-04")["totals"][0], {
            "currency": "ARS", "total_cents": 3825, "account_cents": 1480, "matched_cents": 1200, "to_add_cents": 1145,
        })
        self.assertIn("| 71 | ARS | 1 | 23,45 |", report)
        self.assertIn("Compras sin total del comprador: 1", report)
        self.assertEqual(len(review_queue(self.db)), 1)
        reconcile(self.db, "71002", "May-1", "11.45", "ARS")
        self.assertIn("| ARS | 14,80 | 23,45 | 0,00 |", monthly_report(self.db, "2026-04"))

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

    def cli(self, *args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(["--home", str(self.home), *args])
        self.assertEqual(status, expected, err.getvalue())
        return out.getvalue()

    def test_json_review(self):
        self.assertEqual(json.loads(self.cli("review", "--json")), [])
        self.cli("sync", "--csv", str(FIXTURE))
        rows = json.loads(self.cli("review", "--json"))
        row = next(r for r in rows if r["source"] == "71005")
        self.assertEqual((row["amount_cents"], row["currency"], row["description"]),
                         (-800, "ARS", "Molino de cartón"))
        updated = json.loads(self.cli("review", "--json", "--id", str(row["id"]), "--kind", "purchase",
                                      "--category", "Materiales"))
        self.assertEqual(updated["category"], "Materiales")
        self.assertNotIn(row, json.loads(self.cli("review", "--json")))

    def test_import_reconciliations_and_report_formats(self):
        with self.db:
            self.db.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,1)", [
                ("71002", "2026-04-10", "71", "ARS", 2345, "Cometas", "credit_card"),
                ("71006", "2026-04-12", "72", "USD", 500, "Luna", "credit_card")])
        (self.home / "rules.toml").write_text('[issuers]\n71 = "Banco de cartón"\n"72" = "Banco Nube"\n',
                                             encoding="utf-8")
        self.cli("sync", "--csv", str(FIXTURE))
        batch = self.home / "lines.csv"
        batch.write_text('payment,line\n71002,"Resumen, línea 3"\n', encoding="utf-8-sig")
        for _ in range(2):
            self.cli("reconcile", "--import", str(batch))
        batch.write_text("payment,line,amount,currency\n71006,Cuota-1,2.00,USD\n", encoding="utf-8")
        self.cli("reconcile", "--import", str(batch))
        args = ("report", "--month", "2026-04")
        data = json.loads(self.cli(*args, "--json"))
        self.assertEqual(data["totals"], [
            {"currency": "ARS", "total_cents": 2345, "account_cents": 0, "matched_cents": 2345, "to_add_cents": 0},
            {"currency": "USD", "total_cents": 500, "account_cents": 0, "matched_cents": 200, "to_add_cents": 300}])
        for key in ("cards", "issuers"):
            self.assertEqual([(r["issuer_id"], r["issuer"]) for r in data[key]],
                             [("71", "Banco de cartón"), ("72", "Banco Nube")])
        markdown = self.cli(*args)
        self.assertIn("| Banco de cartón | ARS | 1 | 23,45 |", markdown)
        self.assertIn("| 71006 | Luna | Banco Nube | USD | 5,00 | 2,00 |", markdown)
        self.assertIn("| ARS | 0,00 | 23,45 | 0,00 |", markdown)
        output = self.home / "report.json"
        self.cli(*args, "--json", "--output", str(output))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), data)

    def test_invalid_batch_rolls_back_and_can_be_retried(self):
        with self.db:
            self.db.execute("INSERT INTO cards VALUES('71002','2026-04-10','71','ARS',2345,'','credit_card',1)")
        batch = self.home / "lines.csv"
        first = "payment,line,amount,currency\n71002,Cuota-1,12,ARS\n"
        for invalid in ("71002,Cuota-1,11,ARS", "71002,Cuota-2,12,ARS", "71002,Cuota-2,11,USD",
                        "71003,Cuota-2,11,ARS", "71002,Cuota-2", "71002,Cuota-2,1,ARS,extra"):
            with self.subTest(invalid=invalid):
                batch.write_text(first + invalid, encoding="utf-8")
                self.cli("reconcile", "--import", str(batch), expected=1)
                self.assertEqual(self.db.execute("SELECT count(*) FROM statement_lines").fetchone()[0], 0)
        batch.write_text(first + "71002,Cuota-2,11.45,ARS", encoding="utf-8")
        self.cli("reconcile", "--import", str(batch))
        self.assertEqual(self.db.execute("SELECT sum(amount) FROM statement_lines").fetchone()[0], 2345)
