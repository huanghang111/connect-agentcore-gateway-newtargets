# Connect Repair MCP Server

把 Repair Service 工具封装成 MCP Server，部署到 AgentCore Runtime，通过 AgentCore Gateway 暴露给 Amazon Connect AI Agent。共 **6 个工具**：五个 repair 业务工具（`requestRepair` / `trackRepair` / `cancelRepair` / `updateRepair` / `listRepairs`）和一个 FAQ 检索工具（`faqSearch`）。**身份核验已完全停用** —— repair 工具仍接受 `callerName` + `callerPhoneTail` 两个参数（向后兼容），但既不校验也不使用，因此都是可选的；任意调用方均可创建 / 查询 / 修改 / 取消 / 列举任意工单。

## 架构

```
Connect AI Agent → AgentCore Gateway (mcpServer target) → AgentCore Runtime (Container) → Backend API
```

## 工具

| Tool | 功能 | 入参（前置校验要求见 docstring） |
|------|------|----------|
| `requestRepair` | 创建机器人维修工单 | `productCategory`(机器人品类), `productsubCategory`(对应部件), `description`, `brand` 必填；`callerName`, `callerPhoneTail`(均不校验), `productModel`, `serialNumber` 可选 |
| `trackRepair` | 查询工单状态（无身份/归属校验，任意调用方可查任意工单） | `woNumber`(WO-YYYY-NNNN) 必填；`callerName`, `callerPhoneTail` 可选(不校验) |
| `cancelRepair` | 取消工单（任意调用方可取消任意工单） | `woNumber`(WO-YYYY-NNNN) 必填；`callerName`, `callerPhoneTail` 可选(不校验) |
| `updateRepair` | 修改工单的故障描述/优先级/状态（任意调用方可改任意工单） | `woNumber` 必填；`description`/`priority`(P0 紧急/P1 高/P2 中/P3 低)/`status`(pending/scheduled/in_progress/completed) 至少传一个；`callerName`, `callerPhoneTail` 可选(不校验) |
| `listRepairs` | 模糊/聚合查询工单（按客户/状态/优先级/品类过滤 + 计数） | 全部可选：`customerName`(公司/联系人模糊子串), `openOnly`(只看 pending/scheduled/in_progress), `status`, `priority`, `productCategory` |
| `faqSearch` | FAQ 知识库自然语言检索（产品使用 / 故障排查 / 保修 / 维修） | `query`(任意自然语言问题) 必填 |

> **设计原则**：所有 tool 的使用方式都写在各自的 docstring 顶部，LLM 通过 `toolConfigurationList` 拿到 description 即可正确使用。**身份核验已完全停用**，Connect AI Agent 的 Orchestration Prompt 不需要任何身份相关配置。
>
> **身份模型（已停用）**：
> 1. **没有核身工具、没有客户注册表（CUSTOMER_REGISTRY 已删除）、没有 token / 缓存，也不做工单归属校验。**
> 2. 四个 repair 工具仍保留 `callerName` / `callerPhoneTail` 两个入参（向后兼容老的工具 schema），但服务端**既不校验也不使用**它们，两个参数都是**可选**的。
> 3. **任意调用方都可以创建 / 查询 / 修改 / 取消任意工单。** 后端 `track` / `cancel` / `update` 不再要求 `customerId`、不再比对 `customerPhone`；`404` 现在只表示「工单不存在」。
> 4. `requestRepair` 不再要求 `customerId`（可选）；若调用方仍传了 `customerId`，它会被写进工单的 `customerPhone` 字段（仅作记录，不用于鉴权），否则为 `null`。
>
> > 如需重新启用身份/归属校验：恢复 `mcp_server.py` 里的 caller 查表逻辑，并在后端三个 Lambda 中加回 `customerPhone === customerId` 判断。

每个 tool 的 docstring 顶部都列出了 **PRECONDITIONS**，明确字段在调用前必须满足的约束（机器人品类 + 对应部件的组合校验、型号/SN）。

