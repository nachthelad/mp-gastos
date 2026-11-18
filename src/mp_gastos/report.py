"""Render the monthly Markdown ledger without mixing currencies or sources."""

from collections import Counter, defaultdict
from decimal import Decimal

from .sync import month_start


def money(value):
    return f"{Decimal(value) / 100:.2f}".replace(".", ",")


def cell(value):
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def monthly_report(db, month):
    month_start(month)
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
        card_rows.append(f"| {row['id']} | {cell(row['description'])} | {cell(row['issuer'])} | "
                         f"{code} | {money(paid) if paid is not None else 'Pendiente'} | {money(linked)} |")
    lines = [f"# Gastos de {month}", "", "| Moneda | Gasto conocido | Ya en resumen | Para sumar al resumen |",
             "|---|---:|---:|---:|"]
    for code, total in sorted(totals.items()):
        lines.append(f"| {code} | {money(total)} | {money(matched[code])} | {money(total-matched[code])} |")
    job = db.execute("SELECT end_at,done FROM jobs WHERE month=?", (month,)).fetchone()
    coverage = "Sin sincronización de reporte por API para este mes."
    if job:
        state = "importado" if job["done"] else "pendiente"
        coverage = f"Reporte de cuenta {state} hasta {job['end_at']}."
    lines += ["", coverage, f"Movimientos sin clasificar: {pending}. Compras sin total del comprador: {unknown}.",
              f"Conciliaciones que requieren revisión: {conflicts}.",
              "", "## Gastos de la cuenta por categoría", "", "| Moneda | Categoría | Importe |", "|---|---|---:|"]
    for (code, category), value in sorted(categories.items()):
        lines.append(f"| {code} | {cell(category)} | {money(value)} |")
    if other:
        lines += ["", "## Otros movimientos de la cuenta", "",
                  "| Moneda | Tipo | Categoría | Neto |", "|---|---|---|---:|"]
        for (code, kind, category), value in sorted(other.items()):
            lines.append(f"| {code} | {kind} | {cell(category)} | {money(value)} |")
    lines += ["", "## Compras con tarjeta", "", "| Emisor | Moneda | Compras | Total conocido |",
              "|---|---|---:|---:|"]
    for (issuer, code), (count, value) in sorted(groups.items()):
        lines.append(f"| {cell(issuer)} | {code} | {count} | {money(value)} |")
    lines += ["", "| Pago MP | Descripción | Emisor | Moneda | Total comprador | Conciliado |",
              "|---|---|---|---|---:|---:|", *card_rows]
    return "\n".join(lines) + "\n"
