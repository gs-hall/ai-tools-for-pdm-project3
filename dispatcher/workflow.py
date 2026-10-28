"""Состояния дела, разбор агентом, решение менеджера и ответ клиента.

Только этот модуль меняет состояние дел и создаёт разрешения.
"""

import json
import sqlite3

from dispatcher import audit, config, guard, llm, tools
from dispatcher.audit import now
from dispatcher.prompts import FALLBACK_CUSTOMER_DRAFT, case_prompt

TRANSITIONS = {
    ("NEW", "AWAITING_MANAGER"),
    ("AWAITING_MANAGER", "AWAITING_MANAGER"),
    ("AWAITING_MANAGER", "CLOSED_APPROVED"),
    ("AWAITING_MANAGER", "CLOSED_DENIED"),
    ("AWAITING_MANAGER", "CLOSED_DUPLICATE"),
    ("AWAITING_MANAGER", "WAITING_CUSTOMER"),
    ("WAITING_CUSTOMER", "NEW"),
}
DECISION_STATE = {
    "APPROVE": "CLOSED_APPROVED",
    "DENY": "CLOSED_DENIED",
    "DUPLICATE": "CLOSED_DUPLICATE",
    "REQUEST_INFO": "WAITING_CUSTOMER",
}
ROUTE_DECISION = {
    "RECOMMEND_APPROVE": "APPROVE",
    "RECOMMEND_DENY": "DENY",
    "DUPLICATE_OR_PROCESSED": "DUPLICATE",
    "NEED_MORE_INFORMATION": "REQUEST_INFO",
}


class NotFound(Exception):
    pass


class Conflict(Exception):
    """Действие недопустимо в текущем состоянии или нарушает правила."""


class BadRequest(Exception):
    pass


def get_case(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    case = conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if case is None:
        raise NotFound(f"Дело {case_id} не найдено")
    return case


def dialog(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute(
        "SELECT author, body, created_at FROM messages WHERE case_id = ? ORDER BY id", (case_id,),
    )]


def latest_run(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM agent_runs WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,),
    ).fetchone()


def move(conn: sqlite3.Connection, case: sqlite3.Row, target: str, actor: str) -> None:
    source = case["state"]
    if (source, target) not in TRANSITIONS:
        raise Conflict(f"Переход {source} → {target} не разрешён")
    conn.execute("UPDATE cases SET state = ?, updated_at = ? WHERE case_id = ?", (target, now(), case["case_id"]))
    audit.write(conn, case["case_id"], actor, "STATE_CHANGE", {"from": source, "to": target})


def guard_context(conn: sqlite3.Connection, case: sqlite3.Row) -> dict:
    return {
        "rule_ids": {row["rule_id"] for row in conn.execute("SELECT rule_id FROM policy_rules")},
        "computed": tools.order_facts(conn, case)["computed"],
        "history": tools.history_for(conn, case["order_number"]),
    }


def fallback_result(conn: sqlite3.Connection, case: sqlite3.Row, reason: str) -> dict:
    computed = tools.order_facts(conn, case)["computed"]
    return {
        "route": "MANUAL_REVIEW",
        "summary": "Результат модели не прошёл серверную проверку — нужен разбор менеджера.",
        "facts": [
            {"name": "Дней с доставки", "value": str(computed["days_since_delivery"]), "source": "order"},
            {"name": "Дорогая позиция", "value": "да" if computed["high_value"] else "нет", "source": "order"},
        ],
        "rules_applied": [{"rule_id": "P-6", "how": "неопределённость передаётся менеджеру"}],
        "missing_info": [],
        "questions_to_customer": [],
        "escalation_reasons": [reason],
        "history_refs": [],
        "damage_claimed": False,
        "external_action_requested": False,
        "stop_reason": "Сервер не принял результат модели.",
        "customer_draft": FALLBACK_CUSTOMER_DRAFT,
        "manager_note": reason,
    }


