# Connect Repair Service API

维修服务 Backend API + MCP Agent，通过 AgentCore Gateway 暴露给 Amazon Connect AI Agent / Quick Connect / Quick Web 调用。

方案架构图如下：

<img width="1046" height="417" alt="image" src="https://github.com/user-attachments/assets/cd0cc9c4-ef83-4de3-a27a-5a8a131f2356" />

## 架构

```
Amazon Connect AI Agent / Quick Desktop / Quick Web (us-east-1)
  → AgentCore Gateway (MCP, authorizerType=NONE — testing only)
    → MCP Server Target (AgentCore Runtime, ARM64 容器)  ← mcp-agent/
      → Backend API (API Gateway + Lambda + DynamoDB, us-east-1)
```

整套链路完全独立、自包含。本分支 (`quick-mcp`) 把原本的两个部署脚本合并成 **一条命令** —— 用户从 fresh clone 到把整套资源部署到自己的 AWS 账号，只需要：

```bash
cd midea
chmod +x deploy.sh cleanup.sh test-api.sh
./deploy.sh
```

> ⚠️ **Inbound Auth = NONE**：`./deploy.sh` 自动创建的 AgentCore Gateway 用的是 `authorizerType=NONE` —— 任何拿到 MCP URL 的人都能调你的工具。仅适用于内部测试 / Quick Desktop & Quick Web 演示。生产环境请把 `.env` 里的 `GATEWAY_ID` + `GATEWAY_SERVICE_ROLE` 指到一个已经配好 `CUSTOM_JWT` 或 `AWS_IAM` 的 Gateway。

## MCP Tools (通过 Gateway 暴露)

| Tool | 入参 | 功能 | 校验 |
|------|------|------|------|
| `requestRepair` | `productCategory`(机器人品类), `productsubCategory`(对应部件), `description`, `brand` (必填); `callerName`, `callerPhoneTail`, `productModel`, `serialNumber` (可选) | 创建机器人维修工单，返回 WO-YYYY-NNNN 工单号 | 品类+部件组合校验（INVALID_CATEGORY / INVALID_SUB_CATEGORY）；必填字段在 Lambda 端校验。`callerName` / `callerPhoneTail` 仅为向后兼容保留，**不做核身、不校验** |
| `trackRepair` | `woNumber` (必填); `callerName`, `callerPhoneTail` (可选) | 查询工单状态 | woNumber 必须为 WO-YYYY-NNNN（INVALID_WO_NUMBER）。**无身份/归属校验**，任意调用方可查任意工单；工单不存在 → `404` |
| `cancelRepair` | `woNumber` (必填); `callerName`, `callerPhoneTail` (可选) | 取消工单 | 同上；任意调用方可取消任意工单；工单不存在 → `404`；幂等：已 cancelled / completed 返 `409` |
| `updateRepair` | `woNumber` (必填); `description`, `priority`, `status` (至少传一个); `callerName`, `callerPhoneTail` (可选) | 修改工单的故障描述/优先级/状态 | 同上；任意调用方可修改任意工单；工单不存在 → `404`；`priority`∈`P0/P1/P2/P3`（P0 紧急/P1 高/P2 中/P3 低）；`status`∈`pending/scheduled/in_progress/completed`（取消走 cancelRepair）；NOTHING_TO_UPDATE / INVALID_PRIORITY / INVALID_STATUS；已 cancelled/completed 返 `409` |
| `faqSearch` | `query` | FAQ 关键字检索 | — |

> **身份模型**：身份核验已**完全停用**。没有核身工具、没有客户注册表、没有 token / 缓存，也不做工单归属校验。`requestRepair` / `trackRepair` / `cancelRepair` / `updateRepair` 仍接受 `callerName`（口述姓名）与 `callerPhoneTail`（手机号后 4 位）两个参数，但它们仅为向后兼容保留，**既不校验也不使用**，因此都是**可选**的。任意调用方都可以创建 / 查询 / 修改 / 取消**任意**工单；`404` 现在只表示「工单不存在」，绝不再表示「归属于他人」。

详细的 docstring / 错误码 / 归一化规范见 [`mcp-agent/README.md`](mcp-agent/README.md)。

## Backend API 端点

| 端点 | 功能 |
|------|------|
| `POST /repair/request` | 创建机器人维修工单，返回 WO-YYYY-NNNN 工单号 |
| `POST /repair/track` | 查询工单状态 |
| `POST /repair/cancel` | 取消工单 |
| `POST /repair/update` | 修改工单的故障描述/优先级/状态 |
| `POST /faq/simple` | FAQ 关键字检索（无需 Bedrock KB） |

