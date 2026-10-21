# 项目一生产化改造手册（跨境电商售后 Agent）

> 目标：把课程版改造成能讲、能演示、能经得起追问的个人项目。  
> 原则：简历只写做完的事；下面每条都对应代码/命令/验收。

---

## 0. 已落地的改造

| 改造 | 文件 | 说明 |
|---|---|---|
| 服务间 HMAC 鉴权 | `backend/api/auth.py` | 时间戳 ±300s、nonce 防重放；身份由凭证注入，不信任请求体 |
| 身份对齐 | `backend/api/routes.py` | `/chat` 校验 `runtime_user_id` 与凭证一致；不一致 403 |
| 审批角色校验 | `backend/api/auth.py` | `/chat/resume` 仅 `after_sale_manager/admin` |
| 管理面收口 | `backend/api/routes.py` | `/eval`、`/feedback`、Trace 仅 admin；CORS 白名单 |
| 恢复令牌 HMAC | `backend/workflows/tokens.py` | 绑定 session/workflow/order，带过期；一次性消费 |
| 复核 fail-closed | `backend/workflows/resume.py` | `AGENT_STRICT_BUSINESS_RECHECK=1` 时，拿不到实时订单直接阻断 |
| 状态持久化 | `backend/db/` | SQLAlchemy 存 checkpoint、幂等、审批、审计；支持 MySQL/SQLite |
| 审批分离 | `backend/api/routes.py` | `/chat/consent` 记录用户同意；`/admin/approvals` 做审批；四眼原则 |
| Java 网关 consent | `ecommerce-backend/.../CustomerServiceGatewayService.java` | 商城用户确认改调 `/chat/consent`，不再直接完成审批 |
| Java 出站签名 | `ecommerce-backend/.../security/AgentRequestSigner.java` | 调 Agent 前签 HMAC + 时间戳 + nonce，与 Agent 验签对齐 |
| Java 管理端代理 | `ecommerce-backend/.../controller/AdminAgentApprovalController.java` | `/api/admin/agent-approvals` 代理 Agent 待审批列表与审批 |
| 管理后台客服审批页 | `ecommerce-backend/frontend/admin/App.tsx` | 待审批列表 + 通过/拒绝/要求补充 |
| 启动 fail-fast | `backend/config/production.py` | 生产校验鉴权、密钥、CORS、数据库、审批模式 |
| 请求 ID + JSON 日志 | `backend/observability/logging.py` | `X-Request-Id`、状态码、耗时；异常不返回堆栈 |
| Docker 化 | `backend/Dockerfile`、`.dockerignore` | 非 root、健康检查、单 worker |
| 测试与 CI | `backend/tests/`、`.github/workflows/backend-ci.yml` | 40 条 pytest；13 条离线回归；MySQL 并发冒烟 |

### 已验证

```powershell
cd D:\E-commerce-Agent\backend
pytest -q
# 40 passed

python tests\run_eval.py
# total=13 passed=13 failed=0

# MySQL 持久化 + 并发审批
$env:AGENT_STATE_BACKEND="sql"
$env:AGENT_DATABASE_URL="mysql+pymysql://root:password@127.0.0.1:3306/ecommerce_agent_test?charset=utf8mb4"
python tests\mysql_concurrency.py
# results=[False, True, ...] approval_count=1 status=completed
# after_restart_status=completed
# OK

# Java 网关编译
cd ..\ecommerce-backend
mvn -o -q -DskipTests compile
# exit 0

# 管理后台页面构建
cd frontend
npm ci
npm run build
# exit 0
```

---

## 1. 还差什么

### P0

1. **课程开关/种子数据清理**：生产包不包含 `AGENT_OFFLINE_FACTS` / `AGENT_OFFLINE_RAG` / 固定日期。
2. **转人工落地**：`transfer_to_human` 创建工单，不能只返回字符串。
3. **真实退款执行**：当前只记录审批结果，不执行资金动作；真实执行需要 outbox + 对账。
4. **演示验证**：`docker-compose.demo.yml` 已就绪，需在有 Docker 的机器上实际 `up` 一次并录 5 分钟视频。

### P1

5. 请求级 deadline + 上游超时/重试/熔断。
6. 输出 guard + PII 脱敏 + 生产禁用 `reasoning_view=teaching`。
7. Prometheus 指标 / OTel Trace / 告警。
8. `chat()` 拆分 + `create_agent` 移出热路径。
9. 前端调试台改成内部管理台，生产不部署。

