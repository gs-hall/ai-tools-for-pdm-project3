"""Журнал без секретов, эталон не читается кодом агента, правила разобраны полностью."""

import json
from pathlib import Path

from dispatcher import audit, config
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