## 鉴权

- **Inbound (Connect / Quick Desktop / Quick Web → Gateway)**: `NONE` (testing only — 见上方警告)
- **Outbound (Gateway → AgentCore Runtime)**: `GATEWAY_IAM_ROLE`
- **MCP Agent → Backend API**: API Key（注入到 Runtime 环境变量；生产建议改 Secrets Manager）

## 在 Amazon Quick 中直连 MCP server（OAuth / Keycloak）

除了 `Quick → Gateway(NONE)` 这条链路，还可以让 **Quick 直连一个带 Keycloak JWT 入站鉴权的独立 AgentCore Runtime**（不经过 Gateway）。这条路由由独立脚本 `deploy_runtime_oauth.py` 创建，与默认 `deploy.sh`（Gateway + SigV4 runtime）完全隔离、互不影响，可随时切换。

```bash
# .env 里配置（非密；client secret 绝不入库）：
#   OAUTH_DISCOVERY_URL=https://<keycloak>/realms/<realm>/.well-known/openid-configuration
#   OAUTH_ALLOWED_AUDIENCE=account          # 必须用 account（见下方踩坑）
#   OAUTH_CLIENT_ID=<quick 用的 client id>  # 仅展示用
./.venv/bin/python deploy_runtime_oauth.py   # 或 venv/bin/python，复用现有 ECR 镜像
```

脚本跑完会打印 Quick 需要填写的 **MCP server URL**。在 Quick → **Connectors / Integrations → Model Context Protocol** 里新建，选 **Custom user based OAuth**（即 User authentication / 3LO），填写：

```
MCP server URL    = <脚本输出的 runtime invocations URL，含 ?qualifier=DEFAULT>
Auth type         = OAuth 2.0 (User authentication / 3LO)
Client id         = <你的 OAuth client id>
Client Secret     = <你持有的 secret —— 不写入仓库任何文件>
Token URL         = https://<keycloak>/realms/<realm>/protocol/openid-connect/token
Authorization URL = https://<keycloak>/realms/<realm>/protocol/openid-connect/auth
Redirect URL      = https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback
```

> Keycloak client 需要：开启 Standard flow（授权码）、PKCE=S256、把上面的 Redirect URL 加进 **Valid Redirect URIs**。

### ⚠️ 踩坑记录（Quick 连 Runtime 时 "Creation failed" 的两个根因）

Quick 的 OAuth 流程会成功（Keycloak 日志可见 LOGIN + CODE_TO_TOKEN 无错），但 connector 在 **publish 阶段** 仍报 `Creation failed`。逐字段对照一个能成功的 runtime 后定位到两个**必须满足**的条件：

1. **runtime 的 JWT authorizer 必须用 `allowedAudience: ["account"]`**，**不能**用 `allowedClients`。
   Quick 拿到的 token `aud=["<client_id>","account"]`；用 `allowedClients` 校验会导致 publish 失败，用共享的 `account` audience 才能过。`deploy_runtime_oauth.py` 已默认 `allowedAudience=account`。

2. **每个 MCP 工具必须暴露 `outputSchema`**。
   工具若用 `@mcp.tool(structured_output=False)` 就不会生成 `outputSchema`，Quick 注册 action 时校验失败。改回 `@mcp.tool()`（FastMCP 自动按返回类型生成 outputSchema）即可。本仓库所有工具已全部改回。

   > 副作用：去掉 `structured_output=False` 后，工具返回体除 `content[]` 外还会带 `structuredContent`。对老的 Connect/Gateway 路径无影响（只是多一个字段）。

诊断时还排除了若干**非根因**：OAuth 本身、JWT 验签、MCP `initialize`/`tools/list`（均 HTTP 200）、`inputSchema` 合法性（合法 Draft-7）、description 长度（压缩后仍失败）。真正卡点就是上面两条。

## 部署

> 推荐在 **AWS CloudShell** 中执行：脚本会自动通过 `aws sts get-caller-identity` 解析当前账号 ID,并在 boto3 schema 不够新时(CloudShell 自带 1.42.x)自动创建 venv 升级到 `boto3>=1.43`。

```bash
cd midea
chmod +x deploy.sh cleanup.sh test-api.sh
cp .env.example .env       # 编辑 .env, 至少改 REGION (默认 us-east-1)
./deploy.sh
```

`./deploy.sh` 一次性完成 12 步：