### 接口契约速查（MCP tool ↔ 后端 API）

> **身份核验已停用**：repair tool 不再核身，也不向后端传 `customerId`（`callerName`/`callerPhoneTail` 入参保留但忽略）。`trackRepair` / `cancelRepair` / `updateRepair` 后端不再做归属校验，任意调用方可操作任意工单，`404` 只表示工单不存在。
> 全部 repair tool 都会在拿到后端响应后过一次 Strands+Bedrock 归一化（详见"响应归一化"章节），表里的"出参"指的就是归一化后的 schema —— 也是 LLM 实际看到的字段。

#### 1. `requestRepair` — 创建工单

| 项 | 内容 |
|----|------|
| MCP 入参（必填） | `productCategory`, `productsubCategory`, `description`, `brand` |
| MCP 入参（可选） | `callerName`, `callerPhoneTail`(均不校验), `productModel`, `serialNumber` |
| MCP 本地校验 | `INVALID_CATEGORY` / `INVALID_SUB_CATEGORY`（详见下面的"服务端校验"） |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/request`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{productCategory, productsubCategory, productModel, serialNumber, description, brand}`（camelCase；不再传 `customerId`） |
| 后端必填校验 | `productCategory, productsubCategory, description, brand` 缺任意一个 → `400 {"error":"Missing required fields: ..."}` |
| 后端写库 | DynamoDB `RepairTicketsTable` PutItem：`ticketNumber`(WO-YYYY-NNNN) + 上面字段 + `customerPhone:null` + `status:"pending"` + `priority:"P2"` + `createdAt` + `updatedAt` |
| 后端响应（201） | `{"message":"Repair ticket created successfully","ticketNumber":"...","ticket":{...}}` |
| MCP 归一化出参（`RequestResponse`） | `{woNumber, created(bool), status, scheduledAt, message}` |

#### 2. `trackRepair` — 查询工单

| 项 | 内容 |
|----|------|
| MCP 入参（必填） | `woNumber`(WO-YYYY-NNNN) |
| MCP 入参（可选） | `callerName`, `callerPhoneTail`(均不校验) |
| MCP 本地校验 | `INVALID_WO_NUMBER` |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/track`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{woNumber}`（不再需要 `customerId`） |
| 后端必填校验 | `woNumber` 必须 WO-YYYY-NNNN 格式；**无归属校验**，任意调用方可查任意工单；工单不存在 → `404` |
| 后端响应（200） | `{"message":"Repair ticket found","ticket":{ticketNumber,status,priority,productCategory,productsubCategory,productModel,serialNumber,brand,customerName,customerPhone,description,createdAt,updatedAt}}`；不存在 → `404` |
| MCP 归一化出参（`TrackResponse`） | `{woNumber, status, statusDescription, scheduledAt, technicianName, technicianPhone, address, lastUpdatedAt, remarks}` |

#### 3. `cancelRepair` — 取消工单

| 项 | 内容 |
|----|------|
| MCP 入参（必填） | `woNumber`(WO-YYYY-NNNN) |
| MCP 入参（可选） | `callerName`, `callerPhoneTail`(均不校验) |
| MCP 本地校验 | `INVALID_WO_NUMBER` |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/cancel`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{woNumber}`（不再需要 `customerId`） |
| 后端必填校验 | `woNumber` 必须 WO-YYYY-NNNN 格式；**无归属校验**，任意调用方可取消任意工单；工单不存在 → `404` |
| 后端响应（200） | `{"message":"Repair ticket cancelled","ticketNumber":"...","status":"cancelled"}`；不存在 → `404`；状态已是 `cancelled`/`completed` → `409 {"error":"Work order is already ...","status":"..."}` |
| MCP 归一化出参（`CancelResponse`） | `{woNumber, cancelled(bool), status, message}` |

#### 4. `updateRepair` — 修改工单（故障描述 / 优先级 / 状态）

