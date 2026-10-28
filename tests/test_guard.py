"""Серверная проверка результата модели."""

from dispatcher import guard
from tests.conftest import result

RULES = {f"P-{n}" for n in range(1, 7)} | {f"C-{n}" for n in range(1, 7)}
ALL_READ = {"get_order", "get_return_history", "get_policy"}
FRESH = {"within_return_window": True, "high_value": False}
AUTHORIZED = [{"record_id": "RH-5887", "status": "RETURN_AUTHORIZED", "authorization_reference": "RA-5887"}]


def ctx(computed=None, history=None, tools_used=ALL_READ) -> dict:
    return {"rule_ids": RULES, "computed": computed or FRESH, "history": history or [], "tools_used": tools_used}


def test_valid_approve_is_accepted():
    assert guard.validate(result("RECOMMEND_APPROVE"), ctx()) == []


def test_unknown_route_is_rejected():
    assert guard.validate(result("APPROVE_NOW"), ctx()) == ["Недопустимый маршрут: 'APPROVE_NOW'"]


def test_unknown_rule_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE", rules_applied=[{"rule_id": "P-99", "how": "-"}]), ctx())
    assert "Нет таких правил: P-99" in errors


def test_result_without_reading_data_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE"), ctx(tools_used={"get_policy"}))
    assert "Не прочитаны данные: get_order, get_return_history" in errors


def test_approve_after_deadline_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE"), ctx({"within_return_window": False, "high_value": False}))
    assert "Одобрение после истечения срока (P-1)" in errors


def test_approve_high_value_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE"), ctx({"within_return_window": True, "high_value": True}))
    assert "Одобрение дорогого товара без менеджера (C-4)" in errors


def test_approve_with_damage_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE", damage_claimed=True), ctx())
    assert "Одобрение при заявлении о повреждении (C-4)" in errors


def test_approve_when_already_authorized_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE"), ctx(history=AUTHORIZED))
    assert "По заказу уже выдано разрешение (P-4)" in errors


def test_duplicate_needs_history_of_this_order():
    errors = guard.validate(result("DUPLICATE_OR_PROCESSED", history_refs=["RH-0000"]), ctx(history=AUTHORIZED))
    assert "Повторное обращение без ссылки на историю этого заказа" in errors


def test_duplicate_may_cite_previous_authorization():
    payload = result(
        "DUPLICATE_OR_PROCESSED",
        history_refs=["RH-5887"],
        rules_applied=[{"rule_id": "P-4", "how": "повтор"}],
        customer_draft="Ваше обращение уже рассмотрено, номер разрешения RA-5887.",
    )
    assert guard.validate(payload, ctx(history=AUTHORIZED)) == []


def test_draft_with_new_authorization_number_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE", customer_draft="Ваш номер RA-9999."), ctx())
    assert "Черновик содержит новый номер разрешения — его выдаёт только сервер" in errors


def test_need_info_draft_cannot_promise_result():
    payload = result(
        "NEED_MORE_INFORMATION",
        missing_info=["состояние"],
        questions_to_customer=["В каком состоянии товар?"],
        customer_draft="Возврат одобрен, только уточните состояние.",
    )
    assert "Черновик обещает результат до решения менеджера (P-3)" in guard.validate(payload, ctx())


def test_external_action_must_go_to_manager():
    errors = guard.validate(result("RECOMMEND_DENY", external_action_requested=True), ctx())
    assert "Просьба о внешнем действии должна идти менеджеру (P-6)" in errors


def test_draft_cannot_promise_courier():
    errors = guard.validate(result("RECOMMEND_APPROVE", customer_draft="Курьер приедет завтра."), ctx())
    assert "Черновик обещает внешнее действие (P-5)" in errors


def test_key_in_manager_note_is_rejected():
    errors = guard.validate(result("RECOMMEND_APPROVE", manager_note="ключ sk-abcdefghijklmnop"), ctx())
    assert "Поле manager_note похоже на ключ доступа" in errors


def test_manual_review_needs_reason():
    assert "Ручной разбор без причины передачи" in guard.validate(result("MANUAL_REVIEW"), ctx())


def test_deny_draft_with_infinitive_is_not_read_as_approval():
    payload = result(
        "RECOMMEND_DENY",
        rules_applied=[{"rule_id": "C-6", "how": "браслет подключали к телефону"}],
        customer_draft="Возврат принять не можем: браслет подключали к телефону (правило C-6).",
    )
    assert guard.validate(payload, ctx()) == []


def test_approval_inside_deny_draft_is_still_caught():
    payload = result(
        "RECOMMEND_DENY",
        rules_applied=[{"rule_id": "C-6", "how": "-"}],
        customer_draft="Возврат принят, но мы отказываем.",
    )
    assert "Черновик отказа содержит одобрение" in guard.validate(payload, ctx())
