"""
HITL 恢复与 checkpoint 层 -- 保护高风险售后动作的恢复,复核和幂等.

[这个文件是干什么的]
售后 workflow 停在"等人工审批"后,由本文件负责恢复:校验恢复令牌,
复核业务事实(防止审批期间订单状态漂移),保证幂等(重复审批不重复执行).
被 customer_service_agent.resume() 调用(对应 POST /chat/resume 接口).

[怎么跑起来]
调用链:
    POST /chat/resume -> customer_service_agent.resume()
      -> 本文件的 resume_from_checkpoint()   <- 唯一对外入口
        -> business_recheck(复核冻结事实)-> 记录审批结果 + 保证幂等

[核心流程:恢复的三重校验 + 幂等]
  ① checkpoint 存在?        不存在 -> blocked(checkpoint_not_found)
  ② resume_token 匹配?      不匹配 -> blocked(invalid_resume_token)
  ③ 业务事实复核?           订单状态漂移 -> blocked(business_fact_drift)
  ④ 幂等:同一 idempotency_key 已提交过,直接复用结果(不重复执行)

[关键设计:为什么需要冻结 + 复核 + 幂等]
  1. 冻结字段:审批前把订单状态"冻结"进 checkpoint,恢复时对比,防止
     "审批期间订单已发货/已取消"却继续执行退款.
  2. 幂等:同一审批请求重复提交(网络重试等),不会重复记录退款.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from api.schemas import *
from config.production import approval_mode
from config.settings import TRACE_SCHEMA_VERSION
from db import repository
from integrations.ecommerce_client import order_fact_from_ecommerce
from observability.trace import public_trace_summary
from tools.runtime_context import logistics_status_from_order, order_no, order_status
from workflows import tokens as resume_tokens


def build_resume_token(session_id: str, workflow_id: str, order_id: str | None) -> str:
    """生成 HITL 恢复令牌;hmac 模式为一次性签名令牌,legacy 模式保留课程格式."""
    return resume_tokens.issue_resume_token(
        session_id=session_id,
        workflow_id=workflow_id,
        order_id=order_id,
    )


def freeze_workflow_fields(request: ChatRequest, order: dict[str, Any] | None) -> dict[str, Any]:
    """冻结审批前的关键业务事实,恢复时用来检查订单状态是否漂移."""
    return {
        "runtime_user_id": request.runtime_user_id,
        "order_id": order_no(order) if order else None,
        "order_status": order_status(order) if order else None,
        "logistics_status": logistics_status_from_order(order) if order else None,
    }


def business_recheck(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """恢复前复核冻结事实:对比审批前的订单状态和当前状态,防止漂移后继续执行."""
    frozen = checkpoint["frozen_fields"]
    order_id = frozen.get("order_id")
    current_order = order_fact_from_ecommerce(order_id, str(frozen["runtime_user_id"])) if order_id else None
    if current_order is None:
        # 生产必须 fail-closed:拿不到实时事实就阻断,不能用审批前的旧快照放行.
        if os.getenv("AGENT_STRICT_BUSINESS_RECHECK", "0") == "1":
            return {
                "passed": False,
                "reason": "business_fact_unavailable",
                "mismatches": {"order_id": {"frozen": order_id, "current": None}},
            }
        current_order = checkpoint.get("order_snapshot")
    if current_order is None:
        return {"passed": False, "reason": "order_not_found", "mismatches": {"order_id": {"frozen": order_id, "current": None}}}
    mismatches: dict[str, Any] = {}
    current_values = {
        "order_status": order_status(current_order),
        "logistics_status": logistics_status_from_order(current_order),
    }
    for field in ("order_status", "logistics_status"):
        if frozen.get(field) != current_values.get(field):
            mismatches[field] = {"frozen": frozen.get(field), "current": current_values.get(field)}
    return {"passed": not mismatches, "reason": None if not mismatches else "business_fact_drift", "mismatches": mismatches}


def build_refund_workflow_checkpoint(
    *,
    request: ChatRequest,
    order: dict[str, Any] | None,
    order_id: str | None,
    citations: list[Citation],
) -> dict[str, Any]:
    """组装并保存未发货退款 HITL checkpoint(含冻结字段 + 幂等键 + 恢复令牌)."""
    workflow = {
        "workflow_id": f"wf-{request.session_id}",
        "session_id": request.session_id,
        "workflow_type": "unshipped_refund",
        "status": "paused",
        "pending_action": "require_approval",
        "order_id": order_no(order) if order else order_id,
        "resume_token": build_resume_token(request.session_id, f"wf-{request.session_id}", order_id),
        "approval_id": f"appr-{request.session_id}",
        "idempotency_key": f"hitl:{request.session_id}:{order_id}",
        "frozen_fields": freeze_workflow_fields(request, order),
    }
    checkpoint = {
        "session_id": request.session_id,
        "workflow": workflow,
        "frozen_fields": workflow["frozen_fields"],
        "resume_token": workflow["resume_token"],
        "idempotency_key": workflow["idempotency_key"],
        "citations": [citation.model_dump() for citation in citations],
        "order_snapshot": order,
        "user_consented_at": None,
    }
    repository.save_checkpoint(checkpoint)
    return workflow


def blocked_resume_response(
    request: ChatResumeRequest,
    reason: str,
    trace_store: Any,
    business_recheck_payload: dict[str, Any] | None = None,
) -> ChatResumeResponse:
    """构造被阻断的恢复响应,把失败原因显式暴露给观察台."""
    recheck = business_recheck_payload or {"passed": False, "reason": reason, "mismatches": {}}
    return ChatResumeResponse(
        session_id=request.session_id,
        workflow_id=request.workflow_id,
        status="blocked",
        answer="审批恢复没有通过校验，不能继续执行高风险售后动作。",
        resume_result={"accepted": False, "decision": request.decision, "idempotent_replay": False, "request_id": None, "reason": reason},
        workflow=None,
        business_recheck=recheck,
        session_state={
            "agent_version": "v1",
            "workflow": None,
            "trace": {"schema_version": TRACE_SCHEMA_VERSION, "event_count": len(trace_store.list(request.session_id)), "public_trace_only": True},
        },
    )


def resume_from_checkpoint(
    request: ChatResumeRequest,
    trace_store: Any,
    *,
    require_resume_token: bool = True,
) -> ChatResumeResponse:
    """主入口:恢复暂停的 HITL workflow.校验令牌,复核业务事实并保持幂等."""
    # ① checkpoint 存在?sql 模式从 MySQL/SQLite 读,memory 模式读进程内快照
    checkpoint = repository.load_checkpoint(request.session_id, request.workflow_id)
    if checkpoint is None:
        return blocked_resume_response(request, "checkpoint_not_found", trace_store)

    # ② 恢复令牌:hmac 模式校验签名/过期/绑定关系;legacy 模式保持精确匹配
    if require_resume_token:
        if resume_tokens.token_mode() == "hmac":
            token_ok, _reason = resume_tokens.verify_resume_token(
                request.resume_token,
                session_id=request.session_id,
                workflow_id=request.workflow_id,
                order_id=checkpoint["frozen_fields"].get("order_id"),
            )
            if not token_ok:
                return blocked_resume_response(request, "invalid_resume_token", trace_store)
        elif checkpoint["resume_token"] != request.resume_token:
            return blocked_resume_response(request, "invalid_resume_token", trace_store)

    # ③ 幂等:同一审批已提交过,直接复用结果;一次性令牌只有幂等重放才允许重复使用
    idempotency_key = checkpoint["idempotency_key"]
    idempotent_replay = repository.get_submitted_action(idempotency_key) is not None
    if (
        require_resume_token
        and resume_tokens.token_mode() == "hmac"
        and resume_tokens.is_token_used(request.resume_token)
        and not idempotent_replay
    ):
        return blocked_resume_response(request, "resume_token_already_used", trace_store)

    # ④ 业务事实复核(订单状态是否漂移)
    recheck = business_recheck(checkpoint)
    if not recheck["passed"]:
        reason = str(recheck.get("reason") or "business_fact_drift")
        return blocked_resume_response(request, reason, trace_store, business_recheck_payload=recheck)

    # ⑤ 审批决定落库:strict 模式要求先有 user consent,且审批人不能是申请人
    strict = approval_mode() == "strict"
    try:
        result = repository.decide(
            session_id=request.session_id,
            workflow_id=request.workflow_id,
            decision=request.decision,
            reviewer_id=request.reviewer_id,
            reviewer_role=request.reviewer_role,
            note=request.reviewer_note,
            idem_key=idempotency_key,
            require_consent=strict,
            enforce_four_eyes=True,
        )
    except repository.ConsentRequired:
        return blocked_resume_response(request, "user_consent_required", trace_store, business_recheck_payload=recheck)
    except repository.SelfApprovalForbidden:
        return blocked_resume_response(request, "self_approval_forbidden", trace_store, business_recheck_payload=recheck)
    except repository.CheckpointNotFound:
        return blocked_resume_response(request, "checkpoint_not_found", trace_store)

    workflow = result.checkpoint["workflow"]
    idempotent_replay = result.idempotent_replay
    request_id = result.request_id

    if request.decision == "approved":
        status: Literal["completed", "paused", "rejected", "blocked"] = "completed"
        answer = "售后主管已批准模拟退款申请，系统记录审批通过；本版本不执行真实资金退款。"
    elif request.decision == "rejected":
        status = "rejected"
        answer = "售后主管已拒绝本次模拟退款申请，Agent 只能把结果告知用户，不能绕过人工审批。"
    else:
        status = "paused"
        answer = "售后主管要求补充信息，workflow 继续暂停，等待用户或客服补齐材料。"

    # ⑥ 一次性令牌消费:首次决定后标记;幂等重放不再标记
    if (
        require_resume_token
        and resume_tokens.token_mode() == "hmac"
        and not idempotent_replay
    ):
        resume_tokens.mark_token_used(request.resume_token)

    trace_store.add(
        request.session_id,
        "workflow_resumed",
        {
            "session_id": request.session_id,
            "workflow_id": request.workflow_id,
            "pending_action": workflow["pending_action"],
            "status": status,
        },
    )
    trace_store.add(
        request.session_id,
        "human_approval_resolved",
        {
            "session_id": request.session_id,
            "workflow_id": request.workflow_id,
            "decision": request.decision,
            "reviewer_role": request.reviewer_role,
            "status": status,
        },
    )
    cost_summary = {
        "schema_version": "cost_summary_v1",
        "path_type": "hitl_resume_path",
        "model_calls": {"route_planner": 0, "final_answer": 0, "extra_reasoning": 0},
        "tool_call_count": 0,
        "business_tool_call_count": 0,
        "rag": {"needs_rag": False, "hit_count": 0, "cache_hit": False},
        "workflow": {"used_langgraph": True, "workflow_id": request.workflow_id, "hitl_required": False, "status": status},
        "safety_boundary": {
            "cost_control_does_not_skip_business_facts": True,
            "cost_control_does_not_skip_hitl": True,
            "not_finops_or_billing_system": True,
        },
    }
    trace_store.add(request.session_id, "cost_recorded", cost_summary)
    return ChatResumeResponse(
        session_id=request.session_id,
        workflow_id=request.workflow_id,
        status=status,
        answer=answer,
        resume_result={
            "accepted": True,
            "decision": request.decision,
            "idempotent_replay": idempotent_replay,
            "request_id": request_id,
            "reason": "approval_recorded",
        },
        workflow=workflow,
        business_recheck=recheck,
        session_state={
            "agent_version": "v1",
            "workflow": workflow,
            "cost_summary": cost_summary,
            "trace": public_trace_summary(request.session_id),
        },
    )
