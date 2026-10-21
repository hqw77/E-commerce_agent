# E-commerce 售后客服 Agent

基于大模型的跨境电商售后客服 Agent，通过「意图识别 → 路由分发 → 三路处理」的编排架构，将用户请求分流到 **RAG 知识检索、只读工具调用、售后工作流** 三条路径，覆盖订单查询、清关物流、活动咨询、退款退货等场景。

核心解决 Agent 落地时的三个边界问题：**AI 不乱编事实、不乱承诺退款、不越权操作**。

## 架构

```
入口层    FastAPI（7 个接口：chat / resume / trace / eval / feedback ...）
   ↓
编排层    customer_service_agent.chat()（核心调度，9 步主链路）
   ↓
处理层    三条路径按诉求性质区分：
          ├─ 稳定知识（退换政策、关税规则）→ RAG 检索 + citations 可追溯
          ├─ 实时事实（订单、物流、库存）  → 只读工具查业务后端
          └─ 高风险售后（退款、退货）      → LangGraph 工作流 + 人工审批
外围横切  注入防御 / Trace 脱敏可观测 / 成本治理 / 回归评测
```

## 目录结构

```
├── backend/              # Agent 服务（FastAPI + LangChain + LangGraph）
│   ├── agents/           # 编排层（customer_service_agent）
│   ├── api/              # 路由 + 数据契约
│   ├── tools/            # 意图识别、只读工具、运行时上下文
│   ├── rag/              # 混合检索（向量 + 关键词 + 证据门）
│   ├── workflows/        # LangGraph 售后状态机 + HITL 恢复
│   ├── safety/           # Prompt 注入防御
│   ├── observability/    # Trace 可观测
│   ├── cost/             # 成本治理
│   ├── evals/            # 回归评测
│   └── knowledge/        # 知识文档（退换政策、发票、活动、会员券）
├── ecommerce-backend/    # 业务后端（订单 / 物流 / 库存 / 售后，Spring Boot）
├── frontend/             # 调试观察台
└── doc/                  # 运行手册
```

## 快速开始

### 1. 配置环境变量

```bash
cp course.env.example course.env
# 编辑 course.env，填入你的模型 API Key（默认硅基流动 OpenAI 兼容接口）
```

### 2. 启动业务后端（Docker）

```bash
docker compose -f docker-compose.infra.yml up -d mysql ecommerce-service
```

### 3. 启动 Agent 服务

```bash
cd backend
pip install -r requirements.txt
python main.py
```

### 4. 启动调试观察台（可选）

```bash
cd frontend
npm install
npm run dev
```

## 一键演示环境

需要 Docker Desktop。复制环境变量后启动：

```bash
cp .env.demo.example .env.demo
# 修改 AGENT_INTERNAL_SECRET / AGENT_RESUME_SECRET / AGENT_SERVICE_AUTH_TOKEN
docker compose --env-file .env.demo -f docker-compose.demo.yml up --build
```

- 商城：http://localhost:8081/
- 管理后台：http://localhost:8081/admin
- 账号：`zhangsan / 123456`、`admin / admin123456`

完整步骤、账号和排错见 [DEMO.md](./DEMO.md)。

## 核心能力

- **混合检索 RAG**：向量召回（语义）+ 关键词召回（长尾精确词）双路合并重排，配合查询改写、索引指纹版本缓存、证据门防幻觉，回答附 citations 可追溯。
- **只读工具调用**：通过服务间委托认证对接业务后端查询实时事实，工具全只读、参数受控、订单归属校验，异常降级转人工。
- **LangGraph 工作流 + HITL**：退款/退货走显式状态机，高风险动作必须人工审批，resume_token + 幂等键防重复提交。
- **安全与可观测**：Prompt 注入检测 + 污染脱敏，Trace 记录隐私脱敏，不泄露 hidden CoT。
- **质量闭环**：规则化回归评测 + 反馈归因回填，防止行为漂移。

## 生产化改造

在课程版本基础上，按生产边界补了以下工程能力：

- **服务间鉴权**：`backend/api/auth.py` 支持 `AGENT_AUTH_MODE=hmac`，用 HMAC + 时间戳 + nonce 防重放；身份由凭证注入，不再信任请求体里的 `runtime_user_id`。`off` 仅用于本地调试。Java 网关的 `AgentRequestSigner` 已补出站签名。
- **管理面收口**：`/eval/run`、`/feedback/submit`、`/sessions/{id}/trace` 在 hmac 模式下要求 admin 角色；CORS 改为域名白名单。
- **HITL 令牌**：`backend/workflows/tokens.py` 在 `AGENT_RESUME_TOKEN_MODE=hmac` 时签发一次性、带过期时间的签名令牌；生产开启 `AGENT_STRICT_BUSINESS_RECHECK=1`，业务事实拿不到时 **fail-closed**。
- **状态持久化**：`backend/db/` 用 SQLAlchemy 把 workflow checkpoint、幂等记录、审批记录、审计流水落库；MySQL 行锁 + 唯一约束保证并发幂等，重启可恢复。本地可用 SQLite，生产用 MySQL。
- **审批分离**：`/chat/consent` 只记录用户同意，`/admin/approvals/{workflow_id}` 由审批角色做决定；申请人不能审批自己的单（四眼原则），审批结果写 `agent_approval_record` + `agent_audit_log`。
- **启动 fail-fast**：`backend/config/production.py` 在 `AGENT_ENV=production` 时校验鉴权、密钥、CORS、数据库、审批模式、课程/离线开关；配置不全直接拒绝启动。
- **可观测与交付**：请求 ID + JSON 结构化日志；`backend/Dockerfile` 容器化；`backend/tests/` 提供 40 条 pytest 用例、无 pytest 自检脚本和 MySQL 并发冒烟脚本。
- **本地验证**：`cd backend && pytest -q`；MySQL 持久化验证见 `tests/mysql_concurrency.py`。

## 已知边界（生产前还需补）

- memory 模式仍是默认值，适合本地演示；生产必须 `AGENT_STATE_BACKEND=sql` + MySQL。
- 商城 Java 网关已把用户确认改调 `/chat/consent`；管理后台“客服审批”页已接入 `/api/admin/agent-approvals`。
- 转人工目前只是 `next_action` 字符串，没有接工单/队列。
- 退款只记录审批结果，不执行真实资金动作；真实执行需要 outbox + 对账。
- 评测用例是规则化离线用例，不等同于线上监控或 LLM-as-judge。
- 多副本部署前需要确认 MySQL 连接池、行锁和审批唯一约束在目标环境生效。


