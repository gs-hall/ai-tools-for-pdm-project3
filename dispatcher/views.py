"""Что видит менеджер и что видит клиент.

Клиентский ответ собирается из явного списка полей: рекомендации, фактов, заметки и черновика в нём нет.
"""

import json
import sqlite3

from dispatcher import tools
from dispatcher.workflow import dialog, get_case, latest_run

CUSTOMER_STATUS = {
    "NEW": "Обращение получено",
    "AWAITING_MANAGER": "Обращение на рассмотрении",
    "WAITING_CUSTOMER": "Ждём ваш ответ",
    "CLOSED_APPROVED": "Решение принято",
    "CLOSED_DENIED": "Решение принято",
    "CLOSED_DUPLICATE": "Решение принято",
}
CUSTOMER_FIELDS = ("case_id", "order_number", "subject", "opened_at")


def queue(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT c.case_id, c.subject, c.sender_name, c.state,
               (SELECT json_extract(r.result, '$.route') FROM agent_runs r
                 WHERE r.case_id = c.case_id ORDER BY r.id DESC LIMIT 1) AS route,
               (SELECT d.decision FROM decisions d
                 WHERE d.case_id = c.case_id ORDER BY d.id DESC LIMIT 1) AS decision
        FROM cases c ORDER BY c.case_id
    """)
    return [dict(row) for row in rows]


def manager_case(conn: sqlite3.Connection, case_id: str) -> dict:
    case = get_case(conn, case_id)
    run = latest_run(conn, case_id)
    decisions = [dict(row) for row in conn.execute(
        "SELECT decision, recommended_route, comment, created_at FROM decisions WHERE case_id = ? ORDER BY id", (case_id,),
    )]
    authorization = conn.execute(
        "SELECT authorization_id, created_at FROM return_authorizations WHERE case_id = ?", (case_id,),
    ).fetchone()
    log = [dict(row) | {"details": json.loads(row["details"])} for row in conn.execute(
        "SELECT ts, actor, event, details FROM audit_log WHERE case_id = ? ORDER BY id", (case_id,),
    )]
    return {
        "case": dict(case),
        "order": tools.order_facts(conn, case),
        "history": tools.history_for(conn, case["order_number"]),
        "dialog": dialog(conn, case_id),
        "recommendation": json.loads(run["result"]) if run else None,
        "run": {
            "id": run["id"], "model": run["model"], "created_at": run["created_at"],
            "tools_used": json.loads(run["tools_used"]), "guard_errors": json.loads(run["guard_errors"]),
            "fallback": bool(run["fallback"]),
        } if run else None,
        "decisions": decisions,
        "authorization": dict(authorization) if authorization else None,
        "audit": log,
    }


def customer_case(conn: sqlite3.Connection, case_id: str) -> dict:
    case = get_case(conn, case_id)
    return {
        **{field: case[field] for field in CUSTOMER_FIELDS},
        "status": CUSTOMER_STATUS[case["state"]],
        "can_reply": case["state"] == "WAITING_CUSTOMER",
        "messages": dialog(conn, case_id),
    }
