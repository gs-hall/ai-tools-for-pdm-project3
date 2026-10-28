"""Проводит очередь через агента и сверяет маршруты с эталоном.

    python scripts/run_queue.py            разобрать все дела в состоянии NEW и записать отчёты
    python scripts/run_queue.py RC-6001    разобрать одно дело
    python scripts/run_queue.py --report   только пересобрать отчёты по текущей базе
    python scripts/run_queue.py --models   показать модели Claude, доступные через агрегатор

Отчёты: docs/results.md (маршруты, решения, состояния) и docs/audit-log.md (журнал по делам).
Эталон читает только этот скрипт — не агент и не сервер.
"""

import json
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dispatcher import config, db, llm, workflow  # noqa: E402

EXPECTED = json.loads((ROOT / "tests" / "fixtures" / "expected-routes.json").read_text(encoding="utf-8"))
REPORT = ROOT / "docs" / "results.md"
AUDIT_REPORT = ROOT / "docs" / "audit-log.md"
DECISION_NAMES = {"APPROVE": "одобрить", "DENY": "отказать", "DUPLICATE": "повторное", "REQUEST_INFO": "запросить сведения"}


def cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def case_log(conn, case_id: str) -> list:
    return conn.execute(
        "SELECT id, ts, actor, event, details FROM audit_log WHERE case_id = ? ORDER BY id", (case_id,),
    ).fetchall()


def states(log: list, current: str) -> str:
    """Состояние до первого перехода и текущее состояние дела."""
    changes = [json.loads(entry["details"]) for entry in log if entry["event"] == "STATE_CHANGE"]
    return f"{changes[0]['from'] if changes else current} → {current}"


def log_link(log: list, case_id: str) -> str:
    if not log:
        return "—"
    return f"[№{log[0]['id']}–{log[-1]['id']}]({AUDIT_REPORT.name}#{case_id.lower()})"


def write_audit_report(conn) -> None:
    sections = []
    for case_id in EXPECTED:
        rows = [
            f"| {entry['id']} | {entry['ts']} | {entry['actor']} | {entry['event']} | `{cell(entry['details'])}` |"
            for entry in case_log(conn, case_id)
        ]
        sections.append(
            f"## {case_id}\n\n| № | Время (UTC) | Кто | Событие | Подробности |\n|---|---|---|---|---|\n" + "\n".join(rows)
        )
    AUDIT_REPORT.write_text(
        "# Журнал по делам\n\nВыгрузка таблицы `audit_log` после прогона и решений менеджера. "
        "Ключи доступа в журнал не попадают: перед записью строки проходят очистку.\n\n" + "\n\n".join(sections) + "\n",
        encoding="utf-8",
    )


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
        log = case_log(conn, case_id)
        rules = ", ".join(rule["rule_id"] for rule in result.get("rules_applied", []))
        rows.append(
            f"| {case_id} | {expected} | {route} | {'✅' if route == expected else '❌'} | {rules} "
            f"| {cell(result.get('stop_reason', ''))} | {DECISION_NAMES.get(decision[0], '—') if decision else '—'} "
            f"| {authorization[0] if authorization else '—'} | {states(log, workflow.get_case(conn, case_id)['state'])} "
            f"| {log_link(log, case_id)} |"
        )
        texts.append(f"| {case_id} | {cell(result.get('customer_draft', '—'))} | {cell(result.get('manager_note', '—'))} |")
    model = conn.execute("SELECT model FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(
        "# Результаты агента по 16 обращениям\n\n"
        f"Модель: {model[0] if model else '—'}. Совпало с эталоном: **{matched} из {len(EXPECTED)}**.\n\n"
        "Состояние — до первого перехода и после решения менеджера; журнал — номера записей в "
        f"[{AUDIT_REPORT.name}]({AUDIT_REPORT.name}).\n\n"
        "| Дело | Эталон | Маршрут агента | Совпадение | Правила | Почему агент остановился | Решение менеджера "
        "| Разрешение | Состояние | Журнал |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "## Материалы агента по каждому делу\n\n"
        "| Дело | Черновик клиенту | Заметка менеджеру |\n"
        "|---|---|---|\n" + "\n".join(texts) + "\n",
        encoding="utf-8",
    )
    write_audit_report(conn)
    return matched


def main(args: list[str]) -> None:
    if args == ["--models"]:
        print("\n".join(model.id for model in llm.make_client().models.list(limit=100).data if "claude" in model.id))
        return
    with closing(db.connect()) as conn:
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
    print(f"Совпало {matched} из {len(EXPECTED)} · модель {config.AI_MODEL} · "
          f"{REPORT.relative_to(ROOT)}, {AUDIT_REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
