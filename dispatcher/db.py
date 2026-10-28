"""SQLite: схема, подключение, импорт и сброс стартовых данных.

Запуск из командной строки: ``python -m dispatcher.db reset``.
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from dispatcher import audit
from dispatcher.audit import now
from dispatcher.config import DATA_DIR, DB_PATH
from dispatcher.policy import load_all_rules

SCHEMA = """
CREATE TABLE orders (
    order_number TEXT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    status TEXT NOT NULL,
    delivery_city TEXT NOT NULL
);
CREATE TABLE order_items (
    order_number TEXT NOT NULL REFERENCES orders(order_number),
    line_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price_rub INTEGER NOT NULL,
    PRIMARY KEY (order_number, line_id)
);
CREATE TABLE history (
    record_id TEXT PRIMARY KEY,
    order_number TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    summary TEXT NOT NULL,
    authorization_reference TEXT
);
CREATE TABLE cases (
    case_id TEXT PRIMARY KEY,
    order_number TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    channel TEXT NOT NULL,
    subject TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    author TEXT NOT NULL CHECK (author IN ('customer', 'manager')),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE policy_rules (
    rule_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    tools_used TEXT NOT NULL,
    raw_output TEXT NOT NULL,
    guard_errors TEXT NOT NULL,
    result TEXT NOT NULL,
    fallback INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    run_id INTEGER NOT NULL REFERENCES agent_runs(id),
    recommended_route TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT NOT NULL,
    customer_message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE return_authorizations (
    authorization_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL UNIQUE REFERENCES cases(case_id),
    order_number TEXT NOT NULL,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    created_at TEXT NOT NULL
);
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    case_id TEXT,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT NOT NULL
);
"""


BUSY_TIMEOUT_MS = 5000


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def load_json(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def drop_all(conn: sqlite3.Connection) -> None:
    tables = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in tables:
        conn.execute(f"DROP TABLE {table}")
    conn.execute("PRAGMA foreign_keys = ON")


def reset(conn: sqlite3.Connection) -> dict:
    """Пересоздаёт базу из стартовых файлов. Стартовые файлы не меняются."""
    drop_all(conn)
    conn.executescript(SCHEMA)

    orders = load_json("orders.json")["orders"]
    for order in orders:
        conn.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order["order_number"], order["customer_name"], order["created_at"],
             order["delivered_at"], order["status"], order["delivery_city"]),
        )
        conn.executemany(
            "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(order["order_number"], item["line_id"], item["sku"], item["name"],
              item["category"], item["quantity"], item["price_rub"]) for item in order["items"]],
        )

    history = load_json("return-history.json")["history"]
    conn.executemany(
        "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(h["record_id"], h["order_number"], h["opened_at"], h["closed_at"], h["status"],
          h["reason_code"], h["summary"], h["authorization_reference"]) for h in history],
    )

    cases = load_json("open-cases.json")["cases"]
    stamp = now()
    for case in cases:
        conn.execute(
            "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, 'NEW', ?)",
            (case["case_id"], case["order_number"], case["sender_name"], case["channel"],
             case["subject"], case["opened_at"], stamp),
        )
        conn.execute(
            "INSERT INTO messages (case_id, author, body, created_at) VALUES (?, 'customer', ?, ?)",
            (case["case_id"], case["letter"], case["opened_at"]),
        )

    rules = load_all_rules()
    conn.executemany(
        "INSERT INTO policy_rules VALUES (?, ?, ?, ?)",
        [(r["rule_id"], r["title"], r["text"], r["source"]) for r in rules],
    )

    counts = {"orders": len(orders), "history": len(history), "cases": len(cases), "rules": len(rules)}
    audit.write(conn, None, "server", "IMPORT", counts)
    conn.commit()
    return counts


def ensure_ready(conn: sqlite3.Connection) -> None:
    """Импортирует стартовые данные, если база пустая."""
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cases'").fetchone()
    if not exists:
        reset(conn)


def dump(conn: sqlite3.Connection, path: Path) -> int:
    """Сохраняет базу текстовым SQL-дампом — его можно положить в репозиторий и восстановить без ключа API."""
    lines = list(conn.iterdump())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def restore(conn: sqlite3.Connection, path: Path) -> dict:
    """Заменяет содержимое базы дампом."""
    drop_all(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(path.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")
    return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("cases", "agent_runs", "decisions", "return_authorizations", "audit_log")}


USAGE = """Использование:
  python -m dispatcher.db reset          сбросить базу к стартовым данным
  python -m dispatcher.db dump ФАЙЛ      сохранить базу SQL-дампом
  python -m dispatcher.db restore ФАЙЛ   восстановить базу из дампа"""


if __name__ == "__main__":
    command, *rest = sys.argv[1:] or [""]
    with closing(connect()) as connection:
        if command == "reset" and not rest:
            print(reset(connection))
        elif command == "dump" and len(rest) == 1:
            print(f"Записано строк: {dump(connection, Path(rest[0]))}")
        elif command == "restore" and len(rest) == 1:
            print(restore(connection, Path(rest[0])))
        else:
            sys.exit(USAGE)
