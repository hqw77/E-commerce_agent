# 跨境电商智能售后 Agent

一个面向电商售后场景的 **production-like Agent 演示项目**：把用户咨询分流到「RAG 知识检索 / 只读工具 / HITL 工作流」三条路径，并把「用户确认」与「客服主管审批」拆成两个独立动作。

项目重点不是“让模型多聊几句”，而是解决 Agent 落地时最容易出问题的三个边界：

- **AI 不乱编事实**：实时订单走只读工具，稳定政策走 RAG + citation。
- **AI 不乱承诺退款**：高风险售后进入状态机，停在前台确认 + 后台审批。
- **AI 不越权操作**：服务间 HMAC 鉴权，身份由凭证注入，不信任请求体。

> 说明：本项目基于课程项目骨架做了二次开发和生产化改造，定位是“可演示、可追问的工程实践”，不是完整生产系统。默认不执行真实资金退款。

---

## 目录

- [核心亮点](#核心亮点)
- [系统架构](#系统架构)
- [核心流程](#核心流程)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [一键演示](#一键演示)
- [关键设计](#关键设计)
- [API](#api)
- [配置](#配置)
- [测试与验证](#测试与验证)
- [本地开发](#本地开发)
- [已知边界与路线图](#已知边界与路线图)
- [安全说明](#安全说明)

---

## 核心亮点

| 能力 | 说明 |
|---|---|
| 三路编排 | 规则 guard + 模型路由，把请求分流到 RAG / Tool / Workflow |
| 只读工具 | 订单、物流、退款进度、商品；参数与路由确认值比对，订单归属校验 |
| 混合检索 RAG | 向量 + 关键词召回、查询改写、重排、citation、证据门、低置信转人工 |
| HITL 工作流 | LangGraph 状态机；一次性恢复令牌；冻结字段复核；幂等重放 |
| 真实审批分离 | `/chat/consent` 只记录用户同意；`/admin/approvals` 由审批角色决定 |
| 四眼原则 | 申请人不能审批自己的单 |
| MySQL 持久化 | checkpoint / 幂等 / 审批 / 审计落库；行锁 + 唯一约束保证并发唯一 |
| fail-closed 复核 | 审批前拿不到实时订单就阻断，不用审批前旧快照放行 |
| 服务间鉴权 | Java BFF 出站 HMAC 签名；Agent 校验时间戳、nonce、路径和角色 |
| 管理后台 | “客服审批”页：待审批列表、通过/拒绝/要求补充 |
| 用户状态刷新 | 商城可刷新审批状态，看到“审批中 / 已通过 / 已拒绝” |
| 转人工工单 | `transfer_to_human` 落 `agent_handoff_ticket`，管理端可查，重复触发按 session/intent/order 去重 |
| 可观测与评测 | Request ID、JSON 日志、公开 Trace、13 条离线回归、41 条 pytest |

---

## 系统架构

```mermaid
flowchart LR
    U[商城用户] -->|登录态| J[Java BFF<br/>Spring Boot]
    A[客服主管] -->|ADMIN 角色| J

    J -->|HMAC 签名<br/>用户/角色/路径/nonce| P[Agent 服务<br/>FastAPI]
    P -->|只读工具| B[业务后端<br/>订单/物流/售后]
    P -->|RAG + citation| K[知识与规则]
    P -->|Workflow + HITL| W[(MySQL<br/>agent_workflow_checkpoint)]
    P -->|审批记录/审计| M[(MySQL<br/>agent_approval_record / agent_audit_log)]
    P -->|Trace / Cost| O[公开 Trace / 日志]

    S[商城前端] --- J
    AD[管理后台] --- J
    D[调试观察台<br/>React/Vite] -.->|调试模式 auth=off| P
```

职责划分：

- **商城 / 管理后台**：用户与管理员页面，负责登录态和业务展示。
- **Java BFF**：鉴权、角色、用户资料、会话、出站 HMAC 签名、管理端代理。
- **Agent 服务**：意图路由、RAG、只读工具、Workflow/HITL、Trace、Eval、成本摘要。
- **MySQL**：业务表 + `agent_*` 状态表；两边表名隔离，避免冲突。

---

## 核心流程

### 1. 普通咨询 / 订单物流

```text
用户提问
  -> guard + 模型路由
  -> 只读工具查订单/物流（订单归属校验）
  -> 模型只基于工具 Observation 生成话术
  -> 返回 tool_calls + Trace
```

### 2. 政策 / 活动 / 发票咨询

```text
用户提问
  -> RAG 查询改写 -> 向量 + 关键词召回 -> 重排
  -> 证据门与低置信判断
  -> 生成回答 + citations
```

### 3. 高风险退款

```text
用户咨询退款
  -> Workflow 创建 checkpoint，停在 require_approval
  -> 用户点“确认提交”
  -> /chat/consent 记录 user_consented_at（不等于审批）
  -> 管理后台“客服审批”
  -> /admin/approvals/{workflow_id} 由审批角色决定
  -> 四眼校验 + 业务事实复核 + 写审批/审计
  -> 用户刷新状态：completed / rejected
```

---

## 技术栈

| 层 | 技术 |
|---|---|
| Agent | Python 3.12、FastAPI、Pydantic、LangChain、LangGraph |
| 持久化 | SQLAlchemy 2、MySQL 8、PyMySQL；本地可用 SQLite |
| 业务后端 | Java 17、Spring Boot 3、JPA、Spring Security |
| 前端 | React 19、TypeScript、Vite（商城 / 管理后台 / 调试台） |
| 部署 | Docker、Docker Compose、GitHub Actions |
| 模型 | OpenAI 兼容接口（默认硅基流动），可切换 DeepSeek 等 |

---

## 目录结构

```text
.
├── backend/                         # Agent 服务
│   ├── agents/                      # 编排层
│   ├── api/                         # 路由、鉴权、数据契约
│   ├── config/                      # 配置与生产 fail-fast
│   ├── context/                     # Runtime Context / 会话记忆
│   ├── db/                          # SQLAlchemy 模型与仓储
│   ├── workflows/                   # LangGraph 状态机、HITL 令牌、恢复
│   ├── rag/                         # 混合检索、知识、citation
│   ├── tools/                       # 意图路由、只读工具
│   ├── models/                      # 路由模型 / 回答模型
│   ├── safety/                      # Prompt 注入防御
│   ├── observability/               # Trace、Request ID、日志
│   ├── evals/                       # 离线回归
│   └── tests/                       # pytest + MySQL 并发脚本
│
├── ecommerce-backend/               # Java BFF + 商城/管理后台
│   ├── src/main/java/...            # 登录、订单、审批代理、HMAC 签名
│   └── frontend/                    # shop / admin 两套页面
│
├── frontend/                        # Agent 调试观察台（React）
├── docker-compose.demo.yml          # 一键演示
├── .env.demo.example                # 演示环境变量模板
├── DEMO.md                          # 演示步骤与排错
├── PRODUCTION-UPGRADE.md            # 生产化改造手册
└── README.md
```

---

## 一键演示

需要 Docker Desktop / Docker Engine + Compose v2。

```powershell
cd E-commerce-Agent
copy .env.demo.example .env.demo
```

打开 `.env.demo`，至少修改：

```text
AGENT_INTERNAL_SECRET=换成强随机值
AGENT_RESUME_SECRET=换成另一个强随机值
AGENT_SERVICE_AUTH_TOKEN=换成服务令牌
```

启动：

```powershell
docker compose --env-file .env.demo -f docker-compose.demo.yml up --build -d
```

访问：

| 入口 | 地址 | 账号 |
|---|---|---|
| 商城 | http://localhost:8081/ | `zhangsan / 123456` |
| 管理后台 | http://localhost:8081/admin | `admin / admin123456` |
| Agent 健康检查 | http://localhost:8000/health | - |
| MySQL | `127.0.0.1:3307` | 见 `.env.demo` |

演示步骤：

1. 商城登录 `zhangsan`。
2. 打开订单 `SO20260601090000008-a1000008`（已支付、未发货）。
3. 客服提问退款，点“确认提交申请”。
4. 页面显示“等待客服主管审批”。
5. 管理后台登录 `admin`，进入“客服审批”，点“通过”。
6. 回商城点“刷新审批状态”，看到“审批已通过”。

默认是离线演示模式（`AGENT_OFFLINE_RAG=1`、`AGENT_DISABLE_LLM=1`），不需要模型 Key。  
要真实模型：改成 `0`，并配置 `AGENT_OPENAI_API_KEY`。

更多说明见 [DEMO.md](./DEMO.md)。

---

## 关键设计

### 1. 身份不能由请求体自报

Java BFF 调 Agent 时签名：

```text
HMAC-SHA256(user_id|role|timestamp|nonce|path)
```

并发送：

```text
X-Agent-User-Id
X-Agent-Role
X-Agent-Timestamp
X-Agent-Nonce
X-Agent-Signature
```

Agent 端校验时间窗、nonce 防重放和签名，再把身份注入请求。请求体里的 `runtime_user_id` 只能与凭证一致，不能覆盖凭证。

相关代码：

- `backend/api/auth.py`
- `ecommerce-backend/.../security/AgentRequestSigner.java`
- `ecommerce-backend/.../service/RestCustomerServiceAgentClient.java`

### 2. 用户确认 ≠ 人工审批

```text
/chat/consent             记录 user_consented_at，状态 pending_approval
/admin/approvals/{id}     审批角色做最终决定
```

- strict 模式下没有用户 consent 不允许审批。
- 审批人 == 申请人时返回 `self_approval_forbidden`。
- 同一单重复审批走幂等重放，不重复写审批。

### 3. HITL 令牌与 fail-closed

- 恢复令牌：HMAC 签名、绑定 session/workflow/order、带过期、一次性消费。
- 幂等键：`idempotency_record`，重复提交返回上次结果。
- 业务复核：`AGENT_STRICT_BUSINESS_RECHECK=1` 时，实时订单拿不到直接阻断，不用旧快照放行。

### 4. MySQL 持久化

Agent 表统一使用 `agent_` 前缀，避免和 Java 业务表冲突：

```text
agent_workflow_checkpoint   # workflow 快照 + 冻结字段 + payload JSON
agent_approval_record       # 审批记录
agent_idempotency_record    # 幂等键 + 一次性令牌消费
agent_audit_log             # 审计流水
agent_handoff_ticket        # 转人工工单
```

并发审批使用 `SELECT ... FOR UPDATE` 锁 workflow 行；MySQL 8 并发测试中 8 个请求只有 1 条非重放审批。

### 5. 转人工工单

`transfer_to_human` 不再只返回字符串，而是落一张可查询的工单：

```text
transfer_to_human
  -> agent_handoff_ticket
  -> 管理端 /admin/handoff-tickets 可查
  -> 同 session/intent/order 重复触发返回原工单
```

退款/退货已经在 workflow 审批队列里的场景不重复建单，避免“审批队列 + 工单队列”双写。

### 6. 可观测与评测

- 每个请求有 `X-Request-Id`，日志为一行 JSON。
- Agent 产出公开 Trace：工具调用、RAG、Workflow、HITL、成本、降级。
- 回归评测 13 条，覆盖物流、活动规则、退款/退货、注入、降级、恢复幂等。

---

## API

### Agent（FastAPI）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/capabilities` | 能力清单 |
| POST | `/chat` | 主聊天入口 |
| POST | `/chat/consent` | 用户确认提交 |
| GET | `/chat/workflow/{workflow_id}` | 用户查询审批状态 |
| GET | `/admin/approvals` | 待审批列表（admin） |
| POST | `/admin/approvals/{workflow_id}` | 审批决定（admin） |
| GET | `/admin/handoff-tickets` | 转人工工单列表（admin） |
| GET | `/admin/handoff-tickets/{ticket_id}` | 转人工工单详情（admin） |
| POST | `/chat/resume` | HITL 恢复（审批角色 / legacy） |
| GET | `/sessions/{session_id}/trace` | 公开 Trace（admin） |
| POST | `/eval/run` | 回归评测（admin） |

Agent 交互式文档：`http://localhost:8000/docs`

### Java BFF

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 用户/管理员登录 |
| POST | `/api/customer-service/chat` | 商城客服对话 |
| POST | `/api/customer-service/resume` | 用户确认提交 |
| GET | `/api/customer-service/approval-status` | 用户查询审批状态 |
| GET | `/api/admin/agent-approvals` | 管理后台待审批列表 |
| POST | `/api/admin/agent-approvals/{workflow_id}` | 管理后台审批 |

---

## 配置

关键环境变量：

| 变量 | 说明 |
|---|---|
| `AGENT_ENV` | `dev` / `staging` / `production` |
| `AGENT_AUTH_MODE` | `off`（本地） / `hmac`（生产） |
| `AGENT_INTERNAL_SECRET` | Java → Agent HMAC 密钥 |
| `AGENT_RESUME_TOKEN_MODE` | `legacy` / `hmac` |
| `AGENT_RESUME_SECRET` | HITL 令牌密钥 |
| `AGENT_APPROVAL_MODE` | `legacy` / `strict` |
| `AGENT_STATE_BACKEND` | `memory` / `sql` |
| `AGENT_DATABASE_URL` | MySQL 或 SQLite URL |
| `AGENT_STRICT_BUSINESS_RECHECK` | `1` 时审批前 fail-closed |
| `AGENT_OFFLINE_RAG` | `1` 时使用本地字符向量，仅演示 |
| `AGENT_DISABLE_LLM` | `1` 时使用确定性规则，仅演示 |
| `AGENT_OPENAI_API_KEY` | OpenAI 兼容模型 Key |
| `AGENT_OPENAI_BASE_URL` | 模型接口地址 |
| `AGENT_OPENAI_MODEL` | 聊天模型 |

`AGENT_ENV=production` 时，`backend/config/production.py` 会强制校验鉴权、密钥、数据库、审批模式和课程开关，缺一项直接拒绝启动。

---

## 测试与验证

```powershell
cd backend

# 单元 / 集成测试
pytest -q
# 41 passed

# 离线回归（不依赖真实模型/业务后端）
python tests/run_eval.py
# total=13 passed=13 failed=0

# MySQL 持久化 + 并发审批
$env:AGENT_STATE_BACKEND="sql"
$env:AGENT_DATABASE_URL="mysql+pymysql://user:password@127.0.0.1:3306/ecommerce_agent_test?charset=utf8mb4"
python tests/mysql_concurrency.py
# 8 并发 -> approval_count=1, status=completed
```

Java / 前端：

```powershell
cd ecommerce-backend
mvn -o -q -DskipTests compile

cd frontend
npm ci
npm run build
```

Docker：

```powershell
docker compose --env-file .env.demo -f docker-compose.demo.yml config --quiet
```

完整演示也已实际跑通：用户退款咨询 → consent → 管理后台审批 → 用户状态 completed；MySQL 中 `agent_workflow_checkpoint`、`agent_approval_record`、`agent_audit_log` 均有记录。

---

## 本地开发

### Agent

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python main.py
```

默认 `AGENT_STATE_BACKEND=memory`、`AGENT_APPROVAL_MODE=legacy`，适合本地调试；生产使用 SQL + strict。

### Java BFF

```powershell
cd ecommerce-backend
mvn spring-boot:run
```

需要本地 MySQL，或使用 `docker-compose.infra.yml` 起 MySQL/Java。

### 商城 / 管理后台前端

```powershell
cd ecommerce-backend/frontend
npm ci
npm run dev
```

开发地址默认 `5174`，代理 `/api` 到 `8081`。

### Agent 调试观察台

```powershell
cd frontend
npm install
$env:VITE_AGENT_BASE_URL="http://localhost:8001"
npm run dev
```

调试台不会签 HMAC，建议单独起一个 `AGENT_AUTH_MODE=off` 的调试 Agent（例如 8001），不要直接指向生产鉴权的 8000。调试台会把当前用户的会话、消息和 Trace 持久化到浏览器 `localStorage`，刷新后仍保留。

---

## 已知边界与路线图

当前项目是“production-like demo”，不是完整生产系统。已知边界：

- 商城 Java 会话仍在内存，重启会丢；生产应迁 Redis/MySQL。
- 转人工已落 `agent_handoff_ticket` 工单，但尚未接外部 ITSM/工单队列。
- 退款只记录审批结果，不执行真实资金动作；真实执行需要 outbox + 对账。
- 课程种子数据、固定日期仍需从生产包彻底移除。
- 可观测只有日志和公开 Trace，没有完整指标/告警。
- 请求级 deadline、上游重试/熔断、SSE 流式仍待补。
- 多副本部署前需要把 nonce 缓存迁 Redis，并补 Alembic 迁移。

后续优先项：

1. 管理后台接入真实工单/客服系统。
2. 真实退款执行（审批通过后走业务 API + 对账）。
3. Prometheus / OTel / 告警。
4. Redis 会话与分布式 nonce。
5. 压测与故障注入。

更细的改造记录见 [PRODUCTION-UPGRADE.md](./PRODUCTION-UPGRADE.md)。

---

## 安全说明

- 演示账号和 `.env.demo` 默认值仅用于本地，请在上线前替换全部密钥。
- Agent 默认不应对公网开放；生产应由 BFF/网关通过内网调用。
- 不要把 `AGENT_INTERNAL_SECRET`、模型 Key、数据库密码提交到仓库。
- 本项目默认不执行真实资金退款；生产接入资金动作前必须补审批、对账和回滚机制。

---

如果这个项目对你有帮助，欢迎 star；也欢迎提 issue 讨论 Agent 的边界设计与工程化落地。
