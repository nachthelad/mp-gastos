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
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ARGENTINA = timezone(timedelta(hours=-3))
KINDS = ("purchase", "transfer", "transfer_in", "transfer_out", "card_payment", "yield", "fee", "refund",
         "withdrawal", "loan", "other")
REQUIRED = {"SOURCE_ID", "TRANSACTION_DATE", "TRANSACTION_TYPE", "SETTLEMENT_NET_AMOUNT",
            "TRANSACTION_CURRENCY"}
DETAIL_FIELDS = ("description", "external_reference", "payment_method_type", "counterparty", "impact_kind")
FIELDS = {"transaction_type", *DETAIL_FIELDS}
OPERATORS = {"contains", "exact", "prefix", "word"}
DEFAULT_RULES = Path(__file__).with_name("rules.toml")
YIELD_MARKER = re.compile(
    r"\b(rendimientos?|yield|investment interest|money market|asset management gain)\b"
)


@dataclass(frozen=True)
class YieldHeuristic:
    start: time
    end: time
    max_cents: int
    category: str


@dataclass(frozen=True)
class ClassificationRules:
    rules: list
    yield_heuristic: YieldHeuristic | None = None
    credit_fallback: str | None = None


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
    if config.keys() - {"rule", "issuers", "yield_heuristic", "credit_fallback"}:
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


def _classification_section(config, name, fields, default_category, *, allow_empty_category=False):
    section = config.get(name, {})
    if not isinstance(section, dict) or section.keys() - (fields | {"enabled", "category"}):
        raise ValueError(f"Invalid [{name}] section.")
    enabled = section.get("enabled", name in config)
    category = section.get("category", default_category)
    if not isinstance(enabled, bool) or not isinstance(category, str):
        raise ValueError(f"Expected a boolean enabled and a string category in [{name}].")
    if not allow_empty_category and not category.strip():
        raise ValueError(f"Expected a boolean enabled and a nonempty category in [{name}].")
    return category.strip() if enabled else None


def _clock(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError("Expected HH:MM in [yield_heuristic].")
    return time.fromisoformat(value)


def load_rules(path):
    config = _load_config(path)
    rules = config.get("rule", [])
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
    category = _classification_section(config, "yield_heuristic", {"start", "end", "max_amount"}, "Rendimientos")
    heuristic = None
    if category is not None:
        section = config["yield_heuristic"]
        if "max_amount" not in section or (limit := cents(section["max_amount"])) <= 0:
            raise ValueError("Expected a positive max_amount in [yield_heuristic].")
        heuristic = YieldHeuristic(_clock(section.get("start", "00:00")), _clock(section.get("end", "02:59")),
                                   limit, category)
    fallback = _classification_section(config, "credit_fallback", set(), "", allow_empty_category=True)
    return ClassificationRules(rules, heuristic, fallback)


def classify(row, rules):
    if not isinstance(rules, ClassificationRules):
        rules = ClassificationRules(rules)
    amount = row["amount"]
    direction = "debit" if amount < 0 else "credit" if amount > 0 else "neutral"
    for rule in rules.rules:
        if rule.get("direction", "any") not in ("any", direction):
            continue
        field = rule["field"]
        value = row["event"] if field == "transaction_type" else row[field]
        if field == "description" and not value:
            value = row["counterparty"]
        value = normalize(value)
        if (("contains" in rule and normalize(rule["contains"]) in value)
                or ("exact" in rule and normalize(rule["exact"]) == value)
                or ("prefix" in rule and value.startswith(normalize(rule["prefix"])))
                or ("word" in rule and re.search(r"\b" + re.escape(normalize(rule["word"])) + r"\b", value))):
            return rule["kind"], rule["category"]
    if amount < 0 and normalize(row["external_reference"]).startswith(("ccpaymentprod_", "cctiprod-")):
        return "card_payment", "Tarjeta"
    if amount > 0 and row["event"] == "SETTLEMENT":
        for field in ("description", "counterparty", "external_reference", "impact_kind", "payment_method_type"):
            if YIELD_MARKER.search(normalize(row[field]).replace("_", " ").replace("-", " ")):
                return "yield", "Rendimientos"
        heuristic = rules.yield_heuristic
        if (heuristic and amount <= heuristic.max_cents
                and not any(normalize(row[field]) for field in ("description", "counterparty", "external_reference"))):
            clock = local_date(row["date"]).time().replace(second=0, microsecond=0)
            if heuristic.start <= heuristic.end:
                in_window = heuristic.start <= clock <= heuristic.end
            else:
                in_window = clock >= heuristic.start or clock <= heuristic.end
            if in_window:
                return "yield", heuristic.category
    if amount > 0 and rules.credit_fallback is not None:
        return "transfer_in", rules.credit_fallback
    return None, None


def impact_kind(data):
    value = data.get("impact_kind")
    if isinstance(value, str) and value.strip():
        return value.strip()
    metadata = data.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return ""
    value = metadata.get("impact_kind") if isinstance(metadata, dict) else None
    return value.strip() if isinstance(value, str) else ""


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
        movement["impact_kind"] = impact_kind({"impact_kind": movement["impact_kind"],
                                               "metadata": row.get("METADATA")})
        movement["identity"] = movement_identity(movement, occurrences)
        yield movement
