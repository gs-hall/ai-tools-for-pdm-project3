"""Пути и настройки приложения."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STARTER = ROOT / "return-dispatcher-starter"
DATA_DIR = STARTER / "data"
POLICY_DIR = STARTER / "policy"
RUNTIME_DIR = ROOT / "data" / "runtime"

load_dotenv(ROOT / ".env")

DB_PATH = Path(os.environ.get("DISPATCHER_DB", RUNTIME_DIR / "dispatcher.sqlite"))
AI_PROXY_URL = os.environ.get("AI_PROXY_URL", "").rstrip("/")
AI_PROXY_TOKEN = os.environ.get("AI_PROXY_TOKEN", "")
AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-5")

RETURN_WINDOW_DAYS = 14
HIGH_VALUE_RUB = 30_000
