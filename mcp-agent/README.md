# Connect Repair MCP Server

把 Repair Service 工具封装成 MCP Server，部署到 AgentCore Runtime，通过 AgentCore Gateway 暴露给 Amazon Connect AI Agent。包含两个轻量身份核验工具（`verifyCustomer` + fallback `verifyCustomerByPhoneAndName`）和三个 repair 业务工具。

## 架构

```
Connect AI Agent → AgentCore Gateway (mcpServer target) → AgentCore Runtime (Container) → Backend API
```

## 工具

| Tool | 功能 | 入参（前置校验要求见 docstring） |
|------|------|----------|
| `verifyCustomer` | 主核身：用手机号后 4 位（`smsToken`）查 `customerId`（目前 stub：`smsToken == "0000"` 时返回 `CUSTOMER_NOT_FOUND`，其他 4 位数字返回 `"0000" + 后 4 位`） | `smsToken`(4 位数字) 必填 |
| `verifyCustomerByPhoneAndName` | Fallback 核身：用**完整手机号 + 姓名**查 `customerId`（仅在 `verifyCustomer` 返回 `CUSTOMER_NOT_FOUND` 后调用；目前 stub：手机号末 4 位为 `0000` 时返回 `CUSTOMER_NOT_FOUND`，其他返回 `"PHN" + 末 4 位`） | `phoneNumber`(纯数字 6–15 位)、`fullName` 必填 |
| `requestRepair` | 创建维修工单 | `productCategory`, `productsubCategory`, `province`, `city`, `district`, `description`, `brand`, `customerId` 必填；`productModel`, `serialNumber` 可选 |
| `trackRepair` | 查询工单状态 | `woNumber`(10 位数字)、`customerId`，工具内强制校验 |
| `cancelRepair` | 取消工单 | `woNumber`(10 位数字)、`customerId`，工具内强制校验 |

> **设计原则**：所有 tool 的使用方式都写在各自的 docstring 顶部，LLM 通过 `toolConfigurationList` 拿到 description 即可正确使用，**Connect AI Agent 的 Orchestration Prompt 不要写任何 tool 用法**。这样后续迭代只需更新 mcp-server,不动 Connect 配置。
>
> **身份核验流程**（SMS 发送 API 上线前的临时方案）：
> 1. Connect 上下文若已带 `customerId`，repair tool 直接传该值，跳过核身。
> 2. 没有 `customerId` 时，先调一次 `verifyCustomer(smsToken=电话号码后 4 位)` —— `smsToken` 必须严格 4 位数字，docstring 已明确告诉 LLM "不要发短信、只问后 4 位"。
> 3. **核身命中**（`verifyCustomer` 返回 `{"customerId": "0000XXXX"}`）：Connect AI Agent **保存**该 `customerId` 到对话上下文，后续整段对话的 repair tool 都用它。
> 4. **核身未命中**（`verifyCustomer` 返回 `{"error": "CUSTOMER_NOT_FOUND"}`）：agent 提示客户"该手机号查不到客户信息，请提供另一个完整手机号 + 姓名"，然后调 `verifyCustomerByPhoneAndName(phoneNumber, fullName)`。该工具命中后返回 `{"customerId": "PHNXXXX"}`，仍走第 3 步保存逻辑；如再次返回 `CUSTOMER_NOT_FOUND`，agent 不再循环，转人工。
> 5. `customerId` 为空时 repair tool 返回 `{"error": "MISSING_CUSTOMER_ID"}`，agent 必须先跑核身工具再重试，不要用空值反复重试。
>
> **Stub 测试触发器**：在真实身份 API 接通前，stub 通过两个"幻数"模拟核身失败 —— `smsToken == "0000"` 让 `verifyCustomer` 返回 `CUSTOMER_NOT_FOUND`；`phoneNumber` 末 4 位为 `0000` 让 `verifyCustomerByPhoneAndName` 也返回 `CUSTOMER_NOT_FOUND`。这样 Connect 端可以端到端演练 fallback 分支。

每个 tool 的 docstring 顶部都列出了 **PRECONDITIONS**，明确字段在调用前必须经过哪些上游校验接口（产品大/小类、地址映射、型号/SN）。

### 服务端校验（`requestRepair`）

除了 `customerId` / 工单号格式校验之外，`requestRepair` 在打到后端之前会再做两层本地校验，校验失败立刻返回错误、不发出网络请求：

