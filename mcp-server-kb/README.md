# Connect KB MCP Server

面向 Amazon Connect 的 **知识库自助问答 + 信息收集转人工** MCP Server，一键部署到 AWS。
基于最新 **AgentCore CLI（`@aws/agentcore`）** 构建：Runtime + Gateway + 全托管 Knowledge Base
全部由 CLI/CDK 管理，容器镜像通过 **CodeBuild（ARM64）** 在云端构建，**无需本地 Docker**。

> 本方案只部署 AWS 侧。Amazon Connect 实例、AI Agent 的挂载与话术由客户自行完成——
> 我们提供一段可直接粘贴的 SOP：`app/kbserver/skills/connect-agent-sop.md`。

## 架构

```
Connect AI Agent  (客户自建，不在本方案内)
  │  MCP over HTTPS（Gateway 鉴权：默认 NONE，可切 CUSTOM_JWT）
  ▼
AgentCore Gateway (connectkb-gw)
  ▼  单个 target: mcpServer (GATEWAY_IAM_ROLE)
AgentCore Runtime (kbserver, ARM64 容器)  ← app/kbserver/
  ├─ searchKnowledgeBase(query, topK?)
  │      → bedrock-agent-runtime.Retrieve → 全托管 Bedrock KB (connectkb-kb)
  │      → 容器内 Strands agent 生成中文答案 + confidence
  ├─ collectCustomerInfo(...)  → 校验最小字段集，返回缺失项（多轮补全）
  ├─ createPreTicket(...)      → 写 DynamoDB (connectkb-pretickets)
  └─ getPreTicket(ticketId)    → 读 DynamoDB

部署期还会创建：
  KB 文档 S3 桶 (sample-docs 上传 → ingestion)  ── 数据源 ──▶ 全托管 KB
  DynamoDB 预工单表 (connectkb-pretickets)
```

**为什么 KB 检索封装成一个 tool（而非 Gateway managed-kb connector 直连）？**
让 Connect 只对接**一个** MCP endpoint，并把意图理解 / 置信度判断 / 多轮编排 / 兜底逻辑
都放进容器，方便后续在 MCP server 内直接扩展 agent 能力（容器里已内置 Strands + BedrockModel）。

## MCP Tools

| Tool | 入参 | 功能 | 返回 |
|------|------|------|------|
| `searchKnowledgeBase` | `query`(必填), `topK`(可选, 默认5) | 检索 KB + 生成带依据答案 | `{answer, confidence(HIGH/MEDIUM/LOW), resolvedSuggestion, citations}` |
| `collectCustomerInfo` | `productModel`,`problemDescription`,`contact`(必填); `serialNumber`(可选) | 校验最小字段集 | `{complete, missing, normalized}` |
| `createPreTicket` | 同上齐全字段 + `sessionSummary`(可选) | 写 DynamoDB 预工单 | `{ticketId, status, createdAt}` |
| `getPreTicket` | `ticketId`(必填) | 读预工单 | 预工单字段 / `{error:"NOT_FOUND"}` |

对话轮次控制（默认 ≤3 轮）、"是否解决"确认、低置信度/多轮未解决/用户要求人工 → 兜底转人工，
这些**编排逻辑属于 Connect AI Agent 的 SOP**（见 `skills/connect-agent-sop.md`），不在容器代码内。

## 前置条件

- **Node.js 20+ / npm**、**AWS CLI**、**Python 3.10+ 与 uv**（CloudShell 均自带或易装）。
- 安装最新 AgentCore CLI：`npm install -g @aws/agentcore`
- **不需要本地 Docker** —— 容器镜像由 AgentCore 在部署时用 **CodeBuild** 构建。
- AWS 凭证已配置（`aws configure` 或环境变量）。

## 一键部署

```bash
cd mcp-server-kb
chmod +x deploy.sh cleanup.sh

# 1) 复制并按需编辑配置（region / Gateway 鉴权）
cp .env.example .env
#   - 只做 PoC：保持默认即可（us-east-1、Gateway 免鉴权 NONE）
#   - 要在 Connect 里用 JWT 鉴权测试：把 OIDC_DISCOVERY_URL 填成 Connect 实例的
#     https://<实例域名>.my.connect.aws/.well-known/openid-configuration

# 2) 部署
./deploy.sh
```

> `.env` 是给 `deploy.sh` 用的（region + Gateway 入站鉴权），与 `agentcore/.env.local`
> （CLI 自己的运行时 secrets 文件）**不是**同一个；只用 Bedrock 时后者留空即可。
> `.env` 已被 `.gitignore` 忽略，不会提交。命令行内联变量（如 `REGION=us-west-2 ./deploy.sh`）
> 会覆盖 `.env` 里的同名值。

`deploy.sh` 依次（**幂等，可反复跑**；从零 fresh clone 一次跑通）：
1. 用当前 STS 身份 + `$REGION` 生成 `agentcore/aws-targets.json`（CLI 会校验目标账号与凭证一致）
2. 建 DynamoDB 预工单表 `connectkb-pretickets`
3. 建 KB 文档桶并上传 `app/kbserver/sample-docs/`
4. 把文档桶 URI 与 Gateway 鉴权写进 `agentcore/agentcore.json`（并 `agentcore validate`）
5. `npm ci` 安装 CDK 依赖（fresh clone 无 `node_modules` 时），再 **`agentcore deploy`**
   —— CDK 部署 Runtime（CodeBuild 构建 ARM64 镜像）+ Gateway + 全托管 KB，并自动 ingest 示例文档

