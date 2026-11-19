"""Build monthly ledger data and render it as Markdown."""

from collections import Counter, defaultdict
from decimal import Decimal

from .ledger import daily_gap_count
from .sync import month_start


def money(value):
    return f"{Decimal(value) / 100:.2f}".replace(".", ",")


def cell(value):
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def monthly_data(db, month, issuers=None):
    month_start(month)
    issuers = {str(key): name for key, name in (issuers or {}).items()}
    totals, matched, categories, other = Counter(), Counter(), Counter(), Counter()
    pending = unknown = conflicts = 0
    groups = defaultdict(lambda: [0, 0])
    for row in db.execute("""
        SELECT m.*, EXISTS(SELECT 1 FROM cards c WHERE c.id=m.source AND c.active) AS card
        FROM movements m WHERE substr(m.date,1,7)=?
    """, (month,)):
        kind, amount, code = row["kind"], row["amount"], row["currency"]
        if row["card"] and amount < 0 and kind in (None, "purchase"):
            continue
        if kind is None:
            pending += 1
        elif kind in ("purchase", "fee", "refund"):
            totals[code] -= amount
            categories[(code, row["category"])] -= amount
        else:
            other[(code, kind, row["category"])] += amount
    card_rows = []
    for row in db.execute("""
        SELECT c.*, COALESCE(SUM(s.amount),0) AS linked,
               COUNT(CASE WHEN s.currency != c.currency THEN 1 END) AS wrong_currency
        FROM cards c LEFT JOIN statement_lines s ON s.payment=c.id
        WHERE substr(date,1,7)=? AND (active OR s.reference IS NOT NULL)
        GROUP BY c.id ORDER BY c.date,c.id
    """, (month,)):
        code, paid, linked = row["currency"], row["paid"], row["linked"]
        if not row["active"] or row["wrong_currency"] or (paid is not None and linked > paid):
            conflicts += 1
            linked = 0
        if not row["active"]:
            continue
        group = groups[row["issuer"], code]
        group[0] += 1
        if paid is None:
            unknown += 1
        else:
            group[1] += paid
            totals[code] += paid
            matched[code] += linked
        issuer = row["issuer"]
        card_rows.append({"payment": row["id"], "description": row["description"], "issuer_id": issuer,
                          "issuer": issuers.get(issuer, issuer), "currency": code,
                          "paid_cents": paid, "matched_cents": linked})
    job = db.execute("SELECT end_at,done FROM jobs WHERE month=?", (month,)).fetchone()
    return {
        "month": month,
        "settlement": {"end_at": job["end_at"], "done": bool(job["done"])} if job else None,
        "daily_only_movements": daily_gap_count(db, month),
        "totals": [{"currency": code, "total_cents": total, "matched_cents": matched[code],
                    "to_add_cents": total - matched[code]} for code, total in sorted(totals.items())],
        "pending_movements": pending, "unknown_card_totals": unknown, "reconciliation_conflicts": conflicts,
        "categories": [{"currency": code, "category": category, "amount_cents": value}
                       for (code, category), value in sorted(categories.items())],
        "other_movements": [{"currency": code, "kind": kind, "category": category, "net_cents": value}
                            for (code, kind, category), value in sorted(other.items())],
        "issuers": [{"issuer_id": issuer, "issuer": issuers.get(issuer, issuer), "currency": code,
                     "count": count, "total_cents": value} for (issuer, code), (count, value) in sorted(groups.items())],
        "cards": card_rows,
    }


def monthly_report(db, month, issuers=None):
    data = monthly_data(db, month, issuers)
    lines = [f"# Gastos de {month}", "", "| Moneda | Gasto conocido | Ya en resumen | Para sumar al resumen |",
             "|---|---:|---:|---:|"]
    for r in data["totals"]:
        lines.append(f"| {r['currency']} | {money(r['total_cents'])} | {money(r['matched_cents'])} | "
                     f"{money(r['to_add_cents'])} |")
    job = data["settlement"]
    coverage = "Sin sincronización de reporte por API para este mes."
    if job:
        state = "importado" if job["done"] else "pendiente"
        coverage = f"Reporte de cuenta {state} hasta {job['end_at']}."
    if data["daily_only_movements"]:
        coverage += f" Movimientos de los diarios que faltan en el mensual: {data['daily_only_movements']}."
    lines += ["", coverage, f"Movimientos sin clasificar: {data['pending_movements']}. "
              f"Compras sin total del comprador: {data['unknown_card_totals']}.",
              f"Conciliaciones que requieren revisión: {data['reconciliation_conflicts']}.",
              "", "## Gastos de la cuenta por categoría", "", "| Moneda | Categoría | Importe |", "|---|---|---:|"]
    for r in data["categories"]:
        lines.append(f"| {r['currency']} | {cell(r['category'])} | {money(r['amount_cents'])} |")
    if data["other_movements"]:
        lines += ["", "## Otros movimientos de la cuenta", "",
                  "| Moneda | Tipo | Categoría | Neto |", "|---|---|---|---:|"]
        for r in data["other_movements"]:
            lines.append(f"| {r['currency']} | {r['kind']} | {cell(r['category'])} | {money(r['net_cents'])} |")
    lines += ["", "## Compras con tarjeta", "", "| Emisor | Moneda | Compras | Total conocido |",
              "|---|---|---:|---:|"]
    for r in data["issuers"]:
        lines.append(f"| {cell(r['issuer'])} | {r['currency']} | {r['count']} | {money(r['total_cents'])} |")
    lines += ["", "| Pago MP | Descripción | Emisor | Moneda | Total comprador | Conciliado |",
              "|---|---|---|---|---:|---:|"]
    for r in data["cards"]:
        paid = money(r["paid_cents"]) if r["paid_cents"] is not None else "Pendiente"
        lines.append(f"| {r['payment']} | {cell(r['description'])} | {cell(r['issuer'])} | "
                     f"{r['currency']} | {paid} | {money(r['matched_cents'])} |")
    return "\n".join(lines) + "\n"