---

## 2. 关键实现

### 2.1 持久化

文件：

```text
backend/db/database.py      # engine / session_scope / init_db
backend/db/models.py        # ORM 表
backend/db/repository.py    # checkpoint/consent/decide/token/audit
```

实际表名：

```text
agent_workflow_checkpoint   # workflow 快照 + 冻结字段 + payload JSON
agent_approval_record       # 审批记录
agent_idempotency_record    # 幂等键 + 一次性令牌消费
agent_audit_log             # 审计流水
```

关键设计：

- `agent_workflow_checkpoint.workflow_id` 唯一；`status`/`pending_action` 单列可查。
- 审批用 `SELECT ... FOR UPDATE` 锁住 workflow 行；同一单只有一条最终审批。
- `agent_idempotency_record.idem_key` 主键，重复审批走幂等重放。
- `resume-token:<sha256>` 写入 `agent_idempotency_record`，令牌消费跨进程一致。
- MySQL 用原生 JSON；SQLite 用通用 JSON，测试不需要额外服务。

环境变量：

```text
AGENT_STATE_BACKEND=sql
AGENT_DATABASE_URL=mysql+pymysql://user:password@host:3306/ecommerce_agent?charset=utf8mb4
```

### 2.2 审批流程

```text
用户咨询退款
  -> /chat 创建 workflow, 状态 paused, pending_action=require_approval
  -> /chat/consent 记录 user_consented_at, 状态 pending_approval
  -> /admin/approvals/{workflow_id} 管理员决定 approved/rejected/needs_more_info
  -> 写 agent_approval_record + agent_audit_log, checkpoint 状态 completed/rejected
```

规则：

- `AGENT_APPROVAL_MODE=strict` 时，没有 user consent 不允许审批。
- 审批人 == 申请人 -> `self_approval_forbidden`（四眼原则）。
- 已完成的单再次审批 -> `idempotent_replay=true`，不重复写审批。
- 审批接口要求 admin 角色；用户确认接口只要求登录用户。

### 2.3 生产配置

`backend/config/production.py` 在 `AGENT_ENV=production` 时要求：

```text
AGENT_AUTH_MODE=hmac
AGENT_INTERNAL_SECRET=...
AGENT_RESUME_TOKEN_MODE=hmac
AGENT_RESUME_SECRET=...
AGENT_STRICT_BUSINESS_RECHECK=1
AGENT_STATE_BACKEND=sql
AGENT_DATABASE_URL=mysql...
AGENT_APPROVAL_MODE=strict
AGENT_CORS_ORIGINS=精确域名
```

缺一项直接拒绝启动。

---

## 3. 面试怎么讲

**Q1：为什么用 HMAC，不用 JWT？**  
“当前是 BFF→Agent 单跳内部调用，HMAC 共享密钥足够，依赖少、延迟低；多服务/多租户时会换 mTLS 或内部 JWT，把 role/scope 放进签名载荷。”

**Q2：怎么防重放？**  
“时间戳 ±5 分钟 + nonce 缓存；先验签再记 nonce，避免攻击者用无效签名消耗正常 nonce。多副本时 nonce 放 Redis SETNX。”

**Q3：恢复令牌为什么一次性？幂等怎么办？**  
“令牌绑定 session/workflow/order，带过期；首次决定后写入 `agent_idempotency_record`。重复提交命中 idempotency_key 就走重放，返回上次结果。”

**Q4：并发审批怎么保证只有一条？**  
“`SELECT ... FOR UPDATE` 锁 workflow 行，事务内写审批并更新状态；第二个请求拿到锁后看到 completed，直接返回幂等重放。MySQL 并发测试 8 个请求只有 1 个非重放。”

**Q5：业务事实复核为什么 fail-closed？**  
“审批期间订单可能已发货/取消；业务接口不可用时如果回退旧快照，等于绕过 HITL。生产必须阻断转人工。”

**Q6：用户确认和人工审批有什么区别？**  
“用户确认只是同意提交；人工审批是客服主管的权限动作。两者状态、入口、权限分开，申请人不能批自己的单。”

---

## 4. 简历文案

见同目录 `项目一-简历文案.md`。**Java 网关改 consent、真实退款执行、工单还没做完，先不要写成完成。**
