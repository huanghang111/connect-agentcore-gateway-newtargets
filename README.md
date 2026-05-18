# Connect Repair Service API

维修服务 Backend API + MCP Agent，通过 AgentCore Gateway 提供给 Amazon Connect AI Agent 调用。

方案架构图如下：

<img width="1046" height="417" alt="image" src="https://github.com/user-attachments/assets/cd0cc9c4-ef83-4de3-a27a-5a8a131f2356" />

## 架构

```
Connect AI Agent (us-east-1)
  → AgentCore Gateway (MCP protocol, CUSTOM_JWT)
    → MCP Server Target (AgentCore Runtime, ARM64 容器)  ← mcp-agent/
      → Backend API (API Gateway + Lambda + DynamoDB, us-east-1)
```

整套链路完全独立、自包含，从零开始 fresh deploy 时只需要按 **第 1 步 → 第 2 步** 顺序跑两套脚本。

## MCP Tools (通过 Gateway 暴露)

| Tool | 入参 | 功能 | 校验 |
|------|------|------|------|
| `requestRepair` | `productCategory`, `productsubCategory`, `province`, `city`, `district`, `description`, `brand` (必填); `productModel`, `serialNumber` (可选) | 创建维修工单，返回 10 位工单号 | 必填字段在 Lambda 端校验 |
| `trackRepair` | `woNumber` | 查询工单状态 | woNumber 必须为 10 位数字（前后端双重校验） |
| `cancelRepair` | `woNumber` | 取消工单 | 同上；幂等：已 cancelled / completed 返 `409` |

> `requestRepair` 的 docstring 中标注了每个字段的 *PRECONDITIONS*（产品大/小类、地址、型号 SN 应由上游接口预校验）；MCP 客户端在调用前就应该满足这些约束。

## Backend API 端点

| 端点 | 功能 |
|------|------|
| `POST /repair/request` | 创建维修工单，返回 10 位工单号 |
| `POST /repair/track` | 查询工单状态 |
| `POST /repair/cancel` | 取消工单 |
| `POST /faq/simple` | FAQ 关键字检索（无需 Bedrock KB） |

## 鉴权

- **Inbound (Connect → Gateway)**: CUSTOM_JWT (Connect 实例 OIDC)
- **Outbound (Gateway → AgentCore Runtime)**: GATEWAY_IAM_ROLE
- **MCP Agent → Backend API**: API Key（注入到 Runtime 环境变量；生产建议改 Secrets Manager）

## Fresh Deployment

### 第 1 步：部署 Backend API

```bash
cd midea
chmod +x deploy.sh cleanup.sh test-api.sh
./deploy.sh
```

默认部署到 `us-east-1`、stack 名 `connect-repair-api-stack`、bucket 名 `connect-repair-api-<account>-us-east-1`。可用环境变量覆盖：

```bash
STACK_NAME=my-stack REGION=us-west-2 ./deploy.sh
```

脚本依次执行：
1. 创建 / 复用 S3 bucket
2. 上传 `connect-api-openapi.yaml` + `connect-api-customer.yaml`（模板 > 51200 字节，必须走 S3）
3. `aws cloudformation create-stack | update-stack`（自动判断走哪个，幂等）
4. 等待 stack 就绪并把 `API URL / API Key / DynamoDB 表名` 写入 `deployment-info.log`

跑测试验证三类用例：

```bash
export API_URL="..."   # 见 deployment-info.log
export API_KEY="..."
./test-api.sh
```

测试覆盖：创建工单 → 查询 → FAQ × 2 → 缺字段 400 → 工单不存在 404 → 非法 woNumber 400 → 取消工单 → 重复取消 409。

### 第 2 步：部署 MCP Agent

```bash
cd midea/mcp-agent
cp .env.example .env
# 填入: REGION, ACCOUNT_ID, REPAIR_API_URL, REPAIR_API_KEY
# 留空: GATEWAY_ID（让脚本自动创建一个 AgentCore Gateway）
# 必填(自动创建场景): GATEWAY_JWT_DISCOVERY_URL（Connect 实例 OIDC discovery URL）
chmod +x deploy.sh cleanup.sh
./deploy.sh
```

脚本依次：
1. 建 ECR repo + S3 上传源码
2. CodeBuild 构建 ARM64 镜像并推送到 ECR
3. 创建 Runtime 执行角色
4. 创建 / 更新 AgentCore Runtime
5. **(自动)** 如果 `GATEWAY_ID` 为空，自动创建一个新的 Gateway（CUSTOM_JWT，audience = Gateway 自身 ID）
6. 给 Gateway service role 加 `InvokeAgentRuntime`
7. 创建 / 同步 Gateway mcpServer target

部署完成后在 `deployment-info.log` 拿到 Gateway ID + Target Name，去 Connect 控制台把这三个工具挂到 AI Agent。详细步骤见 `mcp-agent/README.md`。

### 重新部署（代码改了之后）

两个脚本都幂等，再跑一次即可。Backend API 用 `update-stack`，没变化时跳过；MCP Agent 用 `update_agent_runtime` + `synchronize_gateway_targets`。

## 清理

```bash
cd midea/mcp-agent && ./cleanup.sh   # 先删 Runtime / Target / 自建 Gateway / IAM 角色
cd midea && ./cleanup.sh             # 再删 Backend API stack
```

`midea/mcp-agent/cleanup.sh` 会自动识别 Gateway 是否是脚本自建的；自建则一并删除 Gateway 与 `<AGENT_NAME>-gateway-role`，用户提供的 Gateway 不会被动。

## 文件说明

```
midea/
├── README.md                  # 本文件（顶层架构 + fresh-deploy 流程）
├── deploy.sh                  # Backend API 部署脚本（CloudFormation create/update）
├── cleanup.sh                 # Backend API 清理脚本
├── test-api.sh                # API 9 项端到端测试
├── connect-api-customer.yaml  # CloudFormation 模板（包含 Lambda inline code）
├── connect-api-openapi.yaml   # OpenAPI 规范（独立可读版）
├── deployment-info.log        # 部署后生成，含 API URL/Key
└── mcp-agent/                 # MCP Server Agent
    ├── README.md              # Agent 详细文档
    ├── .env.example           # 配置模板
    ├── mcp_server.py          # FastMCP server (3 tools)
    ├── Dockerfile             # ARM64 容器
    ├── requirements.txt       # Python 依赖
    ├── buildspec.yml          # CodeBuild 构建脚本
    ├── deploy.sh              # Agent 部署 (含 Gateway 自动创建)
    ├── deploy_runtime.py      # Steps 6-8: Runtime + Gateway + Target
    ├── cleanup.sh             # Agent 清理（自动识别 auto-created Gateway）
    └── deployment-info.log    # 部署后生成
```
