import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from mp_gastos.client import REPORT, MercadoPagoClient, MercadoPagoError, access_token
from mp_gastos.cli import main, parser
from mp_gastos.ledger import database, initialize
from mp_gastos.sync import sync, windows

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime.fromisoformat("2026-05-18T11:00:00-03:00")


class Server:
    """Serve one result per page to exercise the offset contract."""

    def __init__(self):
        self.cards = json.loads((FIXTURES / "payments.json").read_text(encoding="utf-8"))
        self.fixtures = json.loads((FIXTURES / "reports.json").read_text())
        self.list_rows = []
        self.task_reply = None
        self.task_lookups = 0
        self.config = {}
        self.tasks = []
        self.ready = False
        self.visible = False
        self.terminal = False
        self.payer = 430
        self.corrupt_debit = False
        self.queries = []

    def __call__(self, request, *, timeout):
        assert request.get_header("Authorization") == "Bearer fixture-only"
        url = urlsplit(request.full_url)
        assert url.hostname == "api.mercadopago.com"
        if url.path == "/users/me":
            reply = {"id": self.payer}
        elif url.path == REPORT + "/config":
            if request.method == "POST":
                self.config = json.loads(request.data)
            reply = self.config
        elif url.path == REPORT + "/list":
            reply = self.list_rows
            if self.visible:
                reply = [dict(**self.tasks[-1], task_id=301, id=901, status="processed",
                              file_name="fictional.csv")]
        elif url.path == REPORT:
            assert request.method == "POST"
            self.tasks.append(json.loads(request.data))
            reply = {"id": 301}
        elif url.path == REPORT + "/task/301":
            self.task_lookups += 1
            reply = {"status": "processing"}
            if self.ready:
                reply = self.fixtures["task_processed"]
            if self.task_reply is not None:
                reply = self.task_reply
            if self.terminal:
                reply = {"status": "failed"}
        elif url.path == REPORT + "/fictional.csv":
            return io.BytesIO((FIXTURES / "settlement.csv").read_bytes())
        elif url.path == "/v1/payments/search":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            self.queries.append(q)
            offset = int(q["offset"])
            rows = [r for r in self.cards if r["payment_type_id"] == q["payment_type_id"]]
            reply = {"results": rows[offset:offset+1], "paging": {"total": len(rows), "offset": offset}}
            if self.corrupt_debit and q["payment_type_id"] == "debit_card":
                reply["paging"]["offset"] = 900
        else:
            raise AssertionError(url.path)
        return io.BytesIO(json.dumps(reply).encode())


class SyncTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        initialize(self.home)
        self.db = self.enterContext(database(self.home))
        self.enterContext(patch.dict(os.environ, {"MP_ACCESS_TOKEN": "fixture-only"}))
        self.server = Server()
        self.client = MercadoPagoClient(opener=self.server)

    def run_sync(self):
        return sync(self.db, self.client, "2026-04", now=NOW, rules_path=self.home / "rules.toml", wait=0)

    def test_pending_resume_and_filters(self):
        base = self.server.cards[0]
        for overrides in ({"payer_id": 999}, {"payer": {"id": 999}}, {"status": "rejected"},
                          {"operation_type": "card_validation"}):
            self.server.cards.append(dict(base, id=len(self.server.cards), **overrides))
        self.assertEqual(self.run_sync(), {"2026-04": "pending"})
        self.server.ready = True
        self.assertEqual(self.run_sync(), {"2026-04": "imported"})
        self.server.visible = True
        self.run_sync()
        self.assertEqual(len(self.server.tasks), 1)
        self.assertEqual(self.server.config["separator"], ";")
        self.assertEqual(self.server.tasks[0], {"begin_date": "2026-04-01T03:00:00.000Z",
                                               "end_date": "2026-05-01T02:59:59.999Z"})
        self.assertIn("4", {q["offset"] for q in self.server.queries})
        for q in self.server.queries:
            self.assertEqual((q["payer.id"], q["status"]), ("430", "approved"))
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT id,paid FROM cards ORDER BY id")],
                         [("71002", 2345), ("71006", 500)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 6)

    def test_discover_report_with_truncated_end_without_post(self):
        self.server.list_rows = [self.server.fixtures["list_processed"]]
        self.assertEqual(self.run_sync(), {"2026-04": "imported"})
        self.assertEqual(tuple(self.db.execute("SELECT task,done FROM jobs").fetchone()), ("301", 1))
        self.assertEqual(self.server.tasks, [])
        self.assertEqual(self.server.task_lookups, 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 6)

    def test_discover_task_by_list_id_then_download_nested_name(self):
        self.server.list_rows = [self.server.fixtures["list_processing"]]
        self.server.task_reply = self.server.fixtures["task_available"]
        self.assertEqual(self.run_sync(), {"2026-04": "imported"})
        self.assertEqual(self.server.tasks, [])
        self.assertEqual(self.server.task_lookups, 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 6)

    def test_pending_job_finds_list_file_by_id(self):
        self.run_sync()
        row = self.server.fixtures["list_processed"].copy()
        del row["end_date"]
        self.server.list_rows = [row]
        self.assertEqual(self.run_sync(), {"2026-04": "imported"})
        self.assertEqual(self.server.task_lookups, 1)
        self.assertEqual(len(self.server.tasks), 1)

    def test_other_cutoff_in_same_day_is_a_different_report(self):
        self.server.list_rows = [dict(self.server.fixtures["list_processed"], id=401,
                                      end_date="2026-05-01T02:59:58Z")]
        self.assertEqual(self.run_sync(), {"2026-04": "pending"})
        self.assertEqual(len(self.server.tasks), 1)

    def test_saved_end_without_milliseconds_resumes_same_window(self):
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES('2026-04','301','2026-05-01T02:59:59Z',1)")
        self.server.task_reply = self.server.fixtures["task_available"]
        self.assertEqual(self.run_sync(), {"2026-04": "imported"})
        self.assertEqual(self.server.tasks, [])
        self.assertEqual(self.server.task_lookups, 1)

    def test_failure_in_second_csv_rolls_back_the_first(self):
        self.server.task_reply = {"status": "available", "files": [
            {"name": "fictional.csv"}, {"name": "incomplete.csv"}]}
        with patch.object(self.client, "download_report", side_effect=[
                (FIXTURES / "settlement.csv").read_bytes(), b"bad headers"]):
            with self.assertRaisesRegex(ValueError, "headers"):
                self.run_sync()
        self.assertEqual(self.db.execute("SELECT count(*) FROM movements").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT done FROM jobs").fetchone()[0], 0)

    def test_cli_pending_then_available(self):
        self.assertEqual(parser().parse_args(["sync"]).wait, 180)
        args = ["--home", str(self.home), "sync", "--month", "2026-04", "--wait", "0"]
        with patch("mp_gastos.cli.MercadoPagoClient", return_value=self.client), \
             patch("mp_gastos.cli.sync", partial(sync, now=NOW)), redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 2)
            self.server.task_reply = self.server.fixtures["task_available"]
            self.assertEqual(main(args), 0)
        self.assertEqual(len(self.server.tasks), 1)

    def test_partial_pages_preserve_previous_month_snapshot(self):
        self.run_sync()
        self.server.cards[0]["transaction_details"]["total_paid_amount"] = 1000
        self.server.corrupt_debit = True
        with self.assertRaises(MercadoPagoError):
            self.run_sync()
        self.assertEqual(self.db.execute("SELECT paid FROM cards WHERE id='71002'").fetchone()[0], 2345)
        self.assertEqual(self.db.execute("SELECT count(*) FROM cards WHERE active").fetchone()[0], 2)

    def test_valid_empty_snapshot_removes_reversed_purchases_from_active_totals(self):
        self.run_sync()
        self.server.cards.clear()
        self.run_sync()
        self.assertEqual(self.db.execute("SELECT count(*) FROM cards WHERE active").fetchone()[0], 0)

    def test_month_limits_and_local_boundary(self):
        periods = list(windows("2026-05", 12, NOW))
        self.assertEqual(len(periods), 12)
        self.assertEqual(periods[0][0].strftime("%Y-%m"), "2025-06")
        self.assertEqual(periods[-1][1], NOW)
        for month, count in (("2026-06", 1), ("2025-05", 1), ("2026-05", 13),
                             ("2026-5", 1), ("2026-00", 1), ("2025-06", 2)):
            with self.subTest(month=month, count=count), self.assertRaises(ValueError):
                list(windows(month, count, NOW))

    def test_different_account_cannot_overwrite_ledger(self):
        self.run_sync()
        self.server.payer += 1
        with self.assertRaisesRegex(ValueError, "another account"):
            self.run_sync()

    def test_failed_task_can_be_recreated(self):
        self.server.terminal = True
        with self.assertRaisesRegex(MercadoPagoError, "generation failed"):
            self.run_sync()
        self.server.terminal = False
        self.run_sync()
        self.assertEqual(len(self.server.tasks), 2)

    def test_missing_paid_total_stays_unknown(self):
        del self.server.cards[0]["transaction_details"]
        self.run_sync()
        self.assertIsNone(self.db.execute("SELECT paid FROM cards WHERE id='71002'").fetchone()[0])

    def test_credentials_and_error_redaction(self):
        envfile = self.home / ".env"
        envfile.write_text('MP_ACCESS_TOKEN="dummy-file-token"\n', encoding="utf-8")
        self.assertEqual(access_token(envfile), "fixture-only")
        with patch.dict(os.environ, {"MP_ACCESS_TOKEN": ""}):
            self.assertEqual(access_token(envfile), "dummy-file-token")

        def fail(request, **kwargs):
            raise HTTPError(request.full_url, 401, "fixture-only", {}, io.BytesIO(b"fixture-only"))

        with self.assertRaises(MercadoPagoError) as raised:
            MercadoPagoClient(opener=fail).get_user_id()
        self.assertNotIn("fixture-only", str(raised.exception))
