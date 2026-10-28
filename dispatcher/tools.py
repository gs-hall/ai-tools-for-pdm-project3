"""Функции чтения, доступные модели, и вычисляемые сервером факты.

Все функции только читают базу и работают в пределах заказа своего дела.
"""

import sqlite3
from datetime import date, datetime

from dispatcher.config import HIGH_VALUE_RUB, RETURN_WINDOW_DAYS
from dispatcher.policy import CATEGORY_RULES

ORDER_ARG = {
    "type": "object",
    "properties": {"order_number": {"type": "string", "description": "Номер заказа из дела"}},
    "required": ["order_number"],
}

READ_TOOLS = [
    {
        "name": "get_order",
        "description": "Заказ дела: покупатель, дата доставки, позиции, категории, цены и вычисленные сервером факты (срок, дорогая позиция, правила категории).",
        "input_schema": ORDER_ARG,
    },
    {
        "name": "get_return_history",
        "description": "Ранее зафиксированные обращения по заказу дела: статус, причина, итог, номер разрешения.",
        "input_schema": ORDER_ARG,
    },
    {
        "name": "get_policy",
        "description": "Политика возвратов P-1…P-6 и правила категорий C-1…C-6 с идентификаторами.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class ToolDenied(Exception):
    """Модель попросила данные вне своего дела."""


def days_since_delivery(delivered_at: str, opened_at: str) -> int:
    """Дней от доставки до обращения; день доставки не считается первым днём срока (P-1)."""
    delivered = date.fromisoformat(delivered_at)
    opened = datetime.fromisoformat(opened_at).date()
    return (opened - delivered).days


def order_facts(conn: sqlite3.Connection, case: sqlite3.Row) -> dict:
    order = conn.execute("SELECT * FROM orders WHERE order_number = ?", (case["order_number"],)).fetchone()
    items = [dict(row) for row in conn.execute(
        "SELECT line_id, sku, name, category, quantity, price_rub FROM order_items WHERE order_number = ? ORDER BY line_id",
        (case["order_number"],),
    )]
    days = days_since_delivery(order["delivered_at"], case["opened_at"])
    return {
        "order": dict(order),
        "items": items,
        "computed": {
            "case_opened_at": case["opened_at"],
            "days_since_delivery": days,
            "return_window_days": RETURN_WINDOW_DAYS,
            "within_return_window": days <= RETURN_WINDOW_DAYS,
            "high_value": any(item["price_rub"] >= HIGH_VALUE_RUB for item in items),
            "high_value_threshold_rub": HIGH_VALUE_RUB,
            "category_rules": sorted({CATEGORY_RULES[item["category"]] for item in items}),
        },
    }


def history_for(conn: sqlite3.Connection, order_number: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT * FROM history WHERE order_number = ? ORDER BY opened_at", (order_number,),
    )]


def all_rules(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT rule_id, title, text FROM policy_rules ORDER BY rule_id")]


def call(conn: sqlite3.Connection, case: sqlite3.Row, name: str, args: dict) -> dict | list:
    """Выполняет функцию чтения по запросу модели."""
    if name != "get_policy" and args.get("order_number") != case["order_number"]:
        raise ToolDenied(f"Заказ {args.get('order_number')!r} не относится к делу {case['case_id']}")
    if name == "get_order":
        return order_facts(conn, case)
    if name == "get_return_history":
        return history_for(conn, case["order_number"])
    if name == "get_policy":
        return all_rules(conn)
    raise ToolDenied(f"Функции {name!r} нет")