| 步骤 | 内容 |
|------|------|
| 0 | 解析 Python 解释器 (确保 boto3 schema 包含 `iamCredentialProvider`) |
| 1 | 创建/复用 API CFN bucket |
| 2 | 上传 `connect-api-openapi.yaml` + `connect-api-customer.yaml` |
| 3 | `cloudformation create-stack \| update-stack`，等待就绪 |
| 4 | 读取 `ApiUrl` / `ApiKey` 输出，写回 `.env` |
| 5 | 创建/复用 ECR repo |
| 6 | 打包 `mcp-agent/` 源码上传 S3 |
| 7 | 创建 CodeBuild 项目 + IAM 角色 |
| 8 | CodeBuild 构建 ARM64 镜像并推送 |
| 9 | 创建/更新 Runtime 执行角色（含 `bedrock:InvokeModel`、X-Ray、CloudWatch 权限） |
| 10 | 创建/更新 AgentCore Runtime |
| 11 | **自动创建 Gateway (`authorizerType=NONE`) ** + 给 Gateway service role 加 `InvokeAgentRuntime` |
| 12 | 创建/同步 Gateway mcpServer target |

部署完成后所有信息会写到 `deployment-info.log`，包括 Gateway URL（直接拿去贴到 Amazon Quick Desktop / Quick Web 的 MCP 配置里）、MCP Endpoint、Runtime ARN 等。

### 配置 (`.env`)

`./deploy.sh` 第一次运行时如果没有 `.env` 会自动从 `.env.example` 复制。99% 的字段都不用改：

```
REGION=us-east-1                    # 改成你目标的 region
ACCOUNT_ID=                         # 留空，从 STS 自动解析
STACK_NAME=connect-repair-api-stack # 默认即可
AGENT_NAME=connect-repair-mcp-server
ECR_REPO_NAME=connect-repair-mcp-server
TARGET_NAME=connect-repair-mcp-agent
GATEWAY_ID=                         # 留空 → 自动创建一个 NONE auth Gateway
GATEWAY_SERVICE_ROLE=
REPAIR_API_URL=                     # 由 deploy.sh 自动填
REPAIR_API_KEY=                     # 由 deploy.sh 自动填
```

### Quick Desktop / Quick Web 接入

`deploy.sh` 完成后看 `deployment-info.log` 拿到的 **Gateway URL** 就是 Quick Connect / Quick Web 需要填的 MCP URL，直接贴进去就能调用工具，无需任何 OIDC/JWT 配置（因为 inbound auth 是 `NONE`）。

### 测试 Backend API

```bash
export API_URL="..."   # 见 deployment-info.log
export API_KEY="..."
./test-api.sh
```

测试覆盖：创建工单 → 查询 → FAQ × 2 → 缺字段 400 → 工单不存在 404 → 非法 woNumber 400 → 取消工单 → 重复取消 409。

### 重新部署（代码改了之后）

```bash
./deploy.sh    # 幂等：CFN 走 update-stack（无变化时跳过），Runtime 用 update_agent_runtime
```

## 清理

```bash
./cleanup.sh
```

按 deploy 的反向顺序拆资源：Gateway target → Gateway IAM 内联策略 → Runtime → CodeBuild → ECR → MCP source bucket → 自动创建的 Gateway + 角色 → IAM 角色 → CFN stack → API CFN bucket。脚本会自动判断 Gateway 是否是 deploy.sh 自建的；自建则一并删除，用户提供的 `GATEWAY_ID` 不会被动。

## 文件说明

```
midea/
├── README.md                  # 本文件
├── .env.example               # 配置模板（fresh clone 复制为 .env）
├── deploy.sh                  # 统一部署脚本（API + MCP Agent + Gateway 一条命令）
├── deploy_runtime.py          # Steps 10-12 (Runtime + Gateway + Target，被 deploy.sh 调用)
├── deploy_runtime_oauth.py    # 独立脚本：建带 Keycloak JWT 鉴权的第 2 个 Runtime，供 Quick 直连（见“在 Amazon Quick 中直连 MCP server”）
├── cleanup.sh                 # 统一清理脚本（reverse order）
├── test-api.sh                # Backend API 端到端测试
├── connect-api-customer.yaml  # CloudFormation 模板（含 Lambda inline code + 10 张预置工单 seed）
├── connect-api-openapi.yaml   # OpenAPI 规范
└── mcp-agent/                 # MCP Server Agent 源码（被 deploy.sh 打包后送进 CodeBuild）
    ├── README.md              # MCP Server 详细文档（工具签名 / docstring / 错误码 / 归一化）
    ├── mcp_server.py          # FastMCP server (5 tools)
    ├── Dockerfile             # ARM64 容器
    ├── requirements.txt
    └── buildspec.yml
```
