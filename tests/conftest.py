"""Общие фикстуры. Вместо внешнего API — заготовленный сценарий ответов модели.

Так тесты проверяют серверную часть: проверку результата, переходы, права и журнал.
"""

import copy
import itertools
from types import SimpleNamespace

import pytest

from app import create_app

MANAGER = {"X-Role": "manager"}
CUSTOMER = {"X-Role": "customer"}


class Block(SimpleNamespace):
    def model_dump(self, exclude_none: bool = False) -> dict:
        return {key: value for key, value in vars(self).items() if not (exclude_none and value is None)}


def tool_use(name: str, args: dict) -> Block:
    return Block(type="tool_use", id=f"call-{next(Block.ids)}", name=name, input=args)


Block.ids = itertools.count(1)


class ScriptedClient:
    """Отдаёт заранее заданные ответы модели по очереди и запоминает запросы."""

    def __init__(self, turns: list[list[Block]]):
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return SimpleNamespace(content=self.turns.pop(0))


def reads(order_number: str) -> list[list[Block]]:
    return [
        [tool_use("get_order", {"order_number": order_number})],
        [tool_use("get_return_history", {"order_number": order_number}), tool_use("get_policy", {})],
    ]


def result(route: str, **overrides) -> dict:
    base = {
        "route": route,
        "summary": "Клиент просит вернуть товар.",
        "facts": [{"name": "Срок", "value": "11 дней из 14", "source": "order"}],
        "rules_applied": [{"rule_id": "P-1", "how": "срок не истёк"}],
        "missing_info": [],
        "questions_to_customer": [],
        "escalation_reasons": [],
        "history_refs": [],
        "damage_claimed": False,
        "external_action_requested": False,
        "stop_reason": "Все условия выполнены.",
        "customer_draft": "Здравствуйте! Возврат подтверждён, номер разрешения будет указан ниже.",
        "manager_note": "Факты подтверждены.",
    }
    return base | overrides


def submit(payload: dict) -> list[Block]:
    return [tool_use("submit_recommendation", payload)]


@pytest.fixture
def make_app(tmp_path):
    """Создаёт приложение со своей базой и сценарием модели."""
    holder: dict = {}

    def factory(turns: list[list[Block]] | None = None):
        holder["client"] = ScriptedClient(turns or [])
        app = holder.get("app") or create_app(tmp_path / "test.sqlite", client_factory=lambda: holder["client"])
        holder["app"] = app
        return app.test_client(), holder["client"]

    return factory
