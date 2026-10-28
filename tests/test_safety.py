"""Журнал без секретов, эталон не читается кодом агента, правила разобраны полностью."""

import json
from contextlib import closing
from pathlib import Path

from dispatcher import audit, config, db
from dispatcher.policy import load_all_rules
from tests.conftest import MANAGER, reads, result, submit

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = ROOT / "tests" / "fixtures" / "expected-routes.json"


def test_token_never_reaches_log(make_app, monkeypatch):
    monkeypatch.setattr(config, "AI_PROXY_TOKEN", "proxy-secret-123456")
    leaky = result("RECOMMEND_APPROVE", summary="токен proxy-secret-123456")
    client, _ = make_app([*reads("SC-84721"), submit(leaky)])
    client.post("/api/cases/RC-6001/analyze", headers=MANAGER)
    case = client.get("/api/cases/RC-6001", headers=MANAGER).json
    assert "proxy-secret-123456" not in json.dumps(case["audit"])


def test_redact_masks_key_like_strings():
    assert audit.redact("ключ sk-ant-api03-abcdefgh и Bearer abcdefghijk") == "ключ *** и ***"


def test_agent_code_does_not_read_expected_answers():
    sources = [ROOT / "app.py", *sorted((ROOT / "dispatcher").glob("*.py"))]
    for path in sources:
        assert "expected-routes" not in path.read_text(encoding="utf-8"), path.name


def test_expected_answers_cover_all_cases():
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    cases = json.loads((config.DATA_DIR / "open-cases.json").read_text(encoding="utf-8"))["cases"]
    assert set(expected) == {case["case_id"] for case in cases}


def test_policy_rules_parsed():
    assert [rule["rule_id"] for rule in load_all_rules()] == [*(f"P-{n}" for n in range(1, 7)), *(f"C-{n}" for n in range(1, 7))]


def test_connection_uses_wal_and_busy_timeout(tmp_path):
    with closing(db.connect(tmp_path / "pragma.sqlite")) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS


def test_responses_are_not_cached(make_app):
    client, _ = make_app()
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert client.get("/api/cases", headers=MANAGER).headers["Cache-Control"] == "no-store"


def test_dump_and_restore_round_trip(make_app, tmp_path):
    client, _ = make_app([*reads("SC-84721"), submit(result("RECOMMEND_APPROVE"))])
    client.post("/api/cases/RC-6001/analyze", headers=MANAGER)
    client.post("/api/cases/RC-6001/decision", headers=MANAGER, json={"decision": "APPROVE"})
    snapshot = tmp_path / "snapshot.sql"
    with closing(db.connect(client.application.config["DB_PATH"])) as conn:
        db.dump(conn, snapshot)
    with closing(db.connect(tmp_path / "restored.sqlite")) as conn:
        counts = db.restore(conn, snapshot)
        state = conn.execute("SELECT state FROM cases WHERE case_id = 'RC-6001'").fetchone()[0]
    assert counts["return_authorizations"] == 1 and counts["cases"] == 16
    assert state == "CLOSED_APPROVED"
