"""Settlement rows, exact money and user-editable classification rules."""

import calendar
import csv
import hashlib
import io
import json
import re
import tomllib
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ARGENTINA = timezone(timedelta(hours=-3))
KINDS = ("purchase", "transfer", "card_payment", "yield", "fee", "refund", "withdrawal", "loan", "other")
REQUIRED = {"SOURCE_ID", "TRANSACTION_DATE", "TRANSACTION_TYPE", "SETTLEMENT_NET_AMOUNT",
            "TRANSACTION_CURRENCY"}
DETAIL_FIELDS = ("description", "external_reference", "payment_method_type", "counterparty", "impact_kind")
FIELDS = {"transaction_type", *DETAIL_FIELDS}
OPERATORS = {"contains", "exact", "prefix"}
DEFAULT_RULES = Path(__file__).with_name("rules.toml")
YIELD_MARKER = re.compile(
    r"\b(rendimientos?|yield|investment interest|money market|asset management gain)\b"
)


def cents(value):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
        scaled = amount * 100
        if not scaled.is_finite() or scaled != scaled.to_integral_value() or abs(scaled) >= 2**63:
            raise ValueError
        return int(scaled)
    except (InvalidOperation, ValueError):
        raise ValueError("Expected an amount in whole cents.") from None


def currency(value):
    if not isinstance(value, str) or not re.fullmatch("[A-Z]{3}", value):
        raise ValueError("Expected a three-letter currency code.")
    return value


def local_date(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Expected a timestamp with a UTC offset.")
    return stamp.astimezone(ARGENTINA)


def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c)).strip()


def month_end(start):
    return start.replace(day=calendar.monthrange(start.year, start.month)[1],
                         hour=23, minute=59, second=59, microsecond=999000)


def _load_config(path):
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if config.keys() - {"rule", "issuers"}:
        raise ValueError("Unknown rules file section.")
    return config


def load_issuers(path):
    issuers = _load_config(path).get("issuers", {})
    if not isinstance(issuers, dict) or not all(
        isinstance(name, str) and name.strip() for name in issuers.values()
    ):
        raise ValueError("Expected issuer names in the [issuers] table.")
    # TOML parses bare numeric keys and quoted keys as strings.
    return {str(key): name for key, name in issuers.items()}


def load_rules(path):
    rules = _load_config(path).get("rule", [])
    if not isinstance(rules, list):
        raise ValueError("Expected a list of [[rule]] sections.")
    for rule in rules:
        if (not isinstance(rule, dict) or rule.keys() - (OPERATORS | {"field", "kind", "category", "direction"})
                or rule.get("field") not in FIELDS or rule.get("kind") not in KINDS
                or rule.get("direction", "any") not in ("debit", "credit", "any")
                or len(OPERATORS & rule.keys()) != 1
                or not all(isinstance(rule.get(k), str) and normalize(rule[k])
                           for k in {"category"} | (OPERATORS & rule.keys()))):
            raise ValueError("Invalid classification rule.")
    return rules


def classify(row, rules):
    amount = row["amount"]
    direction = "debit" if amount < 0 else "credit" if amount > 0 else "neutral"
    for rule in rules:
        if rule.get("direction", "any") not in ("any", direction):
            continue
        field = rule["field"]
        value = row["event"] if field == "transaction_type" else row[field]
        if field == "description" and not value:
            value = row["counterparty"]
        value = normalize(value)
        if (("contains" in rule and normalize(rule["contains"]) in value)
                or ("exact" in rule and normalize(rule["exact"]) == value)
                or ("prefix" in rule and value.startswith(normalize(rule["prefix"])))):
            return rule["kind"], rule["category"]
    if amount < 0 and normalize(row["external_reference"]).startswith(("ccpaymentprod_", "cctiprod-")):
        return "card_payment", "Tarjeta"
    if amount > 0 and row["event"] == "SETTLEMENT":
        for field in ("description", "counterparty", "external_reference", "impact_kind", "payment_method_type"):
            if YIELD_MARKER.search(normalize(row[field]).replace("_", " ").replace("-", " ")):
                return "yield", "Rendimientos"
    return None, None


def movement_identity(row, occurrences):
    # Financial fields match the same movement across reports with different metadata.
    key = [row[field] for field in ("source", "event", "date", "currency", "amount")]
    digest = hashlib.sha256(json.dumps(key).encode()).hexdigest()
    occurrences[digest] += 1
    return f"{digest}.{occurrences[digest]}"


def parse_report(payload):
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = payload.decode("cp1252")
    # Some MP exports embed unescaped JSON quotes inside an already quoted field.
    # Escape interior quotes only, preserving standard doubled quotes and newlines.
    repaired = []
    quoted = False
    field_start = True
    index = 0
    while index < len(text):
        char = text[index]
        after = text[index + 1:index + 2]
        repaired.append(char)
        if char == '"':
            if quoted:
                if after == '"':
                    repaired.append(after)
                    index += 1
                elif after in (";", "\r", "\n", ""):
                    quoted = False
                else:
                    repaired.append('"')
            elif field_start:
                quoted = True
        field_start = not quoted and char in ";\r\n"
        index += 1
    reader = csv.reader(io.StringIO("".join(repaired), newline=""), delimiter=";", strict=True)
    headers = [h.strip().upper() for h in next(reader, [])]
    if not REQUIRED.issubset(headers) or len(headers) != len(set(headers)):
        raise ValueError("Invalid settlement headers. Check the report columns and ';' separator.")
    occurrences = Counter()
    for values in reader:
        if not values:
            continue
        if len(values) != len(headers):
            raise ValueError(f"Wrong number of columns near line {reader.line_num}.")
        row = dict(zip(headers, (v.strip() for v in values)))
        source = row["SOURCE_ID"]
        date = local_date(row["TRANSACTION_DATE"]).isoformat()
        code = currency(row["TRANSACTION_CURRENCY"].upper())
        amount = cents(row["SETTLEMENT_NET_AMOUNT"])
        event = row["TRANSACTION_TYPE"].upper()
        if not event:
            raise ValueError("Missing settlement event type.")
        movement = dict(source=source, date=date, event=event, amount=amount, currency=code)
        movement.update({field: row.get(field.upper(), "") for field in DETAIL_FIELDS})
        movement["identity"] = movement_identity(movement, occurrences)
        yield movement
