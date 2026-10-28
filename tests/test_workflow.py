"""Разбор, решение менеджера, human gate, права ролей и журнал — через API."""

import json
import sqlite3

from tests.conftest import CUSTOMER, MANAGER, reads, result, submit, tool_use

DENY = result(
    "RECOMMEND_DENY",
    rules_applied=[{"rule_id": "C-2", "how": "устройство подключали к телефону"}],
    customer_draft="К сожалению, мы не можем принять возврат: наушники подключали к телефону (правило C-2).",
)


def db(client) -> sqlite3.Connection:
    conn = sqlite3.connect(client.application.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def analyze(make_app, case_id: str, order_number: str, payload: dict):
    client, model = make_app([*reads(order_number), submit(payload)])
    response = client.post(f"/api/cases/{case_id}/analyze", headers=MANAGER)
    return client, model, response


def test_analyze_stores_recommendation_and_moves_to_manager(make_app):
    client, model, response = analyze(make_app, "RC-6001", "SC-84721", result("RECOMMEND_APPROVE"))
    assert response.status_code == 200
    case = client.get("/api/cases/RC-6001", headers=MANAGER).json
    assert case["case"]["state"] == "AWAITING_MANAGER"
    assert case["recommendation"]["route"] == "RECOMMEND_APPROVE"
    assert case["run"]["tools_used"] == ["get_order", "get_return_history", "get_policy"]
    events = [entry["event"] for entry in case["audit"]]
    assert events == ["TOOL_CALL", "TOOL_CALL", "TOOL_CALL", "AGENT_RESULT", "STATE_CHANGE"]
    assert case["authorization"] is None
    assert "submit_recommendation" in {tool["name"] for tool in model.requests[0]["tools"]}


def test_model_has_no_tools_that_change_state(make_app):
    _, model, _ = analyze(make_app, "RC-6001", "SC-84721", result("RECOMMEND_APPROVE"))
    names = {tool["name"] for tool in model.requests[0]["tools"]}
    assert names == {"get_order", "get_return_history", "get_policy", "submit_recommendation"}


def test_customer_does_not_see_recommendation_before_decision(make_app):
    client, _, _ = analyze(make_app, "RC-6002", "SC-84785", DENY)
    view = client.get("/api/cases/RC-6002", headers=CUSTOMER).json
    text = json.dumps(view, ensure_ascii=False)
    assert "RECOMMEND_DENY" not in text
    assert DENY["customer_draft"] not in text
    assert DENY["manager_note"] not in text
    assert view["status"] == "Обращение на рассмотрении"

    client.post("/api/cases/RC-6002/decision", headers=MANAGER, json={"decision": "DENY"})
    view = client.get("/api/cases/RC-6002", headers=CUSTOMER).json
    assert view["messages"][-1]["body"] == DENY["customer_draft"]
    assert "RECOMMEND_DENY" not in json.dumps(view)


def test_confirmed_approve_creates_one_authorization(make_app):
    client, _, _ = analyze(make_app, "RC-6001", "SC-84721", result("RECOMMEND_APPROVE"))
    outcome = client.post("/api/cases/RC-6001/decision", headers=MANAGER, json={"decision": "APPROVE"}).json
    assert outcome == {"decision": "APPROVE", "state": "CLOSED_APPROVED", "authorization_id": "RA-6001"}
    conn = db(client)
    assert conn.execute("SELECT count(*) FROM return_authorizations").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM history WHERE record_id = 'RH-RC-6001'").fetchone()[0] == "RETURN_AUTHORIZED"
    view = client.get("/api/cases/RC-6001", headers=CUSTOMER).json
    assert "RA-6001" in view["messages"][-1]["body"]


def test_recommendation_alone_creates_no_authorization(make_app):
    client, _, _ = analyze(make_app, "RC-6001", "SC-84721", result("RECOMMEND_APPROVE"))
    assert db(client).execute("SELECT count(*) FROM return_authorizations").fetchone()[0] == 0


def test_other_routes_create_no_authorization(make_app):
    client, _, _ = analyze(make_app, "RC-6002", "SC-84785", DENY)
    client.post("/api/cases/RC-6002/decision", headers=MANAGER, json={"decision": "DENY"})
    assert db(client).execute("SELECT count(*) FROM return_authorizations").fetchone()[0] == 0
    assert client.get("/api/cases/RC-6002", headers=MANAGER).json["case"]["state"] == "CLOSED_DENIED"


def test_second_authorization_for_order_is_refused(make_app):
    duplicate = result(
        "DUPLICATE_OR_PROCESSED",
        history_refs=["RH-5887"],
        rules_applied=[{"rule_id": "P-4", "how": "по заказу уже выдано разрешение RA-5887"}],
        customer_draft="Ваше обращение уже рассмотрено: возврат подтверждён ранее, номер разрешения RA-5887.",
    )
    client, _, _ = analyze(make_app, "RC-6005", "SC-84502", duplicate)
    response = client.post("/api/cases/RC-6005/decision", headers=MANAGER, json={
        "decision": "APPROVE", "comment": "клиент просит снова", "customer_message": "Одобряем.",
    })
    assert response.status_code == 409
    assert db(client).execute("SELECT count(*) FROM return_authorizations").fetchone()[0] == 0
    events = [entry["event"] for entry in client.get("/api/cases/RC-6005", headers=MANAGER).json["audit"]]
    assert events[-1] == "DECISION_REFUSED"


def test_override_requires_comment_and_message(make_app):
    client, _, _ = analyze(make_app, "RC-6002", "SC-84785", DENY)
    response = client.post("/api/cases/RC-6002/decision", headers=MANAGER, json={"decision": "APPROVE"})
    assert response.status_code == 400
    response = client.post("/api/cases/RC-6002/decision", headers=MANAGER, json={
        "decision": "APPROVE", "comment": "Исключение по решению руководителя", "customer_message": "Возврат подтверждён.",
    })
    assert response.json["authorization_id"] == "RA-6002"


def test_manual_review_has_no_default_decision(make_app):
    manual = result("MANUAL_REVIEW", escalation_reasons=["стоимость от 30 000 ₽"], customer_draft="Обращение передано менеджеру.")
    client, _, _ = analyze(make_app, "RC-6004", "SC-84846", manual)
    response = client.post("/api/cases/RC-6004/decision", headers=MANAGER, json={"decision": "DENY"})
    assert response.status_code == 400


def test_decision_before_analysis_is_refused(make_app):
    client, _ = make_app()
    response = client.post("/api/cases/RC-6001/decision", headers=MANAGER, json={"decision": "APPROVE"})
    assert response.status_code == 409


def test_customer_cannot_analyze_or_decide(make_app):
    client, _ = make_app()
    assert client.post("/api/cases/RC-6001/analyze", headers=CUSTOMER).status_code == 403
    assert client.post("/api/cases/RC-6001/decision", headers=CUSTOMER, json={"decision": "APPROVE"}).status_code == 403
    assert client.get("/api/cases", headers=CUSTOMER).status_code == 403
    assert client.get("/api/cases").status_code == 401


def test_request_info_dialog_round_trip(make_app):
    need_info = result(
        "NEED_MORE_INFORMATION",
        rules_applied=[{"rule_id": "P-2", "how": "нет причины и состояния"}],
        missing_info=["причина", "состояние"],
        questions_to_customer=["Почему возвращаете?", "В каком состоянии товар?"],
        customer_draft="Подскажите, пожалуйста, почему вы возвращаете органайзер и в каком он состоянии?",
    )
    client, _, _ = analyze(make_app, "RC-6003", "SC-84804", need_info)
    assert client.post("/api/cases/RC-6003/reply", headers=CUSTOMER, json={"text": "Не подошёл"}).status_code == 409
    client.post("/api/cases/RC-6003/decision", headers=MANAGER, json={"decision": "REQUEST_INFO"})
    view = client.get("/api/cases/RC-6003", headers=CUSTOMER).json
    assert view["can_reply"] is True
    response = client.post("/api/cases/RC-6003/reply", headers=CUSTOMER, json={"text": "Не подошёл цвет, не пользовался."})
    assert response.json["status"] == "Обращение получено"
    assert db(client).execute("SELECT count(*) FROM return_authorizations").fetchone()[0] == 0


def test_rejected_result_falls_back_to_manual_review(make_app):
    bad = result("RECOMMEND_APPROVE", rules_applied=[{"rule_id": "X-1", "how": "-"}])
    client, _ = make_app([*reads("SC-84721"), submit(bad), submit(bad)])
    response = client.post("/api/cases/RC-6001/analyze", headers=MANAGER)
    assert response.json["recommendation"]["route"] == "MANUAL_REVIEW"
    case = client.get("/api/cases/RC-6001", headers=MANAGER).json
    assert case["run"]["fallback"] is True
    assert "Нет таких правил: X-1" in case["run"]["guard_errors"]


def test_model_gets_one_chance_to_fix_result(make_app):
    bad = result("RECOMMEND_APPROVE", rules_applied=[{"rule_id": "X-1", "how": "-"}])
    client, model = make_app([*reads("SC-84721"), submit(bad), submit(result("RECOMMEND_APPROVE"))])
    response = client.post("/api/cases/RC-6001/analyze", headers=MANAGER)
    assert response.json["recommendation"]["route"] == "RECOMMEND_APPROVE"
    feedback = model.requests[-1]["messages"][-1]["content"][0]
    assert feedback["is_error"] is True and "X-1" in feedback["content"]


def test_reading_other_order_is_denied_and_logged(make_app):
    turns = [[tool_use("get_order", {"order_number": "SC-84846"})], *reads("SC-84721"), submit(result("RECOMMEND_APPROVE"))]
    client, _ = make_app(turns)
    client.post("/api/cases/RC-6001/analyze", headers=MANAGER)
    events = [entry["event"] for entry in client.get("/api/cases/RC-6001", headers=MANAGER).json["audit"]]
    assert events[0] == "TOOL_DENIED"


def test_reset_restores_starter_state(make_app):
    client, _, _ = analyze(make_app, "RC-6001", "SC-84721", result("RECOMMEND_APPROVE"))
    client.post("/api/cases/RC-6001/decision", headers=MANAGER, json={"decision": "APPROVE"})
    imported = client.post("/api/admin/reset", headers=MANAGER).json["imported"]
    assert imported == {"orders": 16, "history": 4, "cases": 16, "rules": 12}
    states = {case["state"] for case in client.get("/api/cases", headers=MANAGER).json["cases"]}
    assert states == {"NEW"}
