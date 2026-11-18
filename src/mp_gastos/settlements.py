"""Settlement rows, exact money and user-editable classification rules."""

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
KINDS = ("purchase", "transfer", "card_payment", "yield", "fee", "refund", "withdrawal", "other")
REQUIRED = {"SOURCE_ID", "TRANSACTION_DATE", "TRANSACTION_TYPE", "SETTLEMENT_NET_AMOUNT",
            "TRANSACTION_CURRENCY"}
FIELDS = {"description", "transaction_type", "external_reference", "payment_method_type"}
DEFAULT_RULES = Path(__file__).with_name("rules.toml")


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


def load_rules(path):
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    if config.keys() - {"rule"}:
        raise ValueError("Unknown rules file section.")
    rules = config.get("rule", [])
    if not isinstance(rules, list):
        raise ValueError("Expected a list of [[rule]] sections.")
    for rule in rules:
        if (not isinstance(rule, dict) or rule.keys() - {"field", "contains", "kind", "category", "direction"}
                or rule.get("field") not in FIELDS or rule.get("kind") not in KINDS
                or rule.get("direction", "any") not in ("debit", "credit", "any")
                or not all(isinstance(rule.get(k), str) and rule[k].strip()
                           for k in ("contains", "category"))):
            raise ValueError("Invalid classification rule.")
    return rules


def classify(row, amount, rules):
    direction = "debit" if amount < 0 else "credit" if amount > 0 else "neutral"
    for rule in rules:
        if (rule.get("direction", "any") in ("any", direction)
                and normalize(rule["contains"]) in normalize(row.get(rule["field"].upper(), ""))):
            return rule["kind"], rule["category"]
    return None, None


def parse_report(payload, rules):
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
        # A purchase and its later refund may share SOURCE_ID. Preserve repeated
        # equal rows within one export while deduplicating overlapping exports.
        identity = [source, event, date]
        if not source:
            identity.extend((code, amount))
            identity.extend(row.get(k, "") for k in ("DESCRIPTION", "EXTERNAL_REFERENCE", "COUNTERPARTY"))
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        occurrences[digest] += 1
        kind, category = classify(row, amount, rules)
        yield (f"{digest}.{occurrences[digest]}", source, date, event, amount, code,
               row.get("DESCRIPTION", ""), kind, category)
