# 一键演示环境（production-like demo）

这个环境把 **MySQL + Agent + Java 商城/管理后台** 起在一起，演示完整链路：

```text
商城用户咨询退款
  -> 用户点“确认提交”
  -> Java 网关 /chat/consent 记录用户同意
  -> 管理后台“客服审批”看到待审批 workflow
  -> 管理员通过
  -> 用户刷新审批状态，看到结果
  -> agent_approval_record + agent_audit_log 落 MySQL
```

## 1. 准备

需要 Docker Desktop（或 Docker Engine + Compose v2）。

```powershell
cd D:\E-commerce-Agent
copy .env.demo.example .env.demo
```

打开 `.env.demo`，至少改这三个值（两边必须一致）：

```text
AGENT_INTERNAL_SECRET=换成你自己的强随机值
AGENT_RESUME_SECRET=换成另一个强随机值
AGENT_SERVICE_AUTH_TOKEN=换成服务令牌
```

> 默认是离线演示模式：`AGENT_OFFLINE_RAG=1`、`AGENT_DISABLE_LLM=1`，不需要模型 Key。  
> 想用真实模型，把这两个改成 0，并填 `AGENT_OPENAI_API_KEY`。

## 2. 启动

```powershell
docker compose --env-file .env.demo -f docker-compose.demo.yml up --build
```

首次启动会构建 Java（含前端）和 Agent 镜像，时间会长一些。

## 3. 地址和账号

| 入口 | 地址 | 账号 |
|---|---|---|
| 商城 | http://localhost:8081/ | `zhangsan / 123456` |
| 管理后台 | http://localhost:8081/admin | `admin / admin123456` |
| Agent 健康检查 | http://localhost:8000/health | - |
| MySQL | `127.0.0.1:3307` | `root / .env.demo 里的密码` |

## 4. 演示步骤

1. 商城登录 `zhangsan`。
2. 进入订单/商品，打开客服，问一个退款问题（例如“SO... 还没发货，我现在能退款吗？”）。
3. 出现“确认提交申请”卡片，点确认。
4. 客服回复“等待客服主管审批”，消息下出现“刷新审批状态”。
5. 新开窗口登录管理后台 `admin`。
6. 左侧进入“客服审批”，看到刚才的 workflow。
7. 点行，填写意见，点“通过”。
8. 回到商城，点“刷新审批状态”，看到“审批已通过”。
9. 数据库可查 `agent_approval_record`、`agent_audit_log`、`agent_workflow_checkpoint`；转人工场景还会写 `agent_handoff_ticket`。

## 5. 生产化开关说明

| 变量 | demo 值 | 作用 |
|---|---|---|
| `AGENT_AUTH_MODE` | `hmac` | Java → Agent 出站签名 + Agent 验签 |
| `AGENT_INTERNAL_SECRET` | 自定义 | 两边共用的 HMAC 密钥 |
| `AGENT_APPROVAL_MODE` | `strict` | 没有 user consent 不能审批 |
| `AGENT_STATE_BACKEND` | `sql` | checkpoint/审批/审计落 MySQL |
| `AGENT_RESUME_TOKEN_MODE` | `hmac` | 一次性、带过期的恢复令牌 |
| `AGENT_STRICT_BUSINESS_RECHECK` | `1` | 审批前拿不到实时订单就阻断 |
| `AGENT_AUTH_MODE` | `hmac` | 用户状态查询也走签名 |

## 6. 常见问题

**401 / invalid_signature**  
Java 和 Agent 的 `AGENT_INTERNAL_SECRET` 不一致，或者只重启了一边。改 `.env.demo` 后重新 `up -d --build`。

**管理后台看不到待审批**  
- 用户没有点“确认提交”，workflow 还是 `paused`，列表会显示但未 consent；先完成用户确认。
- 管理员审批后用 `docker compose logs -f agent` 看 Agent 是否报 403/401。

**端口冲突**  
8081 / 8000 / 3307 被占用时，改 `docker-compose.demo.yml` 的宿主机端口映射。

**没有模型 Key 时回答很模板化**  
这是默认离线模式，属于预期行为；填 Key 后改 `AGENT_OFFLINE_RAG=0`、`AGENT_DISABLE_LLM=0`。

## 7. 边界

- 这是演示环境，不是生产部署：单实例、种子数据、不执行真实资金退款。
- Java 会话仍是内存；重启后商城会话会丢，但 Agent 的审批记录在 MySQL。
- 真实工单、真实退款、监控告警仍未接。
