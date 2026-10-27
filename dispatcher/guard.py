"""Серверная проверка результата модели.

Модель только предлагает маршрут. Здесь сервер решает, можно ли принять предложение.
"""

import re

from dispatcher.audit import KEY_PATTERN

ROUTES = (
    "RECOMMEND_APPROVE",
    "RECOMMEND_DENY",
    "NEED_MORE_INFORMATION",
    "MANUAL_REVIEW",
    "DUPLICATE_OR_PROCESSED",
)
FACT_SOURCES = ("letter", "order", "history", "policy")
REQUIRED_READS = ("get_order", "get_return_history", "get_policy")
AUTHORIZED_STATUS = "RETURN_AUTHORIZED"

STRING_LIST = {"type": "array", "items": {"type": "string"}}

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": list(ROUTES)},
        "summary": {"type": "string", "description": "Одна фраза для менеджера"},
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "source": {"type": "string", "enum": list(FACT_SOURCES)},
                },
                "required": ["name", "value", "source"],
            },
        },
        "rules_applied": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"rule_id": {"type": "string"}, "how": {"type": "string"}},
                "required": ["rule_id", "how"],
            },
        },
        "missing_info": STRING_LIST,
        "questions_to_customer": STRING_LIST,
        "escalation_reasons": STRING_LIST,
        "history_refs": STRING_LIST,
        "damage_claimed": {"type": "boolean"},
        "external_action_requested": {"type": "boolean"},
        "stop_reason": {"type": "string"},
        "customer_draft": {"type": "string"},
        "manager_note": {"type": "string"},
    },
    "required": [
        "route", "summary", "facts", "rules_applied", "missing_info", "questions_to_customer",
        "escalation_reasons", "history_refs", "damage_claimed", "external_action_requested",
        "stop_reason", "customer_draft", "manager_note",
    ],
}

AUTHORIZATION_NUMBER = re.compile(r"\bRA-\d+", re.IGNORECASE)
EXTERNAL_PROMISE = re.compile(
    r"(верн[её]м (вам )?деньги|деньги (будут )?(возвращены|вернутся)|оформ\w* обмен|обменяем|курьер (приедет|заберёт|будет)|вызовем курьера|отправим курьера)",
    re.IGNORECASE,
)
APPROVE_PROMISE = re.compile(r"(возврат (одобрен|оформлен|подтвержд[её]н|принят)|одобряем|одобрили|разрешение выдано)", re.IGNORECASE)
DENY_PROMISE = re.compile(r"(отказ(ано|ываем)|не можем принять|возврат невозможен|в возврате отказ)", re.IGNORECASE)


def validate(result: dict, ctx: dict) -> list[str]:
    """Возвращает список ошибок; пустой список — результат принят.

    ctx: tools_used, rule_ids, computed (факты заказа), history (записи по заказу).
    """
    errors: list[str] = []
    missing_fields = [field for field in RESULT_SCHEMA["required"] if field not in result]
    if missing_fields:
        return [f"Нет полей: {', '.join(missing_fields)}"]

    unread = [name for name in REQUIRED_READS if name not in ctx["tools_used"]]
    if unread:
        errors.append(f"Не прочитаны данные: {', '.join(unread)}")

    route = result["route"]
    if route not in ROUTES:
        return errors + [f"Недопустимый маршрут: {route!r}"]

    errors += check_rules(result, ctx["rule_ids"])
    errors += check_facts(result)
    errors += check_route(result, ctx)
    known_refs = {record["authorization_reference"] for record in ctx["history"] if record["authorization_reference"]}
    errors += check_texts(result, known_refs)
    return errors


def check_rules(result: dict, rule_ids: set[str]) -> list[str]:
    cited = [rule["rule_id"] for rule in result["rules_applied"]]
    if not cited:
        return ["Нет ссылок на правила"]
    unknown = [rule_id for rule_id in cited if rule_id not in rule_ids]
    return [f"Нет таких правил: {', '.join(unknown)}"] if unknown else []


def check_facts(result: dict) -> list[str]:
    if not result["facts"]:
        return ["Нет найденных фактов"]
    bad = [fact["name"] for fact in result["facts"] if fact.get("source") not in FACT_SOURCES]
    return [f"Недопустимый источник у фактов: {', '.join(bad)}"] if bad else []


def check_route(result: dict, ctx: dict) -> list[str]:
    route = result["route"]
    computed = ctx["computed"]
    history = ctx["history"]
    errors: list[str] = []

    if route == "RECOMMEND_APPROVE":
        if not computed["within_return_window"]:
            errors.append("Одобрение после истечения срока (P-1)")
        if computed["high_value"]:
            errors.append("Одобрение дорогого товара без менеджера (C-4)")
        if result["damage_claimed"]:
            errors.append("Одобрение при заявлении о повреждении (C-4)")
        if result["escalation_reasons"]:
            errors.append("Одобрение при наличии причин для ручного разбора")
        if result["missing_info"]:
            errors.append("Одобрение при недостающих сведениях (P-2)")
        if any(record["status"] == AUTHORIZED_STATUS for record in history):
            errors.append("По заказу уже выдано разрешение (P-4)")
    elif route == "RECOMMEND_DENY":
        grounds = [rule["rule_id"] for rule in result["rules_applied"] if rule["rule_id"] not in ("P-3", "P-5")]
        if not grounds:
            errors.append("Отказ без правила-основания")
    elif route == "NEED_MORE_INFORMATION":
        if not result["missing_info"] or not result["questions_to_customer"]:
            errors.append("Запрос сведений без списка недостающих сведений и вопросов")
    elif route == "MANUAL_REVIEW":
        if not result["escalation_reasons"]:
            errors.append("Ручной разбор без причины передачи")
    elif route == "DUPLICATE_OR_PROCESSED":
        known = {record["record_id"] for record in history}
        if not result["history_refs"] or not set(result["history_refs"]) <= known:
            errors.append("Повторное обращение без ссылки на историю этого заказа")

    if result["external_action_requested"] and route not in ("MANUAL_REVIEW", "DUPLICATE_OR_PROCESSED"):
        errors.append("Просьба о внешнем действии должна идти менеджеру (P-6)")
    return errors


def check_texts(result: dict, known_refs: set[str]) -> list[str]:
    errors: list[str] = []
    draft = result["customer_draft"].strip()
    route = result["route"]
    if not draft:
        errors.append("Пустой черновик для клиента")
    new_refs = {ref.upper() for ref in AUTHORIZATION_NUMBER.findall(draft)} - known_refs
    if new_refs:
        errors.append("Черновик содержит новый номер разрешения — его выдаёт только сервер")
    if EXTERNAL_PROMISE.search(draft):
        errors.append("Черновик обещает внешнее действие (P-5)")
    if route in ("NEED_MORE_INFORMATION", "MANUAL_REVIEW"):
        if APPROVE_PROMISE.search(draft) or DENY_PROMISE.search(draft):
            errors.append("Черновик обещает результат до решения менеджера (P-3)")
    if route == "RECOMMEND_APPROVE" and DENY_PROMISE.search(draft):
        errors.append("Черновик одобрения содержит отказ")
    if route == "RECOMMEND_DENY" and APPROVE_PROMISE.search(draft):
        errors.append("Черновик отказа содержит одобрение")
    for field in ("customer_draft", "manager_note", "summary"):
        if KEY_PATTERN.search(result[field]):
            errors.append(f"Поле {field} похоже на ключ доступа")
    return errors
