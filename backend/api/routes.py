"""
FastAPI 路由层 -- HTTP 入口,只负责"接请求 -> 鉴权 -> 转发给对应模块".

生产边界:
  - /chat,/chat/resume 接入服务间 HMAC 鉴权(AGENT_AUTH_MODE=hmac 时生效).
  - /eval/run,/feedback/submit,/sessions/{id}/trace 属于管理面,hmac 模式下仅 admin.
  - 请求带 X-Request-Id,输出结构化日志;启动时校验生产配置.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agents.customer_service_agent import Lesson41Agent
from api.auth import (
    Principal,
    auth_mode,
    enforce_chat_identity,
    enforce_reviewer_identity,
    get_principal,
    require_admin,
)
from api.schemas import *
from config.production import approval_mode, validate_startup_config
from config.settings import CASES_PATH, load_agent_capabilities
from db import repository
from db.database import init_db, state_backend
from evals.runner import EvalRunner
from feedback.attribution import FailureAttributor, build_backfilled_case
from observability.logging import (
    configure_logging,
    current_request_id,
    request_logging_middleware,
)
from observability.trace import trace_store
from state.session_state import BACKFILLED_CASES, FEEDBACK_RECORDS
from workflows import tokens as resume_tokens
from workflows.resume import resume_from_checkpoint

logger = logging.getLogger("agent.routes")
configure_logging()


def _cors_origins() -> list[str]:
    """CORS 白名单;默认只放行本地调试台,生产通过 AGENT_CORS_ORIGINS 配置."""
    raw = os.getenv(
        "AGENT_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时做配置 fail-fast,并按需初始化持久化表."""
    validate_startup_config()
    if state_backend() == "sql":
        init_db()
        logger.info("state_backend=sql tables_ready=true")
    logger.info(
        "agent_started env=%s auth_mode=%s approval_mode=%s",
        os.getenv("AGENT_ENV", "dev"),
        auth_mode(),
        approval_mode(),
    )
    yield


# 实例化核心组件(服务启动时创建,进程内单例)
agent = Lesson41Agent()
eval_runner = EvalRunner(agent, CASES_PATH)
eval_runner.backfilled_cases = BACKFILLED_CASES
failure_attributor = FailureAttributor()

