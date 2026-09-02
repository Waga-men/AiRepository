"""Аналитика по сделкам (порт раздела «Аналитика» расширения)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta


def compute(deals: list, period_days: int | None = None) -> dict:
    """Считает сводку по завершённым продажам (direction OUT).

    period_days=None — вся загруженная история.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=period_days) if period_days else None

    sales = []
    problem_deals = []
    for d in deals:
        try:
            created = datetime.fromisoformat((d.get("created_at") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if cutoff and created < cutoff:
            continue
        status = d.get("status") or ""
        if d.get("direction") and d.get("direction") != "OUT":
            continue
        if status in ("CONFIRMED", "CONFIRMED_AUTOMATICALLY", "ROLLED_BACK", "SOLD"):
            sales.append(d)
        if d.get("has_problem") or status in ("ROLLED_BACK",):
            problem_deals.append(d)

    revenue = sum(float((d.get("item") or {}).get("price") or d.get("price") or 0) for d in sales)
    avg_check = revenue / len(sales) if sales else 0

    by_item: dict = {}
    for d in sales:
        name = (d.get("item") or {}).get("name") or "Без названия"
        price = float((d.get("item") or {}).get("price") or 0)
        by_item.setdefault(name, {"count": 0, "revenue": 0.0})
        by_item[name]["count"] += 1
        by_item[name]["revenue"] += price

    top_items = sorted(by_item.items(), key=lambda kv: kv[1]["revenue"], reverse=True)[:10]

    return {
        "sales_count": len(sales),
        "revenue": revenue,
        "avg_check": avg_check,
        "problem_count": len(problem_deals),
        "top_items": [{"name": n, "count": v["count"], "revenue": v["revenue"]} for n, v in top_items],
    }


def to_csv(deals: list) -> str:
    """Экспорт сделок в CSV."""
    rows = ["дата;товар;цена;статус;покупатель"]
    for d in deals:
        created = (d.get("created_at") or "").replace("T", " ").replace("Z", "")
        name = (d.get("item") or {}).get("name") or "Без названия"
        price = (d.get("item") or {}).get("price") or d.get("price") or 0
        status = d.get("status") or ""
        user = (d.get("user") or {}).get("username") or ""
        rows.append(
            f"{created};\"{name}\";{price};{status};\"{user}\""
        )
    return "\n".join(rows)


def items_to_csv(items: list) -> str:
    """Экспорт каталога лотов в CSV."""
    rows = ["название;цена;статус;просмотры;продажи"]
    for it in items:
        rows.append(
            f"\"{it.get('name') or 'Без названия'}\";{it.get('price') or 0};"
            f"{it.get('status') or ''};{it.get('views_counter') or 0};{it.get('deals_counter') or 0}"
        )
    return "\n".join(rows)
