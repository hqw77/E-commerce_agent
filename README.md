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

## 核心能力

- **混合检索 RAG**：向量召回（语义）+ 关键词召回（长尾精确词）双路合并重排，配合查询改写、索引指纹版本缓存、证据门防幻觉，回答附 citations 可追溯。
- **只读工具调用**：通过服务间委托认证对接业务后端查询实时事实，工具全只读、参数受控、订单归属校验，异常降级转人工。
- **LangGraph 工作流 + HITL**：退款/退货走显式状态机，高风险动作必须人工审批，resume_token + 幂等键防重复提交。
- **安全与可观测**：Prompt 注入检测 + 污染脱敏，Trace 记录隐私脱敏，不泄露 hidden CoT。
- **质量闭环**：规则化回归评测 + 反馈归因回填，防止行为漂移。
