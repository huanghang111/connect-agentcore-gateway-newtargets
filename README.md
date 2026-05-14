# Connect Repair Service API

维修服务 Backend API + MCP Agent，通过 AgentCore Gateway 提供给 Connect AI Agent 调用。

方案架构图如下：

<img width="1046" height="417" alt="image" src="https://github.com/user-attachments/assets/cd0cc9c4-ef83-4de3-a27a-5a8a131f2356" />

## 架构

```
Connect AI Agent (us-east-1)
  → AgentCore Gateway (CUSTOM_JWT, MCP protocol)
    → [NEW] MCP Server Target (AgentCore Runtime)  ← mcp-agent/
      → Backend API (API Gateway + Lambda + DynamoDB, us-east-1)
    → [OLD] OpenAPI Schema Target (可移除)
```

## 部署 MCP Agent (新增)

```bash
cd midea/mcp-agent
cp .env.example .env     # 填写账号、Gateway、后端 API 等配置
chmod +x deploy.sh cleanup.sh
./deploy.sh
```

部署完成后会自动:
1. 构建 ARM64 Docker 镜像推送 ECR
2. 创建 AgentCore Runtime (MCP protocol, CUSTOM_JWT)
3. 在现有 Gateway 中添加 mcpServer target

之后在 Connect 中手动修改 AI Agent 配置即可（详见 `mcp-agent/README.md`）。

**重新部署（代码更新后）:**
```bash
cd midea/mcp-agent
./deploy.sh
```
脚本幂等，已存在的资源会更新而不是重建。

## MCP Tools (通过Gateway暴露)

| Tool | 功能 |
|------|------|
| requestRepair | 创建维修工单 |
| trackRepair | 查询工单状态 |
| faqSearch | FAQ知识库搜索 |

## 鉴权

- **Inbound (Connect → Gateway)**: CUSTOM_JWT (Connect OIDC)
- **Outbound (Gateway → MCP Agent Runtime)**: GATEWAY_IAM_ROLE
- **MCP Agent → Backend API**: API Key (环境变量)

## Backend API 端点

| 端点 | 功能 |
|------|------|
| POST /repair/request | 创建维修工单，返回10位工单号 |
| POST /repair/track | 查询工单状态 |
| POST /faq/simple | FAQ查询 |

## 文件说明

```
midea/
├── deploy.sh                  # Backend API 部署脚本
├── cleanup.sh                 # Backend API 清理脚本
├── test-api.sh                # API 测试脚本
├── connect-api-customer.yaml  # CloudFormation模板
├── connect-api-openapi.yaml   # OpenAPI规范
└── mcp-agent/                 # [NEW] MCP Server Agent
    ├── .env.example           # 配置模板
    ├── mcp_server.py          # FastMCP server (3 tools)
    ├── Dockerfile             # ARM64容器
    ├── requirements.txt       # Python依赖
    ├── deploy.sh              # Agent部署脚本 (含Gateway target)
    ├── cleanup.sh             # Agent清理脚本
    └── README.md              # Agent文档
```

## 清理 MCP Agent

```bash
cd midea/mcp-agent
./cleanup.sh
```