| 项 | 内容 |
|----|------|
| MCP 入参（必填） | `woNumber`(WO-YYYY-NNNN) |
| MCP 入参（可选，至少一个改动字段） | `description`、`priority`(P0 紧急/P1 高/P2 中/P3 低)、`status`(pending/scheduled/in_progress/completed)；另 `callerName`, `callerPhoneTail`(均不校验) |
| MCP 本地校验 | `INVALID_WO_NUMBER` / `NOTHING_TO_UPDATE` / `INVALID_PRIORITY` / `INVALID_STATUS` |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/update`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{woNumber, description?, priority?, status?}`（只带传了的字段；不再需要 `customerId`） |
| 后端必填校验 | `woNumber` 格式；至少一个可改字段；priority/status 枚举校验；**无归属校验**，任意调用方可改任意工单；工单不存在 → `404`。`status` 不接受 `cancelled`（取消用 cancelRepair） |
| 后端响应（200） | `{"message":"Repair ticket updated","ticketNumber":"...","status":"...","priority":"...","description":"...","updatedAt":"..."}`；不存在 → `404`；状态已是 `cancelled`/`completed` → `409` |
| MCP 归一化出参（`UpdateResponse`） | `{woNumber, updated(bool), status, priority, description, message}` |

#### 5. `listRepairs` — 模糊/聚合查询

| 项 | 内容 |
|----|------|
| MCP 入参（全部可选） | `customerName`(公司/联系人模糊子串，忽略大小写), `openOnly`(true=只看 pending/scheduled/in_progress), `status`, `priority`, `productCategory` |
| MCP 本地校验 | `INVALID_PRIORITY` / `INVALID_STATUS`（status 过滤可含 `cancelled`） |
| 后端 endpoint | `POST {REPAIR_API_URL}/repair/list`，header `X-API-Key: <REPAIR_API_KEY>` |
| 后端 body | `{customerName?, openOnly?, status?, priority?, productCategory?}`（只带传了的字段；全空=列全部） |
| 后端实现 | DynamoDB `Scan` 全表（表很小）+ 内存按 AND 过滤；按 priority/woNumber 排序 |
| 后端响应（200） | `{message, count, urgentCount(=P0 数), byPriority{}, byStatus{}, tickets:[{ticketNumber,status,priority,productCategory,productsubCategory,productModel,customerName,description,updatedAt}]}` |
| MCP 出参 | 同后端响应原样返回（**不过归一化**；聚合字段已是固定 schema，供 LLM 答"多少个 / 有无紧急"） |

#### 错误码速查（MCP 本地拦截，不会打到后端）

| 错误码 | 触发位置 |
|--------|---------|
| `INVALID_CATEGORY` | `requestRepair`（品类不在 4 类机器人内） |
| `INVALID_SUB_CATEGORY` | `requestRepair`（部件不属于该品类） |
| `INVALID_WO_NUMBER` | `trackRepair` / `cancelRepair` / `updateRepair` |
| `NOTHING_TO_UPDATE` | `updateRepair`（description/priority/status 一个都没传） |
| `INVALID_PRIORITY` | `updateRepair` / `listRepairs`（priority 不在 P0/P1/P2/P3 内） |
| `INVALID_STATUS` | `updateRepair`（不在 pending/scheduled/in_progress/completed） / `listRepairs`（不在含 cancelled 的 5 个状态内） |
| `HTTP 404`（不存在） | `trackRepair` / `cancelRepair` / `updateRepair`：工单不存在（已无归属校验） |
| `HTTP 400/404/409/500` | 后端透传，归一化时直接跳过（错误路径保持确定） |

### 服务端校验（`requestRepair`）

`requestRepair` 在打到后端之前会做一层本地校验，校验失败立刻返回错误、不发出网络请求：

