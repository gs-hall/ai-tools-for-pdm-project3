"""Разбор документов политики на правила с идентификаторами."""

import re
from pathlib import Path

from dispatcher.config import POLICY_DIR

POLICY_FILES = ("return-policy.md", "category-rules.md")
RULE_HEADER = re.compile(r"^## ([PC]-\d+)\. (.+)$")

CATEGORY_RULES = {
    "APPAREL": "C-1",
    "PERSONAL_ELECTRONICS": "C-2",
    "HOME_GOODS": "C-3",
    "HIGH_VALUE_ELECTRONICS": "C-4",
    "KITCHEN_APPLIANCES": "C-5",
    "WEARABLE_ELECTRONICS": "C-6",
}


def parse_rules(path: Path) -> list[dict]:
    """Возвращает правила документа: идентификатор, заголовок, текст."""
    rules: list[dict] = []
    current: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        header = RULE_HEADER.match(line)
        if header:
            current = {"rule_id": header[1], "title": header[2], "lines": [], "source": path.name}
            rules.append(current)
        elif current is not None:
            current["lines"].append(line)
    return [
        {
            "rule_id": rule["rule_id"],
            "title": rule["title"],
            "text": "\n".join(rule["lines"]).strip(),
            "source": rule["source"],
        }
        for rule in rules
    ]


def load_all_rules() -> list[dict]:
    return [rule for name in POLICY_FILES for rule in parse_rules(POLICY_DIR / name)]
