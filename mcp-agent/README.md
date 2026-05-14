# Midea Repair MCP Server

把三个 Repair Service API 封装成 MCP Server，部署到 AgentCore Runtime，通过 AgentCore Gateway 暴露给 Amazon Connect AI Agent。

## 架构

```
Connect AI Agent → AgentCore Gateway (mcpServer target) → AgentCore Runtime (Container) → Backend API
```

## 工具

| Tool | 功能 |
|------|------|
| `requestRepair` | 创建维修工单 |
| `trackRepair` | 查询工单状态 |
| `faqSearch` | FAQ 搜索 |

## 前置条件

- AWS CLI 已配置
- Python 3.9+（用于创建本地 venv 跑 boto3）
- `zip` 命令
- 一个已存在的 AgentCore **Gateway**
- 后端 Repair API 已部署（提供 `/repair/request`、`/repair/track`、`/faq/simple`）

> 不需要本地装 Docker，镜像构建在 CodeBuild 上完成。

## 配置

把 `.env.example` 复制为 `.env`，填写你的配置：

```bash
cp .env.example .env
```

`.env` 已在 `.gitignore` 中，不会被提交到 Git。

必填项：

| 变量 | 说明 |
|------|------|
| `REGION` | AWS Region，如 `us-east-1` |
| `ACCOUNT_ID` | AWS 账号 ID |
| `AGENT_NAME` | Runtime / 资源名（用短横线，如 `midea-repair-mcp-server`） |
| `ECR_REPO_NAME` | ECR 仓库名 |
| `TARGET_NAME` | Gateway target 名 |
| `GATEWAY_ID` | 已存在的 AgentCore Gateway ID |
| `GATEWAY_SERVICE_ROLE` | Gateway 的 IAM Role **名称**（不是 ARN） |
| `REPAIR_API_URL` | 后端 API 地址 |
| `REPAIR_API_KEY` | 后端 API Key |

查找 Gateway Service Role 名称：

```bash
aws bedrock-agentcore-control get-gateway \
  --gateway-identifier <GATEWAY_ID> \
  --region us-east-1 \
  --query "roleArn" --output text
# Role 名称是 ARN 的最后一段
```

## 部署

```bash
cd mcp-agent
chmod +x deploy.sh
./deploy.sh
```

耗时约 5-10 分钟，脚本依次执行：

| 步骤 | 内容 |
|-----|------|
| 1 | 创建 ECR 仓库 |
| 2 | 打包源码上传 S3 |
| 3 | 创建 CodeBuild 项目和 IAM 角色 |
| 4 | CodeBuild 构建 ARM64 镜像推送到 ECR |
| 5 | 创建 Runtime 执行角色 |
| 6 | 创建/更新 AgentCore Runtime |
| 7 | 给 Gateway Service Role 加 `InvokeAgentRuntime` 权限 |
| 8 | 创建/更新 Gateway 的 mcpServer target |

成功后生成 `deployment-info.log`，包含 Runtime ID、ARN、MCP Endpoint 等信息。

## 验证

```bash
# Runtime 状态应为 READY
aws bedrock-agentcore-control get-agent-runtime \
  --agent-runtime-id <runtime-id> \
  --region us-east-1 --query "status"

# 查看 Runtime 日志
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region us-east-1 --follow
```

日志里看到 `Processing request of type PingRequest` 即表示 Gateway 健康检查正常。

## 在 Connect 中配置

Gateway target 就绪后，去 Amazon Connect 控制台：

1. **AI Agent Designer** → 选择 AI Agent → **Add tool** → **Add existing AI Tool**
2. Namespace 选 `gateway_<gateway-name>`
3. AI Tool 分别选这三个（重复三次）：
   - `midea-repair-mcp-agent___requestRepair`
   - `midea-repair-mcp-agent___trackRepair`
   - `midea-repair-mcp-agent___faqSearch`
4. **Output Filters** → Select Property Keys 里加 `result`（必须勾选，否则 LLM 读不到返回值）
5. 点 **Update** 保存

## 清理

```bash
./cleanup.sh
```

删除本次部署创建的所有资源（Gateway target、Runtime、ECR、S3、CodeBuild、IAM 角色）。Gateway 本身不会被删。

## 本地测试（可选）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export REPAIR_API_URL="https://xxx/<stage>"
export REPAIR_API_KEY="xxx"
python mcp_server.py
# 本地 MCP server 监听 http://localhost:8000/mcp
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `.env.example` | 配置模板（复制为 `.env` 后填写） |
| `mcp_server.py` | MCP Server 实现（FastMCP + 3 个 tool） |
| `Dockerfile` | ARM64 容器（Python 3.11，非 root 用户） |
| `requirements.txt` | Python 依赖 |
| `buildspec.yml` | CodeBuild 构建脚本 |
| `deploy.sh` | 主部署脚本（Step 1-5 shell，Step 6-8 调 Python） |
| `deploy_runtime.py` | Step 6-8：Runtime、IAM、Gateway target |
| `cleanup.sh` | 清理脚本 |

## 注意事项

- Runtime 必须用 **ARM64** 镜像，CodeBuild 已配成 ARM 环境
- Dockerfile 遵循官方示例：Python 3.11 基础镜像、非 root 用户 `bedrock_agentcore`、启动命令 `python -m mcp_server`
- Tool 返回 `json.dumps(dict)`（字符串），对应 outputSchema `{result: string}`。Connect Output Filter 必须勾选 `result`，否则 LLM 拿不到返回值
- API Key 通过环境变量注入到 Runtime（生产环境建议改用 Secrets Manager）
- `deploy.sh` 幂等，重复运行会更新 Runtime 并重新同步 Gateway target