| 字段 | 规则 | 失败时返回 |
|------|------|------------|
| `productCategory` + `productsubCategory` | **组合校验**：`productCategory` 必须是 4 类机器人之一，且 `productsubCategory` 必须是该品类下的合法部件（大小写、空格不敏感）。映射见下表 | `{"error": "INVALID_CATEGORY", "allowed": [...]}`（品类不对）或 `{"error": "INVALID_SUB_CATEGORY", "allowed": [...]}`（部件不属于该品类，`allowed` 列出该品类的合法部件） |

机器人品类 → 部件映射（`mcp_server.py` 中的 `ROBOT_CATEGORIES`）：

| 品类 | 合法部件 | 型号示例 |
|------|----------|----------|
| 仓储机器人 | 导航传感器 / 电池 / 驱动电机 / 通信模块 | WR-500, WR-800 |
| 巡检机器人 | 热成像模块 / 轮组 / 气体传感器 / 通信 | IR-200, IR-400 |
| 协作机械臂 | 关节电机 / 力矩传感器 / 控制器 / 线缆 | CA-100, CA-300 |
| 服务机器人 | 语音模块 / 屏幕 / 导航 / 电池 | SR-50, SR-100 |

### 响应归一化（`requestRepair` / `trackRepair` / `cancelRepair`）

不同 BU 的后端 API 返回字段名经常不一致 —— 同一个"工单状态"字段可能叫 `status`、`ticketstatus`、`tstatus`、`statusName`，工单号可能叫 `woNumber`、`wono`、`ticketId`、`orderNumber`，让上层 LLM 难以稳定播报。MCP server 在三个写/查工具拿到后端响应后，会通过 **Strands Agents SDK** 调用 **Bedrock**（默认 `us.anthropic.claude-haiku-4-5-20251001-v1:0`），用 `agent.structured_output(PydanticModel, prompt)` 把原始 JSON 强制映射到固定 Pydantic schema 上 —— Strands 走 Bedrock tool-use 通道，模型几乎不会吐出格式非法的 JSON。

| 工具 | 规范字段（来自 `mcp_server.py` 中的 Pydantic 模型） |
|------|-----------------------------------------------------|
| `requestRepair` | `RequestResponse`：`woNumber`, `created`(bool), `status`, `scheduledAt`, `message` |
| `trackRepair` | `TrackResponse`：`woNumber`, `status`, `statusDescription`, `scheduledAt`, `technicianName`, `technicianPhone`, `address`, `lastUpdatedAt`, `remarks` |
| `cancelRepair` | `CancelResponse`：`woNumber`, `cancelled`(bool), `status`, `message` |

> ⚠️ `requestRepair` 是延迟敏感路径，归一化每次约 +0.5–1s。如果实测延迟拖累播报体验，可以单独把 `requestRepair` 的 `_normalize_with_llm(...)` 一行注释掉，或全局 `NORMALIZE_RESPONSE=0` 关闭。

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

## 部署

> 本目录**不再有自己的 `deploy.sh`** —— 在 `quick-mcp` 分支里所有部署逻辑都合并到了仓库根目录的 `midea/deploy.sh`，从 backend API 到 AgentCore Runtime/Gateway/Target 一条命令搞定。详见 [`../README.md`](../README.md)。

```bash
cd midea           # 注意是上一级目录
chmod +x deploy.sh cleanup.sh
cp .env.example .env
./deploy.sh
```

`./deploy.sh` 把这个目录里的源码 (`mcp_server.py`、`Dockerfile`、`requirements.txt`、`buildspec.yml`) 打包送进 CodeBuild 构建 ARM64 镜像、创建 Runtime、并自动创建一个 `authorizerType=NONE` 的 Gateway（仅适用于测试 / Quick Desktop & Quick Web 演示）。

> ⚠️ Inbound auth 是 `NONE` —— 任何拿到 MCP URL 的人都能调你的工具。生产环境请通过 `.env` 的 `GATEWAY_ID` + `GATEWAY_SERVICE_ROLE` 复用一个已经配好 `CUSTOM_JWT` 或 `AWS_IAM` 的 Gateway。

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
3. 把这四个 AI Tool 都加进来（每次重复 Add existing AI Tool）：
   - `connect-repair-mcp-agent___requestRepair`
   - `connect-repair-mcp-agent___trackRepair`
   - `connect-repair-mcp-agent___cancelRepair`
   - `connect-repair-mcp-agent___faqSearch`
