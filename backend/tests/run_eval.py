"""离线回归评测自检:不依赖真实模型/业务后端."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 仅用于本地自检:显式开启课程离线替身,生产环境禁止
os.environ.setdefault("AGENT_OFFLINE_RAG", "1")
os.environ.setdefault("AGENT_OFFLINE_FACTS", "1")
os.environ.setdefault("AGENT_DISABLE_LLM", "1")

from evals.runner import EvalRunner  # noqa: E402
from agents.customer_service_agent import Lesson41Agent  # noqa: E402
from config.settings import CASES_PATH  # noqa: E402
from db.database import init_db, state_backend  # noqa: E402


if __name__ == "__main__":
    if state_backend() == "sql":
        init_db()
    report = EvalRunner(Lesson41Agent(), CASES_PATH).run()
    print(f"total={report.total} passed={report.passed} failed={report.failed}")
    for result in report.results:
        if not result.passed:
            print("FAIL", result.case_id, result.failure_categories)
    raise SystemExit(0 if report.failed == 0 else 1)
