"""Authenticated transport and the official report and payer endpoints."""

import json
import os
import time
from decimal import Decimal
from functools import cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.mercadopago.com"
REPORT = "/v1/account/settlement_report"
CARD_TYPES = ("credit_card", "debit_card")


def report_task_id(report):
    # Older list responses distinguish the task_id from the generated report id.
    return str(report.get("task_id") or report.get("id") or report.get("report_id") or "")


def ready_files(report):
    if str(report.get("status")).lower() not in ("processed", "available"):
        return []
    if report.get("file_name"):
        return [report["file_name"]]
    return [f["name"] for f in report.get("files") or []
            if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"].lower().endswith(".csv")]


class MercadoPagoError(RuntimeError):
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def access_token(env_file=Path(".env")):
    value = os.getenv("MP_ACCESS_TOKEN", "").strip()
    if not value and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            key, sep, raw = line.partition("=")
            if sep and key.strip() == "MP_ACCESS_TOKEN":
                value = raw.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                break
    if not value or not value.isascii() or not value.isprintable() or any(c.isspace() for c in value):
        raise ValueError("Set a valid MP_ACCESS_TOKEN in the environment or .env.")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class MercadoPagoClient:
    def __init__(self, *, env_file=Path(".env"), opener=None):
        self._token = access_token(env_file)
        self._open = opener or build_opener(_NoRedirect()).open

    def _object(self, method, path, payload=None):
        result = self._request_json(method, path, payload)
        if not isinstance(result, dict):
            raise MercadoPagoError("Expected an object from Mercado Pago.")
        return result

    def get_report_config(self):
        return self._object("GET", REPORT + "/config")

    def list_reports(self):
        rows = self._request_json("GET", REPORT + "/list")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise MercadoPagoError("Invalid report list.")
        return rows

    def create_report(self, *, begin_date, end_date):
        task = self._object("POST", REPORT, {"begin_date": begin_date, "end_date": end_date})
        if not report_task_id(task):
            raise MercadoPagoError("Missing report task ID.")
        return task

    def get_report_task(self, task_id):
        return self._object("GET", REPORT + "/task/" + quote(str(task_id), safe=""))

    def get_user_id(self):
        payer = str(self._object("GET", "/users/me").get("id"))
        if not payer.isdigit():
            raise MercadoPagoError("Missing payer ID.")
        return payer

    def get_payment(self, source_id):
        return self._object("GET", "/v1/payments/" + quote(source_id, safe=""))

    def payment_enricher(self, account):
        @cache
        def enrich(source):
            if not source.isascii() or not source.isdecimal():
                return {}
            try:
                payment = self.get_payment(source)
            except MercadoPagoError as exc:
                # A settlement source can identify another kind of operation or
                # a payment whose details this account cannot query.
                if exc.status in (400, 403, 404):
                    return {}
                raise
            description = _label(payment.get("description"))
            counterparty = _label(payment.get("counterparty"))
            payer, collector = payment.get("payer"), payment.get("collector")
            payer = payer if isinstance(payer, dict) else {}
            collector = collector if isinstance(collector, dict) else {}
            collector_id = payment.get("collector_id") or collector.get("id")
            payer_id = payer.get("id") or payment.get("payer_id")
            other, other_id = {}, None
            if str(collector_id) == account:
                other, other_id = payer, payer_id
            elif str(payer_id) == account:
                other, other_id = collector, collector_id
            name = (_label(other.get("name")) or
                    " ".join(filter(None, (_label(other.get("first_name")), _label(other.get("last_name"))))))
            return {"description": description,
                    "counterparty": counterparty or name or description or str(other_id or "")}

        return enrich

    def search_card_payments(self, payer_id, payment_type, begin_date, end_date):
        if payment_type not in CARD_TYPES:
            raise ValueError("Unsupported card type.")
        offset = 0
        while True:
            query = urlencode({
                "payer.id": payer_id, "payment_type_id": payment_type, "status": "approved",
                "range": "date_created", "sort": "id", "criteria": "asc",
                "begin_date": begin_date, "end_date": end_date, "limit": 50, "offset": offset,
            })
            page = self._object("GET", "/v1/payments/search?" + query)
            rows, paging = page.get("results"), page.get("paging")
            if not isinstance(rows, list) or not isinstance(paging, dict):
                raise MercadoPagoError("Invalid payment search result.")
            total = paging.get("total")
            if not isinstance(total, int) or total < 0 or paging.get("offset") != offset:
                raise MercadoPagoError("Inconsistent payment pagination.")
            for row in rows:
                if not isinstance(row, dict):
                    raise MercadoPagoError("Malformed payment.")
                nested = row.get("payer") or {}
                payers = (row.get("payer_id"), nested.get("id") if isinstance(nested, dict) else None)
                if ({str(p) for p in payers if p is not None} == {str(payer_id)}
                        and row.get("status") == "approved"
                        and row.get("payment_type_id") == payment_type
                        and row.get("operation_type") != "card_validation"):
                    yield row
            offset += len(rows)
            if offset >= total:
                return
            if not rows:
                raise MercadoPagoError("Incomplete payment page.")

    def _request(self, method, path, payload=None):
        request = Request(API + path, method=method,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + self._token,
                                   "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with self._open(request, timeout=45) as response:
                    return response.read()
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if method != "GET" or attempt == 2 or status not in (429, 500, 502, 503, 504):
                    raise MercadoPagoError(f"Mercado Pago returned HTTP {status}.", status=status) from None
            except (URLError, OSError):
                if method != "GET" or attempt == 2:
                    raise MercadoPagoError("Could not reach Mercado Pago.") from None
            time.sleep(2 ** attempt)

    def _request_json(self, method, path, payload=None):
        try:
            return json.loads(self._request(method, path, payload), parse_float=Decimal)
        except (UnicodeError, ValueError):
            raise MercadoPagoError("Invalid JSON from Mercado Pago.") from None

    def download_report(self, filename):
        if not isinstance(filename, str) or not filename.lower().endswith(".csv"):
            raise MercadoPagoError("Expected a CSV report filename.")
        if "/" in filename or "\\" in filename:
            raise MercadoPagoError("Invalid report filename.")
        return self._request("GET", REPORT + "/" + quote(filename, safe=""))


def _label(value):
    return value.strip() if isinstance(value, str) else ""