def analyze(conn: sqlite3.Connection, case_id: str, client) -> dict:
    """Разбор дела агентом. Модель только читает и предлагает; запись делает сервер."""
    case = get_case(conn, case_id)
    if (case["state"], "AWAITING_MANAGER") not in TRANSITIONS:
        raise Conflict(f"Разбор недоступен в состоянии {case['state']}")
    ctx = guard_context(conn, case)

    def execute_tool(name: str, args: dict) -> tuple[object, bool]:
        try:
            data = tools.call(conn, case, name, args)
        except tools.ToolDenied as denied:
            audit.write(conn, case_id, "agent", "TOOL_DENIED", {"tool": name, "args": args, "reason": str(denied)})
            return {"error": str(denied)}, True
        audit.write(conn, case_id, "agent", "TOOL_CALL", {"tool": name, "args": args})
        return data, False

    def check_result(result: dict, tools_used: list[str]) -> list[str]:
        return guard.validate(result, {**ctx, "tools_used": set(tools_used)})

    try:
        outcome = llm.run(client, case_prompt(dict(case), dialog(conn, case_id)), execute_tool, check_result)
    except llm.AgentGaveUp as gave_up:
        outcome = {"tools_used": [], "submissions": [], "accepted": None, "gave_up": str(gave_up)}

    errors = [error for submission in outcome["submissions"] for error in submission["errors"]]
    result = outcome["accepted"]
    if result is None:
        reason = outcome.get("gave_up") or "Ошибки проверки: " + "; ".join(errors)
        result = fallback_result(conn, case, reason)

    cursor = conn.execute(
        "INSERT INTO agent_runs (case_id, created_at, model, tools_used, raw_output, guard_errors, result, fallback) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (case_id, now(), config.AI_MODEL, json.dumps(outcome["tools_used"]),
         audit.redact(json.dumps(outcome["submissions"], ensure_ascii=False)),
         json.dumps(errors, ensure_ascii=False), json.dumps(result, ensure_ascii=False),
         int(outcome["accepted"] is None)),
    )
    audit.write(conn, case_id, "agent", "AGENT_RESULT", {
        "run_id": cursor.lastrowid,
        "route": result["route"],
        "accepted": outcome["accepted"] is not None,
        "guard_errors": errors,
        "data_used": sorted(set(outcome["tools_used"])),
    })
    move(conn, case, "AWAITING_MANAGER", "server")
    conn.commit()
    return result


def decide(conn: sqlite3.Connection, case_id: str, decision: str, comment: str = "", customer_message: str = "") -> dict:
    """Решение менеджера — единственный путь к итоговому статусу и разрешению."""
    case = get_case(conn, case_id)
    if decision not in DECISION_STATE:
        raise BadRequest(f"Неизвестное решение {decision!r}")
    if case["state"] != "AWAITING_MANAGER":
        raise Conflict(f"Решение недоступно в состоянии {case['state']}")
    run = latest_run(conn, case_id)
    result = json.loads(run["result"])
    route = result["route"]
    confirms = ROUTE_DECISION.get(route) == decision
    comment, customer_message = comment.strip(), customer_message.strip()
    if not confirms and not (comment and customer_message):
        raise BadRequest("Решение отличается от рекомендации: нужны комментарий и сообщение клиенту")
    history = tools.history_for(conn, case["order_number"])

    refusal = None
    if decision == "APPROVE":
        issued = conn.execute(
            "SELECT 1 FROM return_authorizations WHERE order_number = ?", (case["order_number"],),
        ).fetchone()
        if issued or any(record["status"] == guard.AUTHORIZED_STATUS for record in history):
            refusal = "По заказу уже выдано разрешение — второе не создаётся (P-4)"
    if decision == "DUPLICATE" and not history:
        refusal = "В истории заказа нет записей — дело нельзя закрыть как повторное"
    if refusal:
        audit.write(conn, case_id, "manager", "DECISION_REFUSED", {"decision": decision, "reason": refusal})
        conn.commit()
        raise Conflict(refusal)

    message = customer_message or result["customer_draft"]
    authorization_id = f"RA-{case_id.split('-')[-1]}" if decision == "APPROVE" else None
    if authorization_id:
        message = f"{message}\n\nНомер разрешения на возврат: {authorization_id}."

    cursor = conn.execute(
        "INSERT INTO decisions (case_id, run_id, recommended_route, decision, comment, customer_message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (case_id, run["id"], route, decision, comment, message, now()),
    )
    decision_id = cursor.lastrowid
    audit.write(conn, case_id, "manager", "MANAGER_DECISION", {
        "recommended_route": route, "decision": decision, "confirms_recommendation": confirms, "comment": comment,
    })
    if authorization_id:
        stamp = now()
        conn.execute(
            "INSERT INTO return_authorizations VALUES (?, ?, ?, ?, ?)",
            (authorization_id, case_id, case["order_number"], decision_id, stamp),
        )
        conn.execute(
            "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"RH-{case_id}", case["order_number"], case["opened_at"], stamp, guard.AUTHORIZED_STATUS,
             "MANAGER_APPROVED", result["summary"], authorization_id),
        )
        audit.write(conn, case_id, "server", "AUTHORIZATION_CREATED", {"authorization_id": authorization_id})
    conn.execute(
        "INSERT INTO messages (case_id, author, body, created_at) VALUES (?, 'manager', ?, ?)",
        (case_id, message, now()),
    )
    move(conn, case, DECISION_STATE[decision], "manager")
    conn.commit()
    return {"decision": decision, "state": DECISION_STATE[decision], "authorization_id": authorization_id}


def customer_reply(conn: sqlite3.Connection, case_id: str, body: str) -> None:
    case = get_case(conn, case_id)
    body = body.strip()
    if not body:
        raise BadRequest("Пустой ответ")
    if case["state"] != "WAITING_CUSTOMER":
        raise Conflict("Ответить можно, когда служба задала вопросы")
    conn.execute(
        "INSERT INTO messages (case_id, author, body, created_at) VALUES (?, 'customer', ?, ?)",
        (case_id, body, now()),
    )
    audit.write(conn, case_id, "customer", "CUSTOMER_REPLY", {"length": len(body)})
    move(conn, case, "NEW", "customer")
    conn.commit()
