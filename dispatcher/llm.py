"""Подключение к языковой модели и цикл вызова функций.

Модель вызывается через агрегатор (AI_PROXY_URL, как в devops-web-api): запросы к Claude идут
на ``<AI_PROXY_URL>/anthropic/v1/messages``.
"""

import json
from collections.abc import Callable

from dispatcher import config, tools
from dispatcher.guard import RESULT_SCHEMA
from dispatcher.prompts import SYSTEM_PROMPT

SUBMIT_TOOL = {
    "name": "submit_recommendation",
    "description": "Завершить разбор и передать рекомендацию на серверную проверку.",
    "input_schema": RESULT_SCHEMA,
}
MAX_TURNS = 10
MAX_SUBMITS = 2


class LlmNotConfigured(Exception):
    """Не заданы AI_PROXY_URL или AI_PROXY_TOKEN."""


class AgentGaveUp(Exception):
    """Модель не вернула результат за отведённое число шагов."""


def make_client():
    if not config.AI_PROXY_URL or not config.AI_PROXY_TOKEN:
        raise LlmNotConfigured("Заполните AI_PROXY_URL и AI_PROXY_TOKEN в .env")
    import anthropic

    return anthropic.Anthropic(base_url=f"{config.AI_PROXY_URL}/anthropic", api_key=config.AI_PROXY_TOKEN)


def run(
    client,
    user_prompt: str,
    execute_tool: Callable[[str, dict], tuple[object, bool]],
    check_result: Callable[[dict, list[str]], list[str]],
) -> dict:
    """Ведёт диалог с моделью до принятого результата или исчерпания попыток.

    execute_tool(name, args) -> (данные, is_error); check_result(result, tools_used) -> список ошибок.
    Возвращает: tools_used, submissions (каждая с ошибками проверки), accepted (результат или None).
    """
    messages = [{"role": "user", "content": user_prompt}]
    tools_used: list[str] = []
    submissions: list[dict] = []

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            tools=[*tools.READ_TOOLS, SUBMIT_TOOL],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": [block.model_dump(exclude_none=True) for block in response.content]})
        calls = [block for block in response.content if block.type == "tool_use"]
        if not calls:
            messages.append({"role": "user", "content": "Заверши разбор вызовом submit_recommendation."})
            continue

        results = []
        for block in calls:
            if block.name == SUBMIT_TOOL["name"]:
                errors = check_result(block.input, tools_used)
                submissions.append({"result": block.input, "errors": errors})
                if not errors:
                    return {"tools_used": tools_used, "submissions": submissions, "accepted": block.input}
                if len(submissions) >= MAX_SUBMITS:
                    return {"tools_used": tools_used, "submissions": submissions, "accepted": None}
                content, is_error = "Результат не принят сервером:\n- " + "\n- ".join(errors), True
            else:
                tools_used.append(block.name)
                data, is_error = execute_tool(block.name, block.input)
                content = json.dumps(data, ensure_ascii=False)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    raise AgentGaveUp(f"Модель не завершила разбор за {MAX_TURNS} шагов")
