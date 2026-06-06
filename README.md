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
| `verifyCustomer` | `smsToken` (口述手机号后 4 位), `userNumber` (LLM 从 customer_info 取) | 主核身：MCP 比对 `userNumber[-4:] == smsToken` 后签发短期 HMAC token 当作 `customerId` 返回 | 末 4 位本地比对，不一致 → `CUSTOMER_NOT_FOUND` |
| `verifyCustomerByPhoneAndName` | `phoneNumber`, `fullName` | Fallback 核身（流程 1 失败后才用） | stub：手机号末 4 位 `0000` 时 `CUSTOMER_NOT_FOUND` |
| `requestRepair` | `productCategory`, `productsubCategory`, `province`, `city`, `district`, `description`, `brand`, `customerId` (必填); `productModel`, `serialNumber` (可选) | 创建维修工单，返回 10 位工单号 | 必填字段在 Lambda 端校验；`customerId` token HMAC 验签 |
| `trackRepair` | `woNumber`, `customerId` | 查询工单状态 | woNumber 必须为 10 位数字；token 验签 |
| `cancelRepair` | `woNumber`, `customerId` | 取消工单 | 同上；幂等：已 cancelled / completed 返 `409` |
| `faqSearch` | `query` | FAQ 关键字检索 | — |

详细的 docstring / 错误码 / 归一化规范见 [`mcp-agent/README.md`](mcp-agent/README.md)。

## Backend API 端点

| 端点 | 功能 |
|------|------|
| `POST /repair/request` | 创建维修工单，返回 10 位工单号 |
| `POST /repair/track` | 查询工单状态 |
| `POST /repair/cancel` | 取消工单 |
| `POST /faq/simple` | FAQ 关键字检索（无需 Bedrock KB） |

## 鉴权

- **Inbound (Connect / Quick Desktop / Quick Web → Gateway)**: `NONE` (testing only — 见上方警告)
- **Outbound (Gateway → AgentCore Runtime)**: `GATEWAY_IAM_ROLE`
- **MCP Agent → Backend API**: API Key（注入到 Runtime 环境变量；生产建议改 Secrets Manager）

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
IDENTITY_TOKEN_SECRET=              # 留空 → 首次部署自动 openssl rand -hex 32 写回
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
├── cleanup.sh                 # 统一清理脚本（reverse order）
├── test-api.sh                # Backend API 9 项端到端测试
├── connect-api-customer.yaml  # CloudFormation 模板（包含 Lambda inline code）
├── connect-api-openapi.yaml   # OpenAPI 规范
└── mcp-agent/                 # MCP Server Agent 源码（被 deploy.sh 打包后送进 CodeBuild）
    ├── README.md              # MCP Server 详细文档（工具签名 / docstring / 错误码 / 归一化）
    ├── mcp_server.py          # FastMCP server (6 tools)
    ├── Dockerfile             # ARM64 容器
    ├── requirements.txt
    ├── buildspec.yml
    └── china_regions_pinyin.json
```
