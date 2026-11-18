"""Authenticated transport and the official report and payer endpoints."""

import json
import os
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://api.mercadopago.com"
REPORT = "/v1/account/settlement_report"
CARD_TYPES = ("credit_card", "debit_card")


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


class Endpoints:
    """Shared by the standalone client and the optional AnalisisGastos adapter."""

    error_type = MercadoPagoError

    def _object(self, method, path, payload=None):
        result = self._request_json(method, path, payload)
        if not isinstance(result, dict):
            raise self.error_type("Expected an object from Mercado Pago.")
        return result

    def get_report_config(self):
        return self._object("GET", REPORT + "/config")

    def list_reports(self):
        rows = self._request_json("GET", REPORT + "/list")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise self.error_type("Invalid report list.")
        return rows

    def create_report(self, *, begin_date, end_date):
        task = self._object("POST", REPORT, {"begin_date": begin_date, "end_date": end_date})
        if not (task.get("id") or task.get("report_id")):
            raise self.error_type("Missing report task ID.")
        return task

    def get_report_task(self, task_id):
        return self._object("GET", REPORT + "/task/" + quote(str(task_id), safe=""))

    def get_user_id(self):
        payer = str(self._object("GET", "/users/me").get("id"))
        if not payer.isdigit():
            raise self.error_type("Missing payer ID.")
        return payer

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
                raise self.error_type("Invalid payment search result.")
            total = paging.get("total")
            if not isinstance(total, int) or total < 0 or paging.get("offset") != offset:
                raise self.error_type("Inconsistent payment pagination.")
            for row in rows:
                if not isinstance(row, dict):
                    raise self.error_type("Malformed payment.")
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
                raise self.error_type("Incomplete payment page.")


class MercadoPagoClient(Endpoints):
    def __init__(self, *, env_file=Path(".env"), opener=None):
        self._token = access_token(env_file)
        self._open = opener or build_opener(_NoRedirect()).open

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