app = FastAPI(title="E-commerce After-sale Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "X-Request-Id"],
)
app.middleware("http")(request_logging_middleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常:不把堆栈返回给调用方,只留 request_id 方便定位."""
    logger.exception(
        "unhandled_exception",
        extra={"request_id": current_request_id(), "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "internal_error", "request_id": current_request_id()},
    )


@app.get("/health")
def health() -> dict[str, str]:
    """存活探针;不暴露密钥和内部地址."""
    return {
        "status": "ok",
        "env": os.getenv("AGENT_ENV", "dev"),
        "auth_mode": auth_mode(),
        "approval_mode": approval_mode(),
        "state_backend": state_backend(),
    }


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    """返回当前能力清单(前端据此点亮/置灰功能)."""
    return load_agent_capabilities()


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, principal: Principal | None = Depends(get_principal)) -> ChatResponse:
    """主入口:鉴权对齐身份后,转发给 Agent 编排层."""
    enforce_chat_identity(request, principal)
    return agent.chat(request)


# HITL 审批恢复
@app.post("/chat/resume", response_model=ChatResumeResponse)
def chat_resume(
    request: ChatResumeRequest,
    principal: Principal | None = Depends(get_principal),
) -> ChatResumeResponse:
    """恢复一个暂停在 HITL 节点的售后 workflow(仅审批角色)."""
    enforce_reviewer_identity(request, principal)
    return agent.resume(request)


# 用户同意提交:不等于人工审批
@app.post("/chat/consent", response_model=ChatConsentResponse)
def chat_consent(
    request: ChatConsentRequest,
    principal: Principal | None = Depends(get_principal),
) -> ChatConsentResponse:
    """记录用户同意提交,等待客服主管审批."""
    checkpoint = repository.load_checkpoint(request.session_id, request.workflow_id)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="checkpoint_not_found")
    user_id = principal.user_id if principal else (request.user_id or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id_required")
    if approval_mode() == "strict" and not request.resume_token:
        raise HTTPException(status_code=400, detail="resume_token_required")
    if request.resume_token:
        token_ok, reason = resume_tokens.verify_resume_token(
            request.resume_token,
            session_id=request.session_id,
            workflow_id=request.workflow_id,
            order_id=(checkpoint.get("frozen_fields") or {}).get("order_id"),
        )
        if not token_ok:
            raise HTTPException(status_code=403, detail=reason)
    try:
        checkpoint = repository.record_consent(
            session_id=request.session_id,
            workflow_id=request.workflow_id,
            user_id=user_id,
        )
    except repository.OwnerMismatch:
        raise HTTPException(status_code=403, detail="workflow_owner_mismatch")
    except repository.CheckpointNotFound:
        raise HTTPException(status_code=404, detail="checkpoint_not_found")
    trace_store.add(
        request.session_id,
        "user_consent_recorded",
        {
            "session_id": request.session_id,
            "workflow_id": request.workflow_id,
            "status": "pending_approval",
        },
    )
    return ChatConsentResponse(
        session_id=request.session_id,
        workflow_id=request.workflow_id,
        status="pending_approval",
        user_consented_at=checkpoint.get("user_consented_at"),
        message="已记录用户同意提交,等待客服主管审批。",
    )


# 用户查询自己的 workflow 审批状态
@app.get("/chat/workflow/{workflow_id}", response_model=WorkflowStatusResponse)
def workflow_status(
    workflow_id: str,
    principal: Principal | None = Depends(get_principal),
) -> WorkflowStatusResponse:
    """只返回状态字段;不暴露 resume_token 或订单快照."""
    status = repository.get_workflow_status(workflow_id)
    if status is None:
        raise HTTPException(status_code=404, detail="workflow_not_found")
    owner = status.get("user_id")
    if principal is not None and owner and owner != principal.user_id:
        raise HTTPException(status_code=403, detail="workflow_owner_mismatch")
    return WorkflowStatusResponse(
        workflow_id=status["workflow_id"],
        session_id=status["session_id"],
        status=status["status"],
        pending_action=status["pending_action"],
        order_no=status.get("order_no"),
        user_consented_at=status.get("user_consented_at"),
        decided_at=status.get("decided_at"),
        decision=status.get("decision"),
        reviewer_role=status.get("reviewer_role"),
    )


# 管理员待审批列表
@app.get(
    "/admin/approvals",
    response_model=list[PendingApproval],
    dependencies=[Depends(require_admin)],
)
def list_pending_approvals(limit: int = 50) -> list[PendingApproval]:
    """返回待审批 workflow;不暴露 resume_token 和订单快照."""
    return [PendingApproval(**item) for item in repository.list_pending(limit=limit)]


# 管理员审批:四眼原则 + 审计
@app.post(
    "/admin/approvals/{workflow_id}",
    response_model=ChatResumeResponse,
    dependencies=[Depends(require_admin)],
)
def decide_approval(
    workflow_id: str,
    request: ApprovalDecisionRequest,
    principal: Principal | None = Depends(get_principal),
) -> ChatResumeResponse:
    """客服主管审批;审批人不能是申请人."""
    reviewer_id = principal.user_id if principal else (request.reviewer_id or "admin-console")
    reviewer_role = principal.role if principal else "after_sale_manager"
    resume_request = ChatResumeRequest(
        session_id=request.session_id,
        workflow_id=workflow_id,
        resume_token="",
        reviewer_id=reviewer_id,
        reviewer_role=reviewer_role,
        decision=request.decision,
        reviewer_note=request.note,
    )
    return resume_from_checkpoint(resume_request, trace_store, require_resume_token=False)


# 会话 Trace 查询(管理面)
@app.get(
    "/sessions/{session_id}/trace",
    response_model=list[TraceEvent],
    dependencies=[Depends(require_admin)],
)
def session_trace(session_id: str) -> list[TraceEvent]:
    """返回指定会话的公开 Trace."""
    return trace_store.list(session_id)


# 回归评测(管理面)
@app.post(
    "/eval/run",
    response_model=EvalRunResponse,
    dependencies=[Depends(require_admin)],
)
def run_eval(request: EvalRunRequest) -> EvalRunResponse:
    """运行大促综合演练回归评测."""
    return eval_runner.run(case_id=request.case_id)


# 反馈提交 + 归因回填(管理面)
@app.post(
    "/feedback/submit",
    response_model=FeedbackSubmitResponse,
    dependencies=[Depends(require_admin)],
)
def submit_feedback(request: FeedbackRequest) -> FeedbackSubmitResponse:
    """提交反馈 -> 生成归因 -> 回填成临时回归 case(闭环)."""
    eval_report = eval_runner.run(case_id=request.case_id) if request.case_id else None
    eval_result = eval_report.results[0] if eval_report and eval_report.results else None
    events = trace_store.list(request.session_id)
    attributions = failure_attributor.attribute(feedback=request, trace_events=events, eval_result=eval_result)
    base_case = next((case for case in eval_runner.load_cases() if case["case_id"] == request.case_id), None)
    backfilled_case = build_backfilled_case(request, attributions, base_case)
    BACKFILLED_CASES.append(backfilled_case)
    record = FeedbackRecord(
        feedback_id=f"fb-{len(FEEDBACK_RECORDS) + 1:03d}",
        session_id=request.session_id,
        case_id=request.case_id,
        rating=request.rating,
        user_comment=request.user_comment,
        trace_event_names=[event.event_type for event in events],
        eval_failure_categories=eval_result.failure_categories if eval_result else [],
        attributions=attributions,
        backfilled_case=backfilled_case,
    )
    FEEDBACK_RECORDS.append(record)
    return FeedbackSubmitResponse(record=record, eval_report=eval_report)