4. 每个 tool 的 **Output Filters** → Select Property Keys 里加 `result`（必须勾选，否则 LLM 读不到工单号等返回值）
5. 身份核验已停用，**无需**任何 Connect 侧身份配置（不需要 `userNumber` / `customer_info` / Contact Flow 属性）。`callerName` / `callerPhoneTail` 入参仍存在但不校验，AI Agent 可不填。
6. 点 **Update / Publish** 保存 AI Agent。

> **更新工具签名后必须重做引用**：MCP server 修改 tool 签名（参数名/必填项变化）并重新 deploy 后，AI Agent 持有的是更早部署时的工具描述快照。Gateway target 同步只刷新 Gateway 侧 schema，AI Agent 侧不会自动跟随。需要在 AI Agent Designer 里把这些工具 **Remove → 再 Add 回来**，让它拉到新 schema，否则 LLM 会按旧签名调用。

> **如何快速诊断工具调用**：
> - 看 **AgentCore Gateway APPLICATION_LOGS**（log group `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/<gateway-name>`，需在 Gateway 上启用日志）的 `tools/call` 行，确认 `arguments` 里的 `woNumber` / 品类 / 字段是否正确。
> - 身份核验已停用：调用不需要任何身份字段；`404` 只表示工单不存在，`400` 多为 woNumber 格式 / 缺必填字段 / 非法 priority|status。

## 清理

```bash
cd midea          # 上一级目录
./cleanup.sh
```

按 deploy 的反向顺序拆 Gateway target / Runtime / ECR / S3 / CodeBuild / IAM 角色 / CFN stack / API CFN bucket。脚本会自动判断 Gateway 是否是 deploy.sh 自建的：
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
| `mcp_server.py` | MCP Server 实现（FastMCP + 6 个 tool：5 repair + 1 FAQ；身份核验已停用） |
| `Dockerfile` | ARM64 容器（Python 3.11，非 root 用户） |
| `requirements.txt` | Python 依赖 |
| `buildspec.yml` | CodeBuild 构建脚本 |

> 部署/清理脚本和 `.env` 模板都在仓库根目录 `midea/`，详见 [`../README.md`](../README.md)。

## 注意事项

- Runtime 必须用 **ARM64** 镜像，CodeBuild 已配成 ARM 环境
- Dockerfile 遵循官方示例：Python 3.11 基础镜像、非 root 用户 `bedrock_agentcore`、启动命令 `opentelemetry-instrument python -m mcp_server`（ADOT 自动加载 OTEL，详见上面的"可观测性"章节）
- 所有 tool 都使用 `@mcp.tool(structured_output=False)` —— 返回体只含 `content[]`，没有 `structuredContent` 字段，避免 Connect AI Agent 解析路径上的兼容问题
- Tool 函数体内手工 `json.dumps(dict)`（字符串），Connect Output Filter 必须勾选 `result`，否则 LLM 拿不到返回值
- `trackRepair` / `cancelRepair` 在工具内部就会校验 `woNumber` 非空且为 WO-YYYY-NNNN 格式，校验失败直接返回 `{"error": "INVALID_WO_NUMBER"}`，不会发出网络请求
- Gateway name 受 AWS 限制：仅允许 `[0-9a-zA-Z-]`，最长 48 字符。脚本自动用 `${AGENT_NAME}-gw` 拼接并裁剪
- Gateway audience 必须等于 Gateway 自身的 ID。一旦看到 Gateway 日志报 `insufficient_scope - The request requires higher privileges than provided by the access token.`，多半就是 audience 配错了
- API Key 通过环境变量注入到 Runtime（生产环境建议改用 Secrets Manager）
- `deploy.sh` 幂等，重复运行会更新 Runtime 并重新同步 Gateway target
