"""
FastAPI 路由层 —— HTTP 入口，只负责「接请求 → 转发给对应模块」。

【这个文件是干什么的】
整个服务的 HTTP 接口定义：把外部的 /chat、/eval/run 等请求，转发给
Agent、评测(eval)、反馈、Trace 等模块。它自己不做业务逻辑，只是「接线层」。

【怎么跑起来】
main.py 启动 uvicorn，加载这里的 app 对象（`from api.routes import app`）。
启动命令：cd backend && python main.py

【核心流程：7 个接口】
  /health                 健康检查
  /capabilities           能力清单（前端据此点亮/置灰功能）
  /chat                   主入口：一次聊天（→ agent.chat）
  /chat/resume            HITL 审批恢复（→ agent.resume）
  /sessions/{id}/trace    公开 Trace（→ trace_store）
  /eval/run               回归评测（→ eval_runner）
  /feedback/submit        反馈提交 + 归因回填（→ failure_attributor）
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agents.customer_service_agent import Lesson41Agent
from api.schemas import *
from config.settings import CASES_PATH, load_agent_capabilities
from evals.runner import EvalRunner
from feedback.attribution import FailureAttributor, build_backfilled_case
from observability.trace import trace_store
from state.session_state import BACKFILLED_CASES, FEEDBACK_RECORDS


# 实例化核心组件（服务启动时创建，进程内单例）
agent = Lesson41Agent()
eval_runner = EvalRunner(agent, CASES_PATH)
eval_runner.backfilled_cases = BACKFILLED_CASES
failure_attributor = FailureAttributor()

app = FastAPI(title="Lesson 41 Xiaozhe Agent Final Rehearsal")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 演示放开跨域，生产需收紧
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok", "lesson": "41"}


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    """返回当前能力清单。"""
    return load_agent_capabilities()


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """主入口：处理一次聊天请求，转发给 Agent 编排层。"""
    return agent.chat(request)

# HITL 审批恢复
@app.post("/chat/resume", response_model=ChatResumeResponse)
def chat_resume(request: ChatResumeRequest) -> ChatResumeResponse:
    """恢复一个暂停在 HITL 节点的售后 workflow（人工审批后恢复）。"""
    return agent.resume(request)

# 会话 Trace 查询
@app.get("/sessions/{session_id}/trace", response_model=list[TraceEvent])
def session_trace(session_id: str) -> list[TraceEvent]:
    """返回指定会话的公开 Trace。"""
    return trace_store.list(session_id)

# 回归评测
@app.post("/eval/run", response_model=EvalRunResponse)
def run_eval(request: EvalRunRequest) -> EvalRunResponse:
    """运行大促综合演练回归评测。"""
    return eval_runner.run(case_id=request.case_id)

# 反馈提交 + 归因回填（闭环）
@app.post("/feedback/submit", response_model=FeedbackSubmitResponse)
def submit_feedback(request: FeedbackRequest) -> FeedbackSubmitResponse:
    """提交反馈 → 生成归因 → 回填成临时回归 case（闭环）。"""
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
