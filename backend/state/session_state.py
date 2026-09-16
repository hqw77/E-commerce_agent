"""
级内存状态 —— 模拟会话、反馈、缓存、checkpoint 和幂等记录。

【这个文件是干什么的】
用进程内全局变量模拟生产系统的持久化存储。这些状态跨请求共享，但重启即丢失。
边界：真实系统应把这些放进数据库/Redis/队列/审计系统，并处理过期、并发、权限和清理。

【各状态变量】
  - MESSAGE_COUNT_BY_SESSION  会话消息计数
  - COMMON_HIT_CACHE          常见命中缓存（如发票 FAQ 最终回答）
  - WORKFLOW_CHECKPOINTS      售后 workflow 的 checkpoint（HITL 暂停点）
  - SUBMITTED_ACTIONS         幂等记录（已提交的审批，防重复执行）
  - FEEDBACK_RECORDS          反馈记录
  - BACKFILLED_CASES          反馈回填的回归用例
"""

from __future__ import annotations

from typing import Any

from api.schemas import FeedbackRecord

MESSAGE_COUNT_BY_SESSION: dict[str, int] = {}               # 会话 ID → 消息计数
FEEDBACK_RECORDS: list[FeedbackRecord] = []                 # 反馈记录列表
BACKFILLED_CASES: list[dict[str, Any]] = []                 # 反馈回填的临时回归用例
COMMON_HIT_CACHE: dict[str, dict[str, Any]] = {}            # 常见命中缓存（如 faq:invoice_issue）
WORKFLOW_CHECKPOINTS: dict[tuple[str, str], dict[str, Any]] = {}  # (session_id, workflow_id) → checkpoint
SUBMITTED_ACTIONS: dict[str, dict[str, Any]] = {}           # 幂等键 → 已提交动作（防重复审批）
