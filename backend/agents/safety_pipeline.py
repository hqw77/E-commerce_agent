"""横切安全检查:清理工具结果与 RAG 文本中的注入内容."""

from __future__ import annotations

from typing import Any

from api.schemas import Citation, ToolCallTrace
from observability.trace import trace_store
from safety.source_guard import inspect_source


def sanitize_external_sources(
    *,
    session_id: str,
    tool_calls: list[ToolCallTrace],
    citations: list[Citation],
    context_report: dict[str, Any],
) -> None:
    """对工具/RAG 外部文本做污染检测,命中后原位替换并写公开 Trace."""
    external_reports: list[dict[str, Any]] = []
    for call in tool_calls:
        report = inspect_source("tool_result", call.output_summary)
        if report["tainted"]:
            call.output_summary = str(report["sanitized_content"])
        external_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
    for citation in citations:
        report = inspect_source("rag_document", citation.snippet)
        if report["tainted"]:
            citation.snippet = str(report["sanitized_content"])
        external_reports.append({key: value for key, value in report.items() if key != "sanitized_content"})
    if not external_reports:
        return

    source_safety = context_report["source_safety"]
    source_safety["reports"].extend(external_reports)
    source_safety["tainted"] = source_safety["tainted"] or any(
        report["tainted"] for report in external_reports
    )
    source_safety["tainted_sources"] = sorted(
        set(source_safety["tainted_sources"])
        | {report["source"] for report in external_reports if report["tainted"]}
    )
    trace_store.add(
        session_id,
        "context_source_safety_checked",
        {
            "session_id": session_id,
            "tainted": source_safety["tainted"],
            "tainted_sources": source_safety["tainted_sources"],
            "source_count": len(source_safety["reports"]),
        },
    )