> **前置**：`node`/`npm`、`aws` CLI、`python3`、`uv`，以及 `npm i -g @aws/agentcore`。
> AWS 凭证已配置。**不需要本地 Docker**。
> `agentcore.json` 里 Gateway 用 **`protocolType: None` + `httpRuntime` target**（按名引用 runtime，
> 部署时解析 ARN 并自动授予 `InvokeAgentRuntime`），所以 Runtime + Gateway + Target 一次部署即成，无需二次 pass。

部署完成后：
```bash
agentcore status         # 查看 Runtime / Gateway / KB 状态与 endpoint（在项目根目录跑）
# 验证一次工具调用：
agentcore invoke --gateway-target-name kbserver-target \
  --tool searchKnowledgeBase --input '{"query":"保修期多久"}' call-tool
```
> KB ingestion 完成后，向量索引可能还需 ~1 分钟预热；紧接着的第一两次检索若返回 LOW/0 命中，稍等重试即可。

拿到 Gateway 的 MCP endpoint，去 Connect 控制台把它配到 AI Agent；再把
`app/kbserver/skills/connect-agent-sop.md` 粘贴进 AI Agent 指令。

> ⚠️ **NONE 鉴权**：任何人拿到 Gateway MCP endpoint 都能调用，仅用于 PoC/内网。
> 上线务必用 `OIDC_DISCOVERY_URL=... ./deploy.sh` 切到 CUSTOM_JWT。

### CUSTOM_JWT（Connect Integration 测试）

填 `OIDC_DISCOVERY_URL` 后，`deploy.sh` 会做**两遍部署**：
1. 先用该 discoveryUrl 建 Gateway（CUSTOM_JWT，audience 占位）；
2. 读到新 Gateway 的 ID 后，把 `allowedAudience` 改成 **Gateway 自身 ID** 再 deploy 一次。

原因：Connect 颁发的 JWT 中 `aud` claim 等于它要调用的 Gateway 的 ID；audience 不一致
会报 `insufficient_scope`。Gateway ID 部署前不可知，故必须两遍。

```bash
OIDC_DISCOVERY_URL="https://<实例域名>.my.connect.aws/.well-known/openid-configuration" \
  REGION=us-east-1 ./deploy.sh
# 可选：限制 client id  →  OIDC_ALLOWED_CLIENTS="id1,id2"
```

> ⚠️ **鉴权类型不可原地切换**：AgentCore 不允许把已存在的 Gateway 从 NONE 改成 CUSTOM_JWT
> （反之亦然）——报 "Authorizer type cannot be updated for an existing gateway"。deploy.sh 会
> 检测到这种不一致并提示先 `./cleanup.sh --yes` 再重部署（Gateway 需重建）。

部署后在 Connect 控制台 **Integrations** 里把 Gateway 的 MCP endpoint 加为 MCP server，
用同一 Connect 实例的身份即可通过 JWT 鉴权测试。

## 替换为真实知识库

`app/kbserver/sample-docs/` 是占位示例。上线前替换为客户真实文档（PDF/MD/TXT 等），
重跑 `./deploy.sh` 即可（会重新 sync 到文档桶并触发 ingestion）。

## 重新部署

改了代码或配置后再跑一次 `./deploy.sh`。`agentcore deploy` 幂等，只在源码变化时
（按 source asset hash）重新走 CodeBuild 构建。

## 清理

```bash
cd mcp-server-kb
./cleanup.sh          # 交互确认；或 ./cleanup.sh --yes
```
`agentcore destroy` 删除 CLI 托管栈（Runtime / Gateway / 全托管 KB），
脚本再额外删除 DynamoDB 表（⚠️ 含已存预工单）与 KB 文档桶。

## 鉴权与 IAM

- **Inbound（Connect → Gateway）**：默认 NONE；填 `OIDC_DISCOVERY_URL` → CUSTOM_JWT。
- **Outbound（Gateway → Runtime）**：GATEWAY_IAM_ROLE（CLI 自动配置）。
- **Runtime → KB / DynamoDB**：`app/kbserver/kb-ddb-policy.json` 内联策略，通过
  `agentcore.json` 的 `additionalPolicies` 挂到 CDK 托管的 Runtime 执行角色上
  （`bedrock:Retrieve`、`bedrock:InvokeModel`、DynamoDB `connectkb-pretickets` CRUD）。
  策略里 KB 资源用了通配（KB id 部署时才生成）；如需最小权限，部署后把 `bedrock:Retrieve`
  的 Resource 收敛为具体 KB ARN。

## 文件说明

```
mcp-server-kb/
├── README.md               # 本文件
├── deploy.sh               # 一键部署（DDB + 文档桶 + agentcore deploy + ingestion）
├── cleanup.sh              # 清理（agentcore destroy + DDB + 文档桶）
├── AGENTS.md               # CLI 生成的 AI 助手上下文
├── agentcore/
│   ├── agentcore.json      # 项目配置（runtime envVars/additionalPolicies、gateway、KB）
│   ├── aws-targets.json    # 部署目标（account+region，deploy 时解析）
│   └── cdk/                # @aws/agentcore-cdk 基础设施（勿手改生成的 cdk-stack.ts）
└── app/kbserver/           # MCP Server 应用代码
    ├── main.py             # FastMCP + 4 个工具（entrypoint）
    ├── kb_search.py        # Retrieve + Strands 生成答案 + 置信度
    ├── kb-ddb-policy.json  # Runtime 执行角色内联 IAM（KB Retrieve + InvokeModel + DDB）
    ├── pyproject.toml/uv.lock  # 依赖（mcp/boto3/strands/pydantic）
    ├── Dockerfile          # ARM64 容器（CodeBuild 用）
    ├── sample-docs/        # 示例知识库文档（客户替换）
    └── skills/
        ├── kb_answer.md         # 容器内答案合成 SOP
        └── connect-agent-sop.md # 给客户粘贴到 Connect AI Agent 的编排 SOP
```