| 字段 | 规则 | 失败时返回 |
|------|------|------------|
| `productsubCategory` | 必须是 `smart version` / `premium version` / `elite version` 三个枚举值之一（大小写、内部空格不敏感） | `{"error": "INVALID_SUB_CATEGORY", "allowed": [...]}` |
| `province` / `city` / `district` | 用 `china_regions_pinyin.json` 校验，接受**中文**或**拼音**（带不带行政后缀都行）；三者必须层级一致。**直辖市特例**：北京/上海/天津/重庆没有真正的 city 层，允许 `city == province`（如 `province="Beijing", city="Beijing", district="Chaoyang"`）；docstring 已显式提示 LLM 不要追问 city | `{"error": "INVALID_PROVINCE" \| "INVALID_CITY" \| "INVALID_DISTRICT"}` |

数据来源：[modood/Administrative-divisions-of-China](https://github.com/modood/Administrative-divisions-of-China)（WTFPL）。当行政区划数据需要刷新时跑一次 `gen_regions.py` 重新生成 `china_regions_pinyin.json`（依赖 `pypinyin`，仅在生成时需要，运行时不依赖）。

### 响应归一化（`requestRepair` / `trackRepair` / `cancelRepair`）

不同 BU 的后端 API 返回字段名经常不一致 —— 同一个"工单状态"字段可能叫 `status`、`ticketstatus`、`tstatus`、`statusName`，工单号可能叫 `woNumber`、`wono`、`ticketId`、`orderNumber`，让上层 LLM 难以稳定播报。MCP server 在三个写/查工具拿到后端响应后，会通过 **Strands Agents SDK** 调用 **Bedrock**（默认 `us.anthropic.claude-haiku-4-5-20251001-v1:0`），用 `agent.structured_output(PydanticModel, prompt)` 把原始 JSON 强制映射到固定 Pydantic schema 上 —— Strands 走 Bedrock tool-use 通道，模型几乎不会吐出格式非法的 JSON。

| 工具 | 规范字段（来自 `mcp_server.py` 中的 Pydantic 模型） |
|------|-----------------------------------------------------|
| `requestRepair` | `RequestResponse`：`woNumber`, `created`(bool), `status`, `scheduledAt`, `message` |
| `trackRepair` | `TrackResponse`：`woNumber`, `status`, `statusDescription`, `scheduledAt`, `technicianName`, `technicianPhone`, `address`, `lastUpdatedAt`, `remarks` |
| `cancelRepair` | `CancelResponse`：`woNumber`, `cancelled`(bool), `status`, `message` |

> ⚠️ `requestRepair` 是已知的延迟敏感路径（参见 `hzh.md` 客户反馈第 10 条 / 2026-05-29 反馈第二条），归一化每次约 +0.5–1s。如果实测延迟拖累播报体验，可以单独把 `requestRepair` 的 `_normalize_with_llm(...)` 一行注释掉，或全局 `NORMALIZE_RESPONSE=0` 关闭。

每个工具的 docstring `RETURNS` 段就是 LLM 拿到的最终 schema —— 修改 Pydantic 模型 + docstring 即可调整播报字段。

设计要点：

- **Fail-soft**：归一化超时 / 限流 / Pydantic 校验失败时返回**原始**响应，不让 Bedrock 故障拖垮工单查询。日志关键字 `normalize ok` / `normalize failed`。
- **错误透传**：原始响应里出现 `error` 键直接跳过归一化，错误路径保持确定。
- **成本/延迟**：每次查询多一次 Haiku 调用（典型 ~0.5–1s）。可以通过 `NORMALIZE_RESPONSE=0` 关掉。
- **单例 Agent**：`_normalize_agent` 懒加载并缓存，避免每次调用都重新构造 Bedrock client。

环境变量（写到 `.env`，部署时通过 Runtime 注入）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `NORMALIZE_RESPONSE` | `1` | 设 `0`/`false`/`off` 关闭归一化，工具直接返回后端原始 JSON |
| `NORMALIZE_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock inference profile / model ID |
| `BEDROCK_REGION` | 同 `AWS_REGION`（默认 `us-east-1`） | 哪个 region 调 Bedrock |
| `NORMALIZE_TIMEOUT_S` | `4` | 单次归一化的 read timeout（秒），超时 fail-soft 退回原始响应 |

部署脚本已经给 Runtime execution role 加了 `bedrock:InvokeModel` 权限（针对 foundation-model + inference-profile 资源），所以重新跑一次 `./deploy.sh` 即可生效；如要换模型，改 `.env` 里的 `NORMALIZE_MODEL_ID` 再重新部署即可。

### 可观测性（OTEL → CloudWatch GenAI Observability）

MCP server 通过 **AWS Distro for OpenTelemetry (ADOT)** 把 trace / metric / log 送到 AgentCore 托管的 OTLP 端点；CloudWatch GenAI Observability 控制台直接可视化（含 Strands 的 LLM 调用、Bedrock 子调用、tokens / latency）。

实现要点：

- `requirements.txt` 新增 `aws-opentelemetry-distro>=0.10.0` —— 提供 `opentelemetry-instrument` 入口。
- `Dockerfile` 的 CMD 由 `python -m mcp_server` 改为 `opentelemetry-instrument python -m mcp_server`，自动加载 ADOT 配置。
- `deploy_runtime.py` 给 Runtime 注入 `OTEL_SERVICE_NAME=${AGENT_NAME}`，作为 CloudWatch 控制台里这个 service 的标签；其他 `OTEL_EXPORTER_OTLP_*` 由 AgentCore Runtime 自动注入，无须配置。
- `deploy.sh` 给 Runtime execution role 加了 `xray:PutTraceSegments`、`xray:PutSpans`、`logs:PutLogEvents`、`cloudwatch:PutMetricData` 等 ADOT 写权限（除 `BedrockAgentCoreFullAccess` 自带的之外的兜底）。
- Strands 框架对 OTEL 有内建支持，无需在代码里调用 `StrandsTelemetry()` —— 一旦容器入口是 `opentelemetry-instrument`、ADOT 装好，trace 自动产生。

**一次性 region 设置（部署前做一次即可）**：

1. **打开 CloudWatch Transaction Search**：CloudWatch 控制台 → Application Signals (APM) → **Transaction search** → **Enable Transaction Search**，勾选 *ingest spans as structured logs* 后保存。
2. **在 Runtime 上启用 Tracing**：`./deploy.sh` 创建/更新 Runtime 后，去 [Agent Runtime 控制台](https://console.aws.amazon.com/bedrock-agentcore/agents) → 选中本 Runtime → **Tracing** 区块 → **Edit** → toggle *Enable* → Save。Spans 之后会出现在 `aws/spans` log group。

**查看 trace**：CloudWatch 控制台 → **GenAI Observability**（左侧导航） → 按 service.name=`connect-repair-mcp-server` 过滤即可看到 Strands 调用 Bedrock 的耗时分布。

**临时关闭 ADOT**（调试需要）：在 Runtime 上设置 `DISABLE_ADOT_OBSERVABILITY=true` 重新部署即可，无需改代码。

## 前置条件

- AWS CLI 已配置
- Python 3.9+（用于创建本地 venv 跑 boto3）
- `zip` 命令
- 后端 Repair API 已部署（提供 `/repair/request`、`/repair/track`、`/repair/cancel`）

> 不需要本地装 Docker，镜像构建在 CodeBuild 上完成。
> AgentCore Gateway **可选** —— 留空 `GATEWAY_ID` 时部署脚本会自动创建一个 (CUSTOM_JWT)。

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
| `AGENT_NAME` | Runtime / 资源名（用短横线，如 `connect-repair-mcp-server`） |
| `ECR_REPO_NAME` | ECR 仓库名 |
| `TARGET_NAME` | Gateway target 名 |
| `REPAIR_API_URL` | 后端 API 地址 |
| `REPAIR_API_KEY` | 后端 API Key |

Gateway 相关（任选其一种模式）：

| 变量 | 说明 |
|------|------|
| `GATEWAY_ID` | **复用现有 Gateway**：填入 ID。同时必须填 `GATEWAY_SERVICE_ROLE`（角色名，非 ARN） |
| `GATEWAY_SERVICE_ROLE` | 现有 Gateway 的 IAM Role **名称** |
| `GATEWAY_JWT_DISCOVERY_URL` | **自动创建 Gateway**：`GATEWAY_ID` 留空时必填，填 IDP（如 Connect 实例）的 OIDC discovery URL |
| `GATEWAY_JWT_ALLOWED_AUDIENCE` | 可选；自动创建场景下脚本会**自动**把新 Gateway 的 ID 加到 audience 列表里，这里填的值仅作叠加 |
| `GATEWAY_JWT_ALLOWED_CLIENTS` | 可选；JWT 允许的 client ID 列表（逗号分隔） |

自动创建模式下，脚本会:
1. 创建 `${AGENT_NAME}-gw` Gateway：`protocolType=MCP`，`authorizerType=CUSTOM_JWT`
2. **关键**：`allowedAudience` 设置成 Gateway 自己的 ID。Connect 颁发给该 namespace 的 JWT 中 `aud` claim 等于 Gateway ID，audience 不一致会触发 `insufficient_scope` 错误
3. 创建并复用 IAM 角色 `${AGENT_NAME}-gateway-role`（带 `InvokeAgentRuntime` + 日志权限）

复用模式下查找现有 Gateway Service Role 名称：

```bash
.venv/bin/python -c "
import boto3
g = boto3.client('bedrock-agentcore-control', region_name='us-east-1') \
        .get_gateway(gatewayIdentifier='<GATEWAY_ID>')
print(g['roleArn'].split('/')[-1])"
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
| 6.5 | **(新)** Gateway 不存在则自动创建（含 IAM 角色） |
| 7 | 给 Gateway Service Role 加 `InvokeAgentRuntime` 权限 |
| 8 | 创建/更新 Gateway 的 mcpServer target |

成功后生成 `deployment-info.log`，包含 Runtime ID、ARN、MCP Endpoint 等信息。

## 验证

```bash
# Runtime 状态应为 READY（CLI 没有 bedrock-agentcore-control 子命令时改用 boto3）
.venv/bin/python -c "import boto3; c = boto3.client('bedrock-agentcore-control', region_name='us-east-1'); \
  print(c.get_agent_runtime(agentRuntimeId='<runtime-id>')['status'])"

# 查看 Runtime 日志
aws logs tail /aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT \
  --region us-east-1 --follow
```

日志里看到 `INFO mcp.server.lowlevel.server Processing request of type PingRequest` 即表示 Gateway 健康检查正常。

`mcp_server.py` 使用标准 `logging`（logger 名 `mcp_server`）。在 CloudWatch Logs Insights 里筛自家日志：

```
fields @timestamp, @message
| filter @message like /mcp_server/
| sort @timestamp desc
| limit 50
```

`WARNING` / `ERROR` 行表示后端 API 调用失败，可作为告警依据。

## 在 Connect 中配置

Gateway target 就绪后，去 Amazon Connect 控制台：

1. **AI Agent Designer** → 选择 AI Agent → **Add tool** → **Add existing AI Tool**
2. Namespace 选 `gateway_<gateway-name>`
3. 把这五个 AI Tool 都加进来（每次重复 Add existing AI Tool）：
   - `connect-repair-mcp-agent___verifyCustomer`
   - `connect-repair-mcp-agent___verifyCustomerByPhoneAndName`
   - `connect-repair-mcp-agent___requestRepair`
   - `connect-repair-mcp-agent___trackRepair`
   - `connect-repair-mcp-agent___cancelRepair`
4. **Output Filters** → Select Property Keys 里加 `result`（必须勾选，否则 LLM 读不到 `customerId` 等返回值）
5. 点 **Update** 保存

> **更新工具签名后必须重做引用**：MCP server 修改 tool 签名（参数名/必填项变化）并重新 deploy 后，AI Agent 持有的是更早部署时的工具描述快照。Gateway target 同步只刷新 Gateway 侧 schema，AI Agent 侧不会自动跟随。需要在 AI Agent Designer 里把这些工具 **Remove → 再 Add 回来**，让它拉到新 schema，否则 LLM 会按旧签名调用。

## 清理

```bash
./cleanup.sh
```

删除本次部署创建的所有资源（Gateway target、Runtime、ECR、S3、CodeBuild、IAM 角色）。脚本会自动判断 Gateway 是否是它自己创建的：
- **复用模式**（`.env` 里给了 `GATEWAY_ID`）：用户提供的 Gateway 与 service role **不会被删**，仅清理 target 和 InvokeAgentRuntime 内联策略
- **自动创建模式**（`.env` 里 `GATEWAY_ID` 为空）：连同 `${AGENT_NAME}-gw` Gateway 与 `${AGENT_NAME}-gateway-role` 一并删除

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
| `china_regions_pinyin.json` | 中国省/市/区拼音清单（运行时用于地址校验） |
| `gen_regions.py` | 一次性生成上面 JSON 的脚本（数据源变更时再跑） |

## 注意事项

- Runtime 必须用 **ARM64** 镜像，CodeBuild 已配成 ARM 环境
- Dockerfile 遵循官方示例：Python 3.11 基础镜像、非 root 用户 `bedrock_agentcore`、启动命令 `python -m mcp_server`
- 三个 tool 都使用 `@mcp.tool(structured_output=False)` —— 返回体只含 `content[]`，没有 `structuredContent` 字段，避免 Connect AI Agent 解析路径上的兼容问题
- Tool 函数体内手工 `json.dumps(dict)`（字符串），Connect Output Filter 必须勾选 `result`，否则 LLM 拿不到返回值
- `trackRepair` / `cancelRepair` 在工具内部就会校验 `woNumber` 非空且为 10 位数字，校验失败直接返回 `{"error": "INVALID_WO_NUMBER"}`，不会发出网络请求
- Gateway name 受 AWS 限制：仅允许 `[0-9a-zA-Z-]`，最长 48 字符。脚本自动用 `${AGENT_NAME}-gw` 拼接并裁剪
- Gateway audience 必须等于 Gateway 自身的 ID。一旦看到 Gateway 日志报 `insufficient_scope - The request requires higher privileges than provided by the access token.`，多半就是 audience 配错了
- API Key 通过环境变量注入到 Runtime（生产环境建议改用 Secrets Manager）
- `deploy.sh` 幂等，重复运行会更新 Runtime 并重新同步 Gateway target
