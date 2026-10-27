"""Проводит очередь через агента и сверяет маршруты с эталоном.

    python scripts/run_queue.py            разобрать все дела в состоянии NEW и записать docs/results.md
    python scripts/run_queue.py RC-6001    разобрать одно дело
    python scripts/run_queue.py --report   только пересобрать docs/results.md
    python scripts/run_queue.py --models   показать модели Claude, доступные через агрегатор

Эталон читает только этот скрипт — не агент и не сервер.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dispatcher import config, db, llm, workflow  # noqa: E402

EXPECTED = json.loads((ROOT / "tests" / "fixtures" / "expected-routes.json").read_text(encoding="utf-8"))
REPORT = ROOT / "docs" / "results.md"
DECISION_NAMES = {"APPROVE": "одобрить", "DENY": "отказать", "DUPLICATE": "повторное", "REQUEST_INFO": "запросить сведения"}


def cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def report(conn) -> int:
    rows, texts, matched = [], [], 0
    for case_id, expected in EXPECTED.items():
        run = workflow.latest_run(conn, case_id)
        result = json.loads(run["result"]) if run else {}
        route = result.get("route", "—")
        matched += route == expected
        decision = conn.execute(
            "SELECT decision FROM decisions WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case_id,),
        ).fetchone()
        authorization = conn.execute(
            "SELECT authorization_id FROM return_authorizations WHERE case_id = ?", (case_id,),
        ).fetchone()
        rules = ", ".join(rule["rule_id"] for rule in result.get("rules_applied", []))
        rows.append(
            f"| {case_id} | {expected} | {route} | {'✅' if route == expected else '❌'} | {rules} "
            f"| {cell(result.get('stop_reason', ''))} | {DECISION_NAMES.get(decision[0], '—') if decision else '—'} "
            f"| {authorization[0] if authorization else '—'} |"
        )
        texts.append(f"| {case_id} | {cell(result.get('customer_draft', '—'))} | {cell(result.get('manager_note', '—'))} |")
    model = conn.execute("SELECT model FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(
        "# Результаты агента по 16 обращениям\n\n"
        f"Модель: {model[0] if model else '—'}. Совпало с эталоном: **{matched} из {len(EXPECTED)}**.\n\n"
        "| Дело | Эталон | Маршрут агента | Совпадение | Правила | Почему агент остановился | Решение менеджера | Разрешение |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "## Материалы агента по каждому делу\n\n"
        "| Дело | Черновик клиенту | Заметка менеджеру |\n"
        "|---|---|---|\n" + "\n".join(texts) + "\n",
        encoding="utf-8",
    )
    return matched


def main(args: list[str]) -> None:
    if args == ["--models"]:
        print("\n".join(model.id for model in llm.make_client().models.list(limit=100).data if "claude" in model.id))
        return
    conn = db.connect()
    db.ensure_ready(conn)
    if args != ["--report"]:
        client = llm.make_client()
        targets = args or [case_id for case_id in EXPECTED
                           if workflow.get_case(conn, case_id)["state"] == "NEW"]
        for case_id in targets:
            result = workflow.analyze(conn, case_id, client)
            mark = "✅" if result["route"] == EXPECTED[case_id] else "❌"
            print(f"{case_id}: {result['route']} {mark}")
    matched = report(conn)
    print(f"Совпало {matched} из {len(EXPECTED)} · модель {config.AI_MODEL} · {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
